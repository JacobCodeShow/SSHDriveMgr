# Build GUI/CLI and compile Inno Setup. CI may reuse executables built this job.
param([switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    if (-not $SkipBuild) {
        foreach ($mode in @('gui', 'cli')) {
            & (Join-Path $repoRoot 'build_dual.ps1') -Mode $mode
            $buildSucceeded = $?
            $buildExitCode = $LASTEXITCODE
            if (-not $buildSucceeded -or $buildExitCode -ne 0) {
                throw "Executable build failed ($mode, exit code $buildExitCode)."
            }
        }
    }
    $version = (Get-Content 'src/version.txt' -Raw -Encoding UTF8).Trim()
    if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid src/version.txt.' }
    foreach ($name in @('SSHDriveMgr.exe', 'SSHDriveMgr-cli.exe')) {
        $artifact = Get-Item -LiteralPath (Join-Path $repoRoot "dist/$name")
        if ($artifact.Length -le 0 -or $artifact.VersionInfo.ProductVersion -ne $version) {
            throw "Missing, empty or wrong-version input executable: $name"
        }
    }
    $iscc = 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
    if (-not (Test-Path -LiteralPath $iscc)) { $iscc = 'C:\Program Files\Inno Setup 6\ISCC.exe' }
    if (-not (Test-Path -LiteralPath $iscc)) {
        $iscc = (Get-Command ISCC.exe -ErrorAction Stop).Source
    }
    $outDir = [System.IO.Path]::GetFullPath((Join-Path $repoRoot 'dist_installer'))
    if ((Split-Path $outDir -Parent) -ne $repoRoot) { throw "Unsafe installer output directory: $outDir" }
    # The entire output directory is dedicated to installers; do not glob-pick
    # an old version after a failed compile.
    if (Test-Path -LiteralPath $outDir) {
        Get-ChildItem -LiteralPath $outDir -Filter 'SSHDriveMgr-Setup-*.exe' | ForEach-Object {
            if ($_.DirectoryName -ne $outDir) { throw 'Unsafe stale installer path.' }
            Remove-Item -LiteralPath $_.FullName -Force
        }
    }
    $started = [DateTime]::UtcNow
    & $iscc (Join-Path $PSScriptRoot 'SSHDriveMgr.iss')
    if ($LASTEXITCODE -ne 0) { throw "ISCC failed with exit code $LASTEXITCODE." }
    $setup = Join-Path $outDir "SSHDriveMgr-Setup-$version.exe"
    $artifact = Get-Item -LiteralPath $setup -ErrorAction Stop
    if ($artifact.Length -le 0 -or $artifact.LastWriteTimeUtc -lt $started.AddSeconds(-2)) {
        throw "Empty or stale installer output: $setup"
    }
    $productVersion = $artifact.VersionInfo.ProductVersion
    $fileVersion = $artifact.VersionInfo.FileVersion
    Write-Host ("[version] Installer expected='{0}', ProductVersion='{1}', FileVersion='{2}'" -f $version, $productVersion, $fileVersion)
    if ($productVersion -cne $version -or $fileVersion -cne $version) {
        throw "Installer embedded version mismatch: expected '$version', ProductVersion='$productVersion', FileVersion='$fileVersion', artifact='$setup'."
    }
    Write-Host "Done! $setup" -ForegroundColor Green
} finally {
    Pop-Location
}
