# Explicit-mode builds. No arguments: help only, with no filesystem changes.
param(
    [string]$Mode,
    [switch]$SkipStartupCheck,
    [Alias('h')][switch]$Help
)

function Show-BuildHelp {
    Write-Host 'Usage: .\build_dual.ps1 <gui|cli|onedir|all> [-SkipStartupCheck]'
    Write-Host '       .\build_dual.ps1 -Mode <gui|cli|onedir|all>'
    Write-Host '       .\build_dual.ps1 [-Help|-h|--help]'
    Write-Host ''
    Write-Host 'Modes:'
    Write-Host '  gui     GUI single-file EXE -> dist\SSHDriveMgr.exe'
    Write-Host '  cli     CLI single-file EXE -> dist\SSHDriveMgr-cli.exe'
    Write-Host '  onedir  GUI folder build   -> dist\SSHDriveMgr-onedir\SSHDriveMgr.exe'
    Write-Host '  all     Build all three modes (gui, cli, onedir).'
    Write-Host ''
    Write-Host 'No mode: display this help only; do not clean, sync versions, or compile.'
    Write-Host 'Before any build, remove all three old PyInstaller caches under build/.'
    Write-Host 'Preserve build logs/test data and unselected dist outputs. Never stop user processes.'
    Write-Host 'Close running output apps first; keep them closed throughout compilation.'
    Write-Host 'Default checks: version/freshness, CLI --help, and visible private GUI Login.'
    Write-Host 'GUI checks require a Windows desktop and no other GUI instance.'
    Write-Host '-SkipStartupCheck explicitly skips launch checks (for example headless CI).'
    Write-Host 'Old -OneDir/-NoClean flags are replaced by explicit modes and mandatory cache cleanup.'
    Write-Host 'Run published dist outputs only, never the intermediate EXE under build/.'
}

$helpTokens = @('help', '--help', '-?', '--?')
if ($Help -or ($Mode -in $helpTokens) -or (@($args | Where-Object { $_ -in $helpTokens }).Count -gt 0)) {
    Show-BuildHelp
    exit 0
}
if ([string]::IsNullOrWhiteSpace($Mode) -and $args.Count -eq 0) {
    Show-BuildHelp
    exit 0
}
if (($Mode -notin @('gui', 'cli', 'onedir', 'all')) -or $args.Count -gt 0) {
    Show-BuildHelp
    Write-Error 'Choose exactly one build mode: gui, cli, onedir, or all.'
    exit 2
}
$Mode = $Mode.ToLowerInvariant()
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-GeneratedBuildPath([string]$RelativePath) {
    $root = [System.IO.Path]::GetFullPath($PSScriptRoot)
    $target = [System.IO.Path]::GetFullPath((Join-Path $root $RelativePath))
    $prefix = $root + [System.IO.Path]::DirectorySeparatorChar
    if (-not $target.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing build operation outside the project: $target"
    }
    # Never traverse a junction/symlink while cleaning or replacing outputs.
    $cursor = $target
    while (-not $cursor.Equals($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        try {
            # Get-Item also sees dangling links that Test-Path reports as absent.
            $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
            if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                throw "Refusing redirected build path: $cursor"
            }
        } catch [System.Management.Automation.ItemNotFoundException] {
            # A genuinely missing generated item is safe to create later.
        }
        $cursor = Split-Path -Parent $cursor
    }
    return $target
}

function Remove-GeneratedBuildItem([string]$RelativePath) {
    $allowed = @(
        'build/SSHDriveMgr', 'build/SSHDriveMgr-cli', 'build/SSHDriveMgr-onedir',
        'dist/SSHDriveMgr-onedir'
    )
    if ($RelativePath -notin $allowed) {
        throw "Refusing to clean a non-generated build item: $RelativePath"
    }
    $target = Get-GeneratedBuildPath $RelativePath
    if (Test-Path -LiteralPath $target) {
        Write-Host "[clean] $target"
        Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction Stop
    }
}

