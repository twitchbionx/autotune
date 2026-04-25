# probe_tuninglib4.ps1
#
# We have 197 controls but my probe used wrong property names. This script:
#   1. Dumps the *full property schema* of the first control object so we
#      know the real field names (Id? ControlIdNumber? ControlIdValue?).
#   2. Re-dumps all 197 controls using the correct getters, focused on the
#      knobs we actually need: Core Voltage Offset, Performance Core Ratio,
#      Processor Cache Ratio, Core Voltage, AVX2 Ratio Offset, ICCMax,
#      Turbo Boost Power Max / Short Power Max, Reference Clock.
#   3. Pulls the active profile's TuningItems (id + value list) since that's
#      the cleanest source of "current values".
#   4. Once we have the real ID for Core Voltage Offset, calls IsControlTunable
#      and GetTuningControlByID against it.
#
# READ-ONLY.

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

$controls = $tun.GetAvailableControls()
Write-Host "Got $($controls.Count) controls" -ForegroundColor Green

# --- 1. Real property schema of the first control ---
Write-Host "`n=== Property schema of XtuTuningControl ===" -ForegroundColor Cyan
$first = $controls[0]
$ctrlType = $first.GetType()
Write-Host "Type: $($ctrlType.FullName)"
$ctrlType.GetProperties() | ForEach-Object {
    $val = "<err>"
    try { $val = $_.GetValue($first) } catch {}
    Write-Host ("  {0,-30} : {1,-25} = {2}" -f $_.Name, $_.PropertyType.Name, $val)
}

# --- 2. Identify the right property names dynamically ---
$idProp = $ctrlType.GetProperties() | Where-Object {
    $_.Name -match "^(Id|ID|ControlId|ControlID|nControlId|ControlIdNumber)$"
} | Select-Object -First 1
$nameProp = $ctrlType.GetProperty("ControlName")
$valProp = $ctrlType.GetProperties() | Where-Object {
    $_.Name -match "^(CurrentValue|Value|ActualValue)$"
} | Select-Object -First 1
$minProp = $ctrlType.GetProperties() | Where-Object {
    $_.Name -match "^(MinValue|Min|MinimumValue)$"
} | Select-Object -First 1
$maxProp = $ctrlType.GetProperties() | Where-Object {
    $_.Name -match "^(MaxValue|Max|MaximumValue)$"
} | Select-Object -First 1
$tunableProp = $ctrlType.GetProperties() | Where-Object {
    $_.Name -match "Tunable" -and $_.Name -notmatch "RealTime"
} | Select-Object -First 1

Write-Host "`nResolved property names:"
Write-Host "  id      = $($idProp.Name)"
Write-Host "  name    = $($nameProp.Name)"
Write-Host "  cur     = $($valProp.Name)"
Write-Host "  min     = $($minProp.Name)"
Write-Host "  max     = $($maxProp.Name)"
Write-Host "  tunable = $($tunableProp.Name)"

# --- 3. Find knobs we care about by name fragment ---
$wantedNames = @(
    "Core Voltage Offset",          # FIVR core offset (the one we hit -50 mV via)
    "Performance Core Ratio",       # all-P-core ratio
    "Processor Cache Ratio",        # ring/cache
    "Core Voltage",                 # Vcore override
    "Core Voltage Mode",
    "AVX2 Ratio Offset",
    "AVX2 Voltage Guardband Scale Factor",
    "Processor Core IccMax",
    "Turbo Boost Power Max",
    "Turbo Boost Short Power Max",
    "Turbo Boost Short Power Max Enable",
    "Turbo Boost Power Time Window",
    "Reference Clock",
    "Overclocking Lock",
    "Max Turbo Boost CPU Speed",
    "1 Active Performance Core",
    "2 Active Performance Cores",
    "3 Active Performance Cores",
    "4 Active Performance Cores",
    "5 Active Performance Cores",
    "6 Active Performance Cores",
    "7 Active Performance Cores",
    "8 Active Performance Cores"
)

Write-Host "`n=== Wanted knobs (exact-name match) ===" -ForegroundColor Cyan
$wantedHits = @{}
foreach ($w in $wantedNames) {
    $hit = $controls | Where-Object {
        $nameProp.GetValue($_) -eq $w
    } | Select-Object -First 1
    if ($hit) {
        $id = $idProp.GetValue($hit)
        $cur = $valProp.GetValue($hit)
        $min = $minProp.GetValue($hit)
        $max = $maxProp.GetValue($hit)
        $tn = $tunableProp.GetValue($hit)
        $wantedHits[$w] = $id
        Write-Host ("  {0,-45} id={1,-12} cur={2,-10} min={3,-10} max={4,-10} tunable={5}" -f `
            $w, $id, $cur, $min, $max, $tn)
    } else {
        Write-Host ("  {0,-45} <not present>" -f $w) -ForegroundColor DarkGray
    }
}

# --- 4. Targeted: Core Voltage Offset ---
$cvId = $wantedHits["Core Voltage Offset"]
if ($cvId) {
    Write-Host "`n=== Targeted reads on Core Voltage Offset (id=$cvId) ===" -ForegroundColor Cyan
    try {
        Write-Host "IsControlTunable($cvId) = $($tun.IsControlTunable([uint32]$cvId))"
    } catch {
        Write-Host "IsControlTunable threw: $($_.Exception.InnerException.Message)" -ForegroundColor Yellow
    }
    try {
        Write-Host "GetTuningControlByID($cvId) = $($tun.GetTuningControlByID([uint32]$cvId))"
    } catch {
        Write-Host "GetTuningControlByID threw: $($_.Exception.InnerException.Message)" -ForegroundColor Yellow
    }
    try {
        $clientCtrl = $tun.GetControl([uint32]$cvId)
        Write-Host "GetControl($cvId) -> $($clientCtrl.GetType().FullName)"
        $clientCtrl.GetType().GetProperties() | ForEach-Object {
            try {
                $v = $_.GetValue($clientCtrl)
                Write-Host "  $($_.Name) = $v"
            } catch {}
        }
    } catch {
        Write-Host "GetControl threw: $($_.Exception.InnerException.Message)" -ForegroundColor Yellow
    }
}

# --- 5. Active profile's TuningItems (IDs + current values) ---
Write-Host "`n=== Active TuningProfile items (id -> value) ===" -ForegroundColor Cyan
$prof = $tun.GetActiveTuningProfile()
$itemsProp = $prof.GetType().GetProperty("ProposedValues")
$items = $itemsProp.GetValue($prof)
if ($items) {
    $first = $items | Select-Object -First 1
    Write-Host "TuningItem schema:"
    $first.GetType().GetProperties() | ForEach-Object {
        $val = "<err>"
        try { $val = $_.GetValue($first) } catch {}
        Write-Host ("  {0,-25} : {1,-20} = {2}" -f $_.Name, $_.PropertyType.Name, $val)
    }
}

Write-Host "`nDone." -ForegroundColor Green
