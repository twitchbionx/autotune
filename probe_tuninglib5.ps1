# probe_tuninglib5.ps1
#
# Schema confirmed: ClientTuningControl has Id / Name / ActiveValue /
# BootValue / ProposedValue / DefaultValue / SupportedValues / ReadOnly /
# Units / ControlType. This script:
#   1. Builds a name->id map for every available control.
#   2. Reports the IDs we'll wire into the Python wrapper for the OC knobs.
#   3. Performs a real read on Core Voltage Offset using its real ID.
#   4. Dumps a sample of the active profile's TuningItem entries (id+value).
#
# READ-ONLY. Safe.

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

# Build id-by-name map. Names we care about for the auto-overclocker:
$wantedNames = @(
    # Voltage knobs
    "Core Voltage Offset",
    "Core Voltage",
    "Core Voltage Mode",
    "Processor Cache Voltage Offset",
    "Processor Cache Voltage",
    "System Agent Voltage Offset",
    # Ratio knobs
    "Performance Core Ratio",
    "Processor Cache Ratio",
    "Efficient Core Ratio",
    "Max Turbo Boost CPU Speed",
    # Active-count rows (for per-count tuning)
    "1 Active Performance Core",
    "2 Active Performance Cores",
    "3 Active Performance Cores",
    "4 Active Performance Cores",
    "5 Active Performance Cores",
    "6 Active Performance Cores",
    "7 Active Performance Cores",
    "8 Active Performance Cores",
    # AVX
    "AVX2 Ratio Offset",
    "AVX2 Voltage Guardband Scale Factor",
    # Power / current
    "Processor Core IccMax",
    "Turbo Boost Power Max",
    "Turbo Boost Short Power Max",
    "Turbo Boost Short Power Max Enable",
    "Turbo Boost Power Time Window",
    # Misc
    "Reference Clock",
    "Overclocking Lock",
    "Intel® Turbo Boost Technology",
    "Enhanced Intel® SpeedStep Technology"
)

Write-Host "`n=== Wanted knobs (real IDs and live values) ===" -ForegroundColor Cyan
$idMap = @{}
foreach ($w in $wantedNames) {
    $hit = $controls | Where-Object { $_.Name -eq $w } | Select-Object -First 1
    if ($hit) {
        $idMap[$w] = [uint32]$hit.Id
        $tunable = -not $hit.ReadOnly
        $line = "  {0,-45} id={1,-10} cur={2,-10} default={3,-10} units={4,-5} tunable={5}" -f `
            $w, $hit.Id, $hit.ActiveValue, $hit.DefaultValue, $hit.Units, $tunable
        Write-Host $line
    } else {
        Write-Host ("  {0,-45} <not present>" -f $w) -ForegroundColor DarkGray
    }
}

# Targeted: Core Voltage Offset
$cvoId = $idMap["Core Voltage Offset"]
if ($cvoId) {
    Write-Host "`n=== Targeted reads on Core Voltage Offset (id=$cvoId) ===" -ForegroundColor Cyan
    Write-Host "IsControlTunable($cvoId)        = $($tun.IsControlTunable([uint32]$cvoId))"
    Write-Host "IsControlTunableRealTime($cvoId) = $($tun.IsControlTunableRealTime([uint32]$cvoId))"
    try {
        $v = $tun.GetTuningControlByID([uint32]$cvoId)
        Write-Host "GetTuningControlByID($cvoId)    = $v" -ForegroundColor Green
    } catch {
        Write-Host "GetTuningControlByID threw: $($_.Exception.InnerException.Message)" -ForegroundColor Yellow
    }
    try {
        $cc = $tun.GetControl([uint32]$cvoId)
        Write-Host "GetControl($cvoId) -> $($cc.GetType().FullName)"
        $cc.GetType().GetProperties() |
            Where-Object { $_.Name -in @("Id","Name","ActiveValue","BootValue","DefaultValue","ProposedValue","Units","ReadOnly","ControlType","SupportedValues") } |
            ForEach-Object {
                try {
                    $v = $_.GetValue($cc)
                    if ($_.Name -eq "SupportedValues" -and $v) { $v = "[$($v.Count) entries]" }
                    Write-Host "  $($_.Name) = $v"
                } catch {}
            }
    } catch {
        Write-Host "GetControl threw: $($_.Exception.InnerException.Message)" -ForegroundColor Yellow
    }
}

# Active profile TuningItem schema
Write-Host "`n=== Active TuningProfile -- first 5 items ===" -ForegroundColor Cyan
$prof = $tun.GetActiveTuningProfile()
$itemsProp = $prof.GetType().GetProperty("ProposedValues")
$items = $itemsProp.GetValue($prof)
$first = $items | Select-Object -First 1
if ($first) {
    Write-Host "TuningItem schema:"
    $first.GetType().GetProperties() | ForEach-Object {
        $val = "<err>"
        try { $val = $_.GetValue($first) } catch {}
        Write-Host ("  {0,-25} : {1,-20} = {2}" -f $_.Name, $_.PropertyType.Name, $val)
    }
    Write-Host "`nFirst 5 items:"
    $items | Select-Object -First 5 | ForEach-Object {
        $idP = $_.GetType().GetProperty("Id")
        $vP  = $_.GetType().GetProperty("Value")
        if (-not $vP) { $vP = $_.GetType().GetProperty("ProposedValue") }
        $idV = if ($idP) { $idP.GetValue($_) } else { "?" }
        $vV  = if ($vP)  { $vP.GetValue($_)  } else { "?" }
        Write-Host "  id=$idV value=$vV"
    }
}

# Emit the resolved IDs as a Python dict literal for easy copy-paste
Write-Host "`n=== Python dict of resolved IDs (paste into autotune/sdk_ids.py) ===" -ForegroundColor Cyan
Write-Host "XTU_CONTROL_IDS = {"
foreach ($k in $idMap.Keys) {
    $py = '"' + $k.Replace('"','\"') + '": ' + $idMap[$k] + ','
    Write-Host "    $py"
}
Write-Host "}"

Write-Host "`nDone." -ForegroundColor Green
