# probe_tuninglib3.ps1
#
# The previous probe got an instance and called Initialize() successfully,
# but GetTuningControlByID(34) bombed with a WCF service exception. The SDK
# IPCs into the "Intel(R) Extreme Tuning Utility Service" -- so either the
# service isn't running, or it hasn't enumerated controls yet, or 34 isn't
# a valid runtime ControlId on this CPU.
#
# This script:
#   1. Reports XTU service status.
#   2. After Initialize(), calls RescanAvailableControls() to force enumeration.
#   3. Lists every control the service actually exposes -- with name + id +
#      tunable flags + current value -- so we know the *real* IDs on THIS box.
#   4. Tries IsControlTunable(34) and GetTuningControlByID(34) once we know
#      whether 34 is even in the list.
#
# READ-ONLY. No Tune/ApplyChanges.

$ErrorActionPreference = "Stop"

# --- 1. XTU service status ---
Write-Host "`n=== XTU Service status ===" -ForegroundColor Cyan
Get-Service | Where-Object { $_.Name -match "Intel" -or $_.DisplayName -match "Extreme Tuning" } |
    ForEach-Object {
        Write-Host "  $($_.Name) [$($_.Status)] - $($_.DisplayName)"
    }

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

Write-Host "`n=== Singleton + Initialize ===" -ForegroundColor Cyan
$tun = $tunType.GetProperty("Instance",
    [Reflection.BindingFlags]"Public,NonPublic,Static").GetValue($null)
$tun.Initialize()
Write-Host "InitializeCheck: $($tun.InitializeCheck())"
Write-Host "GetProcessorFamily: $($tun.GetProcessorFamily())"
Write-Host "IsProcessorCoreTunable: $($tun.IsProcessorCoreTunable())"

# --- 2. Force a fresh control enumeration ---
Write-Host "`n=== RescanAvailableControls() ===" -ForegroundColor Cyan
try {
    $controls = $tun.RescanAvailableControls()
    Write-Host "Got $($controls.Count) controls" -ForegroundColor Green
} catch {
    Write-Host "RescanAvailableControls threw: $($_.Exception.InnerException.Message)" -ForegroundColor Yellow
    Write-Host "Falling back to GetAvailableControls()..."
    $controls = $tun.GetAvailableControls()
    Write-Host "Got $($controls.Count) controls"
}

# --- 3. Dump every control: id, name, tunable flag, current value ---
Write-Host "`n=== All available controls ===" -ForegroundColor Cyan
$controls | Sort-Object { $_.ControlId } | ForEach-Object {
    $c = $_
    # Probe properties we expect: ControlId, ControlName, CurrentValue, MinValue, MaxValue
    $props = @{}
    foreach ($p in $c.GetType().GetProperties()) {
        try { $props[$p.Name] = $p.GetValue($c) } catch { $props[$p.Name] = "<err>" }
    }
    $id   = $props["ControlId"]
    $name = $props["ControlName"]
    if (-not $name) { $name = $props["Name"] }
    $cur  = $props["CurrentValue"]
    $min  = $props["MinValue"]
    $max  = $props["MaxValue"]
    Write-Host ("  id={0,6} cur={1,-10} min={2,-10} max={3,-10}  {4}" -f $id, $cur, $min, $max, $name)
}

# --- 4. If 34 is in the list, try targeted reads ---
Write-Host "`n=== Targeted: ControlId 34 (Core Voltage Offset) ===" -ForegroundColor Cyan
$has34 = $controls | Where-Object {
    $c = $_; $idProp = $c.GetType().GetProperty("ControlId")
    if ($idProp) { $idProp.GetValue($c) -eq 34 } else { $false }
}
if ($has34) {
    Write-Host "Control 34 IS in the available list." -ForegroundColor Green
    try {
        $tunable = $tun.IsControlTunable([uint32]34)
        Write-Host "IsControlTunable(34) = $tunable"
    } catch {
        Write-Host "IsControlTunable(34) threw: $($_.Exception.InnerException.Message)" -ForegroundColor Yellow
    }
    try {
        $v = $tun.GetTuningControlByID([uint32]34)
        Write-Host "GetTuningControlByID(34) = $v" -ForegroundColor Green
    } catch {
        Write-Host "GetTuningControlByID(34) threw: $($_.Exception.InnerException.Message)" -ForegroundColor Yellow
    }
} else {
    Write-Host "Control 34 is NOT in the available list -- the metadata ID 34 is" -ForegroundColor Yellow
    Write-Host "not the runtime ID for FIVR Core Voltage Offset on this CPU. Use" -ForegroundColor Yellow
    Write-Host "the dump above to find the matching name (likely 'Core Voltage Offset')." -ForegroundColor Yellow
}

# --- Bonus: print the active tuning profile ---
Write-Host "`n=== Active tuning profile (per service) ===" -ForegroundColor Cyan
try {
    $prof = $tun.GetActiveTuningProfile()
    if ($null -ne $prof) {
        $prof.GetType().GetProperties() | ForEach-Object {
            try {
                $v = $_.GetValue($prof)
                Write-Host "  $($_.Name) = $v"
            } catch { Write-Host "  $($_.Name) = <err>" }
        }
    }
} catch {
    Write-Host "GetActiveTuningProfile threw: $($_.Exception.InnerException.Message)" -ForegroundColor Yellow
}

Write-Host "`nDone." -ForegroundColor Green