function Assert-GeneratedOutputAvailable([string]$RelativePath) {
    # Validate root/ancestors once (including dangling links), not once per descendant.
    $target = Get-GeneratedBuildPath $RelativePath
    if (-not (Test-Path -LiteralPath $target)) { return }
    $prefix = $target + [System.IO.Path]::DirectorySeparatorChar
    $pending = [System.Collections.Generic.Stack[object]]::new()
    $pending.Push((Get-Item -LiteralPath $target -Force -ErrorAction Stop))
    while ($pending.Count -gt 0) {
        $item = $pending.Pop()
        $itemPath = [System.IO.Path]::GetFullPath($item.FullName)
        if (-not ($itemPath.Equals($target, [System.StringComparison]::OrdinalIgnoreCase) -or
            $itemPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase))) {
            throw "Refusing build operation outside the output tree: $itemPath"
        }
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            throw "Refusing redirected build path: $itemPath"
        }
        if ($item.PSIsContainer) {
            # Enumerate each directory once; validate each returned child before descent/open.
            foreach ($child in Get-ChildItem -LiteralPath $itemPath -Force -ErrorAction Stop) {
                $pending.Push($child)
            }
            continue
        }
        $stream = $null
        try {
            # Read-only, exclusive sharing probe: never truncate, write, or change ACLs.
            $stream = [System.IO.File]::Open($itemPath, [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::Read, [System.IO.FileShare]::None)
        } catch {
            throw "Build output is locked or unreadable: $itemPath. Close its owner; exit via tray, wait for process to stop, retry. $($_.Exception.Message)"
        } finally {
            if ($null -ne $stream) { $stream.Dispose() }
        }
    }
}

function Assert-BuildOutputsAvailable([string[]]$OutputScopes, [switch]$IncludeBuildCaches, [switch]$IncludeOneDir) {
    # Explicit plans supply only selected dist scopes. The optional switch remains
    # an internal helper convenience, not a legacy command-line build mode.
    $outputs = @()
    if ($null -ne $OutputScopes) { $outputs = @($OutputScopes) }
    if ($outputs.Count -eq 0) {
        $outputs = @('dist/SSHDriveMgr.exe', 'dist/SSHDriveMgr-cli.exe')
        if ($IncludeOneDir) { $outputs += 'dist/SSHDriveMgr-onedir' }
    }
    $targets = @($outputs | ForEach-Object { Get-GeneratedBuildPath $_ })
    $processTrees = @($outputs)
    # Every explicit build clears all three known intermediate cache trees.
    if ($IncludeBuildCaches) {
        $processTrees += @('build/SSHDriveMgr', 'build/SSHDriveMgr-cli', 'build/SSHDriveMgr-onedir')
    }
    $processTreePrefixes = @($processTrees | ForEach-Object {
        (Get-GeneratedBuildPath $_) + [System.IO.Path]::DirectorySeparatorChar
    })
    # Inspect current executable paths, not names or command lines (which may contain secrets).
    # Other users' protected process metadata and processes exiting mid-enumeration are normal.
    foreach ($process in Get-Process -ErrorAction SilentlyContinue) {
        try {
            $processPath = $process.Path
            if ([string]::IsNullOrWhiteSpace($processPath)) { continue }
            $processPath = [System.IO.Path]::GetFullPath($processPath)
            $processPath = (Resolve-Path -LiteralPath $processPath -ErrorAction Stop).ProviderPath
            $processId = $process.Id
        } catch { continue }
        $matchesOutput = $false
        foreach ($targetPath in $targets) {
            $matchesOutput = $matchesOutput -or $processPath.Equals($targetPath, [System.StringComparison]::OrdinalIgnoreCase)
        }
        foreach ($treePrefix in $processTreePrefixes) {
            $matchesOutput = $matchesOutput -or $processPath.StartsWith($treePrefix, [System.StringComparison]::OrdinalIgnoreCase)
        }
        if ($matchesOutput) {
            throw "Build output is running: PID $processId, $processPath. Please exit via tray, wait for process to stop, retry."
        }
    }
    foreach ($output in $outputs) { Assert-GeneratedOutputAvailable $output }
    # These are snapshots, not a reservation: another process can start/open files afterwards.
    # Keep destructive-operation errors explicit; never suppress a later sharing/ACL failure.
}

