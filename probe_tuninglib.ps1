# probe_tuninglib.ps1
#
# Goal: figure out how Intel.Overclocking.SDK.Tuning.TuningLibrary is supposed
# to be instantiated. CreateInstance() with no args fails because it has no
# parameterless ctor -- so we need to discover its real construction path.
#
# This script:
#   1. Loads the SDK with the assembly resolver (so XtuCommon.dll loads).
#   2. Dumps every public constructor on TuningLibrary with parameter types.
#   3. Looks for static factory methods (Create / GetInstance / Default ...).
#   4. Scans IntelOverclockingLibrary for any property/method returning
#      ITuningLibrary or TuningLibrary (i.e. it owns the instance).
#   5. Scans the whole SDK assembly for any container / factory / builder
#      types that look like they wire the dependency graph.
#   6. Tries to instantiate via the most likely paths and reports what works.
#
# Run elevated. No driver work happens here -- this is pure reflection.

$ErrorActionPreference = "Stop"

$sdkDir = "C:\Program Files\Intel\Intel(R) Extreme Tuning Utility\Client"
if (-not (Test-Path $sdkDir)) {
    $sdkDir = "C:\Program Files (x86)\Intel\Intel(R) Extreme Tuning Utility\Client"
}
if (-not (Test-Path $sdkDir)) {
    Write-Host "Could not find XTU Client dir; edit `$sdkDir at top of script." -ForegroundColor Red
    exit 1
}
Write-Host "SDK dir: $sdkDir"

# Resolver so XtuCommon and friends load when SDK references them.
[AppDomain]::CurrentDomain.add_AssemblyResolve({
    param($s, $a)
    $name = ($a.Name -split ',')[0]
    $candidate = Join-Path $sdkDir "$name.dll"
    if (Test-Path $candidate) { return [Reflection.Assembly]::LoadFrom($candidate) }
    return $null
})

$sdkPath = Join-Path $sdkDir "IntelOverclockingSDK.dll"
$sdk = [Reflection.Assembly]::LoadFrom($sdkPath)
Write-Host "Loaded: $($sdk.FullName)" -ForegroundColor Green

# --- 1. TuningLibrary constructors ---
Write-Host "`n=== TuningLibrary constructors ===" -ForegroundColor Cyan
$tunType = $sdk.GetType("Intel.Overclocking.SDK.Tuning.TuningLibrary")
if (-not $tunType) {
    Write-Host "TuningLibrary type not found!" -ForegroundColor Red
    exit 1
}
$ctors = $tunType.GetConstructors([Reflection.BindingFlags]"Public,NonPublic,Instance")
foreach ($c in $ctors) {
    $vis = if ($c.IsPublic) {"public"} elseif ($c.IsAssembly) {"internal"} else {"private"}
    $params = ($c.GetParameters() | ForEach-Object {
        "$($_.ParameterType.FullName) $($_.Name)"
    }) -join ", "
    Write-Host "  [$vis] ($params)"
}

# --- 2. Static factories on TuningLibrary itself ---
Write-Host "`n=== TuningLibrary static methods ===" -ForegroundColor Cyan
$tunType.GetMethods([Reflection.BindingFlags]"Public,NonPublic,Static") |
    ForEach-Object {
        $params = ($_.GetParameters() | ForEach-Object {
            "$($_.ParameterType.Name) $($_.Name)"
        }) -join ", "
        Write-Host "  $($_.ReturnType.Name) $($_.Name)($params)"
    }

# --- 3. IntelOverclockingLibrary -- does it own/provide a tuning instance? ---
Write-Host "`n=== IntelOverclockingLibrary public surface ===" -ForegroundColor Cyan
$libType = $sdk.GetType("Intel.Overclocking.SDK.IntelOverclockingLibrary")
if ($libType) {
    Write-Host "-- Properties --"
    $libType.GetProperties([Reflection.BindingFlags]"Public,Instance,Static") |
        ForEach-Object {
            Write-Host "  $($_.PropertyType.Name) $($_.Name) {get=$($_.CanRead);set=$($_.CanWrite)}"
        }
    Write-Host "-- Methods (filtered: Tuning/Tune/Connect/Get) --"
    $libType.GetMethods([Reflection.BindingFlags]"Public,Instance,Static") |
        Where-Object {
            $_.Name -match "Tun|Connect|Create|Get|Resolve|Service" -and
            -not ($_.Name -match "^(get_|set_)")
        } |
        ForEach-Object {
            $params = ($_.GetParameters() | ForEach-Object {
                "$($_.ParameterType.Name) $($_.Name)"
            }) -join ", "
            Write-Host "  $($_.ReturnType.Name) $($_.Name)($params)"
        }
}

