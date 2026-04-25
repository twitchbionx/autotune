# probe_tuninglib2.ps1
#
# Now that we know the entry points, this script:
#   1. Tries TuningLibrary.Instance + Initialize() (singleton path).
#   2. Tries IntelOverclockingLibrary.TuningLib (property path).
#   3. For whichever works, enumerates the *interface surface* of
#      ITuningLibrary so we know exactly which methods to call.
#   4. Attempts to read Core Voltage Offset (ControlId 34) end-to-end.
#
# READ-ONLY. No writes, no ApplyChanges. Safe to run.

$ErrorActionPreference = "Stop"

$sdkDir = "C:\Program Files\Intel\Intel(R) Extreme Tuning Utility\Client"
if (-not (Test-Path $sdkDir)) {
    $sdkDir = "C:\Program Files (x86)\Intel\Intel(R) Extreme Tuning Utility\Client"
}
Write-Host "SDK dir: $sdkDir"

[AppDomain]::CurrentDomain.add_AssemblyResolve({
    param($s, $a)
    $name = ($a.Name -split ',')[0]
    $candidate = Join-Path $sdkDir "$name.dll"
    if (Test-Path $candidate) { return [Reflection.Assembly]::LoadFrom($candidate) }
    return $null
})

$sdk = [Reflection.Assembly]::LoadFrom((Join-Path $sdkDir "IntelOverclockingSDK.dll"))
$libType = $sdk.GetType("Intel.Overclocking.SDK.IntelOverclockingLibrary")
$tunType = $sdk.GetType("Intel.Overclocking.SDK.Tuning.TuningLibrary")
$iTun    = $sdk.GetType("Intel.Overclocking.SDK.Tuning.ITuningLibrary")

# --- Path A: singleton + Initialize ---
Write-Host "`n=== Path A: TuningLibrary.Instance + Initialize() ===" -ForegroundColor Cyan
$tunA = $null
try {
    $instProp = $tunType.GetProperty("Instance",
        [Reflection.BindingFlags]"Public,NonPublic,Static")
    $tunA = $instProp.GetValue($null)
    Write-Host "Instance: $($tunA.GetType().FullName)" -ForegroundColor Green

    $initMethod = $tunType.GetMethod("Initialize",
        [Reflection.BindingFlags]"Public,NonPublic,Instance,Static")
    Write-Host "Calling Initialize()..."
    $initMethod.Invoke($tunA, $null)
    Write-Host "Initialize() returned." -ForegroundColor Green
} catch {
    Write-Host "Path A failed: $($_.Exception.Message)" -ForegroundColor Yellow
    if ($_.Exception.InnerException) {
        Write-Host "  inner: $($_.Exception.InnerException.Message)" -ForegroundColor Yellow
    }
}

# --- Path B: IntelOverclockingLibrary.TuningLib ---
Write-Host "`n=== Path B: IntelOverclockingLibrary.TuningLib ===" -ForegroundColor Cyan
$tunB = $null
try {
    $lib = $libType.GetConstructor([Type]::EmptyTypes).Invoke($null)
    $tunLibProp = $libType.GetProperty("TuningLib")
    $tunB = $tunLibProp.GetValue($lib)
    if ($null -eq $tunB) {
        Write-Host "TuningLib property returned null" -ForegroundColor Yellow
    } else {
        Write-Host "Got: $($tunB.GetType().FullName)" -ForegroundColor Green
    }
} catch {
    Write-Host "Path B failed: $($_.Exception.Message)" -ForegroundColor Yellow
    if ($_.Exception.InnerException) {
        Write-Host "  inner: $($_.Exception.InnerException.Message)" -ForegroundColor Yellow
    }
}

# Pick whichever worked
$tun = if ($tunA) { $tunA } elseif ($tunB) { $tunB } else { $null }
if (-not $tun) {
    Write-Host "`nNo tuning instance obtained -- cannot continue." -ForegroundColor Red
    exit 1
}
Write-Host "`nUsing instance from $(if ($tunA) {'Path A (singleton)'} else {'Path B (lib property)'})." -ForegroundColor Green

# --- Enumerate ITuningLibrary interface ---
Write-Host "`n=== ITuningLibrary methods ===" -ForegroundColor Cyan
$iTun.GetMethods() | ForEach-Object {
    $params = ($_.GetParameters() | ForEach-Object {
        "$($_.ParameterType.Name) $($_.Name)"
    }) -join ", "
    Write-Host "  $($_.ReturnType.Name) $($_.Name)($params)"
}
Write-Host "`n=== ITuningLibrary properties ===" -ForegroundColor Cyan
$iTun.GetProperties() | ForEach-Object {
    Write-Host "  $($_.PropertyType.Name) $($_.Name)"
}

# --- Enumerate concrete TuningLibrary instance methods (more than the interface) ---
Write-Host "`n=== TuningLibrary instance methods (filtered) ===" -ForegroundColor Cyan
$tunType.GetMethods([Reflection.BindingFlags]"Public,Instance") |
    Where-Object {
        $_.DeclaringType -ne [object] -and
        $_.Name -notmatch "^(get_|set_|add_|remove_)"
    } |
    ForEach-Object {
        $params = ($_.GetParameters() | ForEach-Object {
            "$($_.ParameterType.Name) $($_.Name)"
        }) -join ", "
        Write-Host "  $($_.ReturnType.Name) $($_.Name)($params)"
    }

# --- Try to read Core Voltage Offset (ID 34) ---
Write-Host "`n=== Read attempt: ControlId 34 (Core Voltage Offset) ===" -ForegroundColor Cyan
$readMethods = @("GetTuningControlByID", "GetTuningControlById",
                 "GetControlByID", "GetControl", "GetTuningControl",
                 "GetCurrentValue", "ReadValue")
foreach ($mname in $readMethods) {
    $m = $tunType.GetMethod($mname, [Reflection.BindingFlags]"Public,Instance")
    if ($null -eq $m) { continue }
    $params = $m.GetParameters()
    Write-Host "Trying $mname($($params | ForEach-Object {$_.ParameterType.Name}) -join ',')..."
    try {
        # Most likely it takes a single uint or int ID
        if ($params.Count -eq 1) {
            $arg = if ($params[0].ParameterType -eq [uint32]) { [uint32]34 } else { 34 }
            $result = $m.Invoke($tun, @($arg))
            Write-Host "  -> $($result.GetType().FullName)" -ForegroundColor Green
            # Dump the control's properties
            $result.GetType().GetProperties() | ForEach-Object {
                try {
                    $v = $_.GetValue($result)
                    Write-Host "    $($_.Name) = $v"
                } catch {
                    Write-Host "    $($_.Name) = <getter threw>"
                }
            }
            break
        }
    } catch {
        Write-Host "  $mname threw: $($_.Exception.InnerException.Message)" -ForegroundColor Yellow
    }
}

Write-Host "`nDone." -ForegroundColor Green