function Build-Executable([string]$Spec, [string]$Output, [string]$Version, [string]$Scope) {
    # Snapshot again before replacement; never stop an app or suppress a sharing error.
    Assert-BuildOutputsAvailable -OutputScopes $script:SelectedOutputScopes
    $target = Get-GeneratedBuildPath $Output
    $distRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'dist')) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $target.StartsWith($distRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove output outside dist: $target"
    }
    if ($Scope -eq 'dist/SSHDriveMgr-onedir') {
        Remove-GeneratedBuildItem 'dist/SSHDriveMgr-onedir'
    } elseif (Test-Path -LiteralPath $target) {
        if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw "Expected executable file: $target" }
        Remove-Item -LiteralPath $target -Force
    }
    $started = [DateTime]::UtcNow
    & $pyinstaller --noconfirm $Spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed for $Spec (exit $LASTEXITCODE)." }
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw "Missing build output: $target" }
    $artifact = Get-Item -LiteralPath $target
    if ($artifact.Length -le 0 -or $artifact.LastWriteTimeUtc -lt $started.AddSeconds(-2)) {
        throw "Empty or stale build output: $target"
    }
    if ($artifact.VersionInfo.ProductVersion -ne $Version) {
        throw "Embedded product version mismatch in $target (expected $Version)."
    }
}

# Plans are data, so each mode has one authoritative spec/output/scope mapping.
$definitions = @{
    gui = [pscustomobject]@{ Mode = 'gui'; Spec = 'SSHDriveMgr.spec'; Output = 'dist/SSHDriveMgr.exe'; Scope = 'dist/SSHDriveMgr.exe' }
    cli = [pscustomobject]@{ Mode = 'cli'; Spec = 'SSHDriveMgr-cli.spec'; Output = 'dist/SSHDriveMgr-cli.exe'; Scope = 'dist/SSHDriveMgr-cli.exe' }
    onedir = [pscustomobject]@{ Mode = 'onedir'; Spec = 'SSHDriveMgr-onedir.spec'; Output = 'dist/SSHDriveMgr-onedir/SSHDriveMgr.exe'; Scope = 'dist/SSHDriveMgr-onedir' }
}
$modeNames = if ($Mode -eq 'all') { @('gui', 'cli', 'onedir') } else { @($Mode) }
$plans = @($modeNames | ForEach-Object { $definitions[$_] })
$script:SelectedOutputScopes = @($plans | ForEach-Object { $_.Scope })