# --- 4. Any container / factory / builder types in the SDK? ---
Write-Host "`n=== SDK types matching Container|Factory|Builder|Resolver|Provider ===" -ForegroundColor Cyan
$sdk.GetTypes() | Where-Object {
    $_.Name -match "Container|Factory|Builder|Resolver|Provider|Service|Bootstrap"
} | ForEach-Object {
    Write-Host "  $($_.FullName)"
}

# --- 5. ITuningLibrary -- where is it produced? ---
Write-Host "`n=== Methods anywhere in SDK returning ITuningLibrary or TuningLibrary ===" -ForegroundColor Cyan
$iTun = $sdk.GetType("Intel.Overclocking.SDK.Tuning.ITuningLibrary")
foreach ($t in $sdk.GetTypes()) {
    foreach ($m in $t.GetMethods([Reflection.BindingFlags]"Public,NonPublic,Instance,Static")) {
        if ($m.ReturnType -eq $iTun -or $m.ReturnType -eq $tunType) {
            $params = ($m.GetParameters() | ForEach-Object {
                "$($_.ParameterType.Name) $($_.Name)"
            }) -join ", "
            $vis = if ($m.IsPublic) {"public"} elseif ($m.IsAssembly) {"internal"} else {"private"}
            $static = if ($m.IsStatic) {"static "} else {""}
            Write-Host "  [$vis] $static$($t.FullName).$($m.Name)($params) -> $($m.ReturnType.Name)"
        }
    }
    foreach ($p in $t.GetProperties([Reflection.BindingFlags]"Public,NonPublic,Instance,Static")) {
        if ($p.PropertyType -eq $iTun -or $p.PropertyType -eq $tunType) {
            Write-Host "  [property] $($t.FullName).$($p.Name) -> $($p.PropertyType.Name)"
        }
    }
}

# --- 6. Try the obvious paths ---
Write-Host "`n=== Attempting instantiation paths ===" -ForegroundColor Cyan

# 6a. IntelOverclockingLibrary parameterless ctor, then look for Tuning prop/method
try {
    $libCtor = $libType.GetConstructor([Type]::EmptyTypes)
    if ($libCtor) {
        Write-Host "Path A: new IntelOverclockingLibrary()..."
        $lib = $libCtor.Invoke($null)
        Write-Host "  OK -- got $($lib.GetType().FullName)" -ForegroundColor Green
        # Look for a Tuning property
        $tunProp = $libType.GetProperty("Tuning")
        if (-not $tunProp) { $tunProp = $libType.GetProperty("TuningLibrary") }
        if (-not $tunProp) { $tunProp = $libType.GetProperty("Tune") }
        if ($tunProp) {
            Write-Host "  Has property '$($tunProp.Name)' -> $($tunProp.PropertyType.Name)"
            try {
                $tun = $tunProp.GetValue($lib)
                Write-Host "  Got instance: $($tun.GetType().FullName)" -ForegroundColor Green
                $script:tuningInstance = $tun
            } catch {
                Write-Host "  Property getter threw: $($_.Exception.InnerException.Message)" -ForegroundColor Yellow
            }
        }
    } else {
        Write-Host "IntelOverclockingLibrary has no parameterless ctor either." -ForegroundColor Yellow
    }
} catch {
    Write-Host "Path A failed: $($_.Exception.Message)" -ForegroundColor Yellow
}

# 6b. Static InitializeCheck on IntelOverclockingLibrary -- after init, look for static accessor
Write-Host "`nStatic members on IntelOverclockingLibrary:"
$libType.GetMembers([Reflection.BindingFlags]"Public,NonPublic,Static") |
    Where-Object { $_.MemberType -in @("Method","Property","Field") } |
    Where-Object { $_.Name -notmatch "^(get_|set_|add_|remove_)" } |
    ForEach-Object {
        Write-Host "  [$($_.MemberType)] $($_.Name)"
    }

if ($script:tuningInstance) {
    Write-Host "`n*** Got a tuning instance via $($tunProp.Name). Type: $($script:tuningInstance.GetType().FullName)" -ForegroundColor Green
    Write-Host "Methods on it:"
    $script:tuningInstance.GetType().GetMethods([Reflection.BindingFlags]"Public,Instance") |
        Where-Object { $_.Name -notmatch "^(get_|set_)" -and $_.DeclaringType -ne [object] } |
        ForEach-Object {
            $params = ($_.GetParameters() | ForEach-Object { "$($_.ParameterType.Name) $($_.Name)" }) -join ", "
            Write-Host "  $($_.ReturnType.Name) $($_.Name)($params)"
        }
}

Write-Host "`nDone." -ForegroundColor Green
