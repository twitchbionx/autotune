# probe_sdk_write.ps1
#
# Smallest end-to-end SDK write test:
#   1. Read Core Voltage Offset (id=34) -- expect 0 mV.
#   2. Tune(34, -25, false) -- stage a -25 mV undervolt.
#   3. Inspect the TuningResult that came back.
#   4. ApplyChanges(false) -- commit without forced restart.
#   5. Read back -- should now be -25 mV.
#   6. Tune(34, 0, false) + ApplyChanges(false) -- revert to 0.
#   7. Read back -- should be 0 again.
#
# Why this is safe:
#   * We pushed -50 mV through the XTU UI (via the same SDK) in a prior
#     session and the box stayed stable. -25 mV is half of that.
#   * Reverts to 0 mV at the end no matter what. Revert is bracketed in
#     a finally{} so even an exception leaves the chip in stock state.
#   * If anything looks wrong after step 4, we can also abort with
#     DiscardChanges() before ApplyChanges() commits.

$ErrorActionPreference = "Stop"

$sdkDir = "C:\Program Files\Intel\Intel(R) Extreme Tuning Utility\Client"
if (-not (Test-Path $sdkDir)) {
    $sdkDir = "C:\Program Files (x86)\Intel\Intel(R) Extreme Tuning Utility\Client"
}
[AppDomain]::CurrentDomain.add_AssemblyResolve({
    param($s, $a)
    $name = ($a.Name -split ',')[0]
    $candidate = Join-Path $sdkDir "$name.dll"
    if (Test-Path $candidate) { return [Reflection.Assembly]::LoadFrom($candidate) }
    return $null
})
$sdk = [Reflection.Assembly]::LoadFrom((Join-Path $sdkDir "IntelOverclockingSDK.dll"))
$tunType = $sdk.GetType("Intel.Overclocking.SDK.Tuning.TuningLibrary")
$tun = $tunType.GetProperty("Instance",
    [Reflection.BindingFlags]"Public,NonPublic,Static").GetValue($null)
$tun.Initialize() | Out-Null

$ID_CVO = [uint32]34

function Read-CVO {
    $c = $tun.GetControl($ID_CVO)
    return [PSCustomObject]@{
        Active   = $c.ActiveValue
        Boot     = $c.BootValue
        Proposed = $c.ProposedValue
        Default  = $c.DefaultValue
        Units    = $c.Units
    }
}

# --- Pre-state ---
Write-Host "`n=== Before ===" -ForegroundColor Cyan
Read-CVO | Format-List

# Find the Tune(UInt32, Decimal, Boolean) overload up front so we can also
# inspect its return type to understand what TuningResult looks like.
$tuneM = $tunType.GetMethods() |
    Where-Object {
        $_.Name -eq "Tune" -and
        $_.GetParameters().Count -eq 3 -and
        $_.GetParameters()[0].ParameterType -eq [uint32] -and
        $_.GetParameters()[1].ParameterType -eq [decimal] -and
        $_.GetParameters()[2].ParameterType -eq [bool]
    } | Select-Object -First 1
if (-not $tuneM) { throw "Could not find Tune(UInt32, Decimal, Boolean) overload" }
$trType = $tuneM.ReturnType
Write-Host "TuningResult type: $($trType.FullName) (IsEnum=$($trType.IsEnum))"
if ($trType.IsEnum) {
    Write-Host "  Enum values: $([Enum]::GetNames($trType) -join ', ')"
} else {
    $trType.GetProperties() | ForEach-Object {
        Write-Host "  prop: $($_.PropertyType.Name) $($_.Name)"
    }
    $trType.GetFields() | ForEach-Object {
        Write-Host "  field: $($_.FieldType.Name) $($_.Name)"
    }
}

$wrote = $false
try {
    # --- 1. Tune(34, -25, false) ---
    Write-Host "`n=== Tune(34, -25, false) ===" -ForegroundColor Cyan
    $result = $tuneM.Invoke($tun, @([uint32]34, [decimal](-25), [bool]$false))
    Write-Host "Tune returned: $result (type=$($result.GetType().Name))"

    # --- 2. ApplyChanges(false) ---
    Write-Host "`n=== ApplyChanges(false) ===" -ForegroundColor Cyan
    $applyResult = $tun.ApplyChanges([bool]$false)
    $wrote = $true
    Write-Host "ApplyChanges returned: $applyResult"

    Start-Sleep -Milliseconds 500

    # --- 3. Read-back ---
    Write-Host "`n=== After write ===" -ForegroundColor Cyan
    $after = Read-CVO
    $after | Format-List

    if ([decimal]$after.Active -eq [decimal](-25)) {
        Write-Host "*** SDK WRITE VERIFIED: Core Voltage Offset is now -25 mV" -ForegroundColor Green
    } else {
        Write-Host "!!! Read-back mismatch: expected -25, got $($after.Active)" -ForegroundColor Yellow
    }
}
finally {
    # --- 4. Always revert to 0 mV ---
    Write-Host "`n=== Revert to 0 mV ===" -ForegroundColor Cyan
    try {
        if ($wrote) {
            $tuneM.Invoke($tun, @([uint32]34, [decimal]0, [bool]$false)) | Out-Null
            $tun.ApplyChanges([bool]$false) | Out-Null
            Start-Sleep -Milliseconds 500
        } else {
            # We never committed -- just discard staged changes
            $tun.DiscardChanges() | Out-Null
        }
    } catch {
        Write-Host "!! Revert path threw: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "!! Manually verify with XTU UI that Core Voltage Offset is 0 mV." -ForegroundColor Red
    }
    Write-Host "`n=== Final state ===" -ForegroundColor Cyan
    Read-CVO | Format-List
}

Write-Host "`nDone." -ForegroundColor Green