Push-Location $PSScriptRoot
try {
    $pyinstaller = if (Test-Path '.venv/Scripts/pyinstaller.exe') {
        Join-Path $PSScriptRoot '.venv/Scripts/pyinstaller.exe'
    } else { 'pyinstaller' }
    $null = Get-Command $pyinstaller -ErrorAction Stop
    foreach ($plan in $plans) {
        if (-not (Test-Path -LiteralPath $plan.Spec -PathType Leaf)) { throw "Missing build spec: $($plan.Spec)" }
    }
    # Fail before touching old caches or outputs. No unselected dist tree is probed.
    Assert-BuildOutputsAvailable -OutputScopes $script:SelectedOutputScopes -IncludeBuildCaches
    & (Join-Path $PSScriptRoot 'scripts/sync_version.ps1')
    if (-not $?) { throw 'Version sync failed.' }
    $version = (Get-Content 'src/version.txt' -Raw -Encoding UTF8).Trim()
    if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid src/version.txt.' }
    # Always clear all old versions of the three known PyInstaller work trees.
    # Do not recursively erase unrelated logs, diagnostics or test data in build/.
    foreach ($name in @('build/SSHDriveMgr', 'build/SSHDriveMgr-cli', 'build/SSHDriveMgr-onedir')) {
        Remove-GeneratedBuildItem $name
    }
    foreach ($plan in $plans) {
        Write-Host ("[build] {0}: {1}" -f $plan.Mode, $plan.Spec) -ForegroundColor Cyan
        Build-Executable $plan.Spec $plan.Output $version $plan.Scope
        if ($plan.Mode -eq 'onedir') {
            $embedded = (Get-Content 'dist/SSHDriveMgr-onedir/_internal/src/version.txt' -Raw -Encoding UTF8).Trim()
            if ($embedded -ne $version) { throw 'Onedir embedded src/version.txt is inconsistent.' }
        }
    }
    if ($SkipStartupCheck) {
        Write-Warning 'Startup checks SKIPPED explicitly; runnable startup has not been verified.'
    } else {
        $python = $null
        if (@($plans | Where-Object { $_.Mode -ne 'cli' }).Count -gt 0) {
            # Use Python beside the selected PyInstaller, not an unrelated PATH interpreter.
            $installerPath = (Get-Command $pyinstaller -ErrorAction Stop).Source
            $scriptsPath = Split-Path -Parent $installerPath
            $python = Join-Path $scriptsPath 'python.exe'
            if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
                $python = Join-Path (Split-Path -Parent $scriptsPath) 'python.exe'
            }
            if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
                throw "Cannot find Python for selected PyInstaller: $installerPath. Use the project .venv or -SkipStartupCheck explicitly."
            }
        }
        foreach ($plan in $plans) {
            if ($plan.Mode -eq 'cli') {
                $start = [System.Diagnostics.ProcessStartInfo]::new()
                $start.FileName = Get-GeneratedBuildPath $plan.Output
                $start.Arguments = '--help'
                $start.UseShellExecute = $false
                $start.CreateNoWindow = $true
                $start.RedirectStandardOutput = $true
                $start.RedirectStandardError = $true
                $probe = [System.Diagnostics.Process]::Start($start)
                try {
                    $stdoutTask = $probe.StandardOutput.ReadToEndAsync()
                    $stderrTask = $probe.StandardError.ReadToEndAsync()
                    if (-not $probe.WaitForExit(30000)) {
                        $probe.Kill() # only this owned no-connection help probe, never user sessions
                        $probe.WaitForExit()
                        throw 'CLI help startup timed out.'
                    }
                    $text = $stdoutTask.GetAwaiter().GetResult() + $stderrTask.GetAwaiter().GetResult()
                    if ($probe.ExitCode -ne 2 -or $text -notmatch 'Usage:') { throw 'CLI help startup check failed.' }
                    Write-Host '[verify] CLI help startup PASS (expected usage exit 2).'
                } finally { $probe.Dispose() }
            } else {
                Write-Host '[verify] The private test Login window will close automatically; this is not your account session.'
                $startupArgs = @((Join-Path $PSScriptRoot 'scripts/verify_gui_startup.py'))
                if ($plan.Mode -eq 'onedir') { $startupArgs += '--onedir' }
                & $python @startupArgs
                if ($LASTEXITCODE -ne 0) { throw "NORMAL GUI startup check failed (exit $LASTEXITCODE); see build/startup-check-* logs above." }
            }
        }
    }
    Write-Host "Build complete: $Mode ($version)." -ForegroundColor Green
    foreach ($plan in $plans) { Write-Host ($plan.Mode + ': ' + (Join-Path $PSScriptRoot $plan.Output)) }
    if ($modeNames -contains 'onedir') { Write-Host 'Keep the complete SSHDriveMgr-onedir folder, including _internal.' }
    Write-Host ('Launcher: ' + (Join-Path $PSScriptRoot 'Start-SSHDriveMgr.cmd'))
    Write-Warning 'build/ contains intermediate files, NOT runnable releases. Launch only the dist paths above.'
} finally {
    Pop-Location
}
exit 0
