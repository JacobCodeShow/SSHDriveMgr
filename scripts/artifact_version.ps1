# Dot-source this file; validation reads only the supplied VERSIONINFO object.
function Assert-ArtifactVersion {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowNull()]
        [object]$VersionInfo,

        [Parameter(Mandatory = $true)]
        [AllowNull()]
        [AllowEmptyString()]
        [string]$Version,

        [Parameter(Mandatory = $true)]
        [string]$Artifact
    )

    # Do not use System.Version: accepting a fourth component or discarding a
    # suffix would weaken the exact three-component resource-string contract.
    if ($Version -cnotmatch '\A[0-9]+\.[0-9]+\.[0-9]+\z') {
        throw "Invalid expected artifact version '$Version' for '$Artifact': require three decimal components X.Y.Z, each <= 65535."
    }
    $expectedParts = @()
    foreach ($part in $Version.Split('.')) {
        [uint16]$number = 0
        if (-not [uint16]::TryParse($part, [ref]$number)) {
            throw "Invalid expected artifact version '$Version' for '$Artifact': each decimal component must be <= 65535."
        }
        $expectedParts += [int]$number
    }
    $expectedParts += 0

    $problems = @()
    $diagnostics = @(
        "[version] artifact='$Artifact'"
        "expected text='$Version' (length=$($Version.Length))"
        "expected fixed=[$($expectedParts -join ',')]"
    )
    foreach ($name in @('ProductVersion', 'FileVersion')) {
        $property = $null
        if ($null -ne $VersionInfo) {
            $property = $VersionInfo.PSObject.Properties[$name]
        }
        if ($null -eq $property -or $null -eq $property.Value) {
            $diagnostics += "$name normalized=<missing> (normalized length=n/a, raw length=n/a)"
            $problems += "$name is missing or null"
            continue
        }
        if ($property.Value -isnot [string]) {
            $diagnostics += "$name normalized=<non-string> (normalized length=n/a, raw length=n/a)"
            $problems += "$name must be a string"
            continue
        }
        $raw = $property.Value
        # Inno/.NET can expose padded resource strings. Only these two trailing
        # ASCII characters are padding; tabs, Unicode whitespace and all other
        # leading/internal/trailing characters remain significant.
        $normalized = $raw.TrimEnd([char[]]@([char]0x20, [char]0x00))
        $visible = $normalized.Replace([string][char]0, '\0').Replace("`r", '\r').Replace("`n", '\n').Replace("`t", '\t')
        $diagnostics += "$name normalized='$visible' (normalized length=$($normalized.Length), raw length=$($raw.Length))"
        # PowerShell -cne is culture-sensitive and can ignore embedded NUL.
        if (-not [string]::Equals($normalized, $Version, [StringComparison]::Ordinal)) {
            $problems += "$name text does not exactly match expected '$Version'"
        }
    }

    $integerTypes = @([byte], [sbyte], [int16], [uint16], [int32], [uint32], [int64], [uint64])
    foreach ($prefix in @('Product', 'File')) {
        $observedParts = @()
        $index = 0
        foreach ($suffix in @('MajorPart', 'MinorPart', 'BuildPart', 'PrivatePart')) {
            $name = $prefix + $suffix
            $property = $null
            if ($null -ne $VersionInfo) {
                $property = $VersionInfo.PSObject.Properties[$name]
            }
            if ($null -eq $property -or $null -eq $property.Value) {
                $observedParts += "$name=<missing>"
                $problems += "$name is missing or null (expected $($expectedParts[$index]))"
            } else {
                $value = $property.Value
                $observedParts += "$name=$value"
                if ($integerTypes -notcontains $value.GetType() -or $value -ne $expectedParts[$index]) {
                    $problems += "$name must be integer $($expectedParts[$index]) (observed '$value', type=$($value.GetType().Name))"
                }
            }
            $index++
        }
        $diagnostics += "$prefix fixed=[$($observedParts -join ',')]"
    }

    $message = $diagnostics -join '; '
    Write-Host $message
    if ($problems.Count -gt 0) {
        throw "Artifact embedded version mismatch: $($problems -join '; '). $message"
    }
    # Deliberately no success-stream result: callers need diagnostics, not $true.
}
