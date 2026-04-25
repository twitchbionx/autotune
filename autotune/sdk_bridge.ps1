# sdk_bridge.ps1
#
# Long-running PowerShell bridge to the Intel XTU SDK. Used by xtu_sdk.py
# instead of pythonnet, because pythonnet has no prebuilt wheels for
# Python 3.13+ and source-compilation goes through NuGet (flaky).
#
# Protocol: one JSON request per line on stdin, one JSON response per
# line on stdout. Runs forever until {"op":"quit"} or EOF.
#
# Operations:
#   {"op":"ping"}                     -> {"ok":true,"pong":true}
#   {"op":"get_control","id":34}      -> {"ok":true,id,name,active,boot,
#                                          default,proposed,units,
#                                          control_type,read_only}
#   {"op":"is_tunable","id":34}       -> {"ok":true,"tunable":true|false}
#   {"op":"tune","id":34,"value":-25,"requires_reboot":false}
#                                      -> {"ok":true,"success":true,
#                                          "general_code":"Success"}
#   {"op":"apply","force_restart":false}
#                                      -> {"ok":true,"success":true,
#                                          "general_code":"Success"}
#   {"op":"discard"}                  -> {"ok":true}
#   {"op":"quit"}                     -> {"ok":true} ; then exits

$ErrorActionPreference = "Stop"

# Force UTF-8 on both stdin and stdout so JSON round-trips cleanly between
# PowerShell and Python. PS's default is the system code page which Python
# reads as garbage when subprocess is opened with encoding="utf-8".
try {
    [Console]::InputEncoding  = [System.Text.UTF8Encoding]::new($false)
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch {}

# Optional crash log -- env var set by xtu_sdk.py. Lines are appended.
$logPath = $env:AUTOTUNE_BRIDGE_LOG
function Write-Log($msg) {
    if ($logPath) {
        try {
            $ts = (Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff")
            Add-Content -Path $logPath -Value "[$ts] $msg" -Encoding UTF8
        } catch {}
    }
}
Write-Log "bridge starting; pid=$PID"

# --- 1. Locate the XTU SDK (override via env var if needed) ---

$sdkDir = $env:AUTOTUNE_XTU_SDK_DIR
if (-not $sdkDir) {
    $candidates = @(
        "C:\Program Files\Intel\Intel(R) Extreme Tuning Utility\Client",
        "C:\Program Files (x86)\Intel\Intel(R) Extreme Tuning Utility\Client"
    )
    foreach ($c in $candidates) {
        if (Test-Path (Join-Path $c "IntelOverclockingSDK.dll")) {
            $sdkDir = $c
            break
        }
    }
}
if (-not $sdkDir) {
    Write-Log "SDK not found"
    $err = @{ ok = $false; error = "XTU SDK not found in standard locations." }
    [Console]::Out.WriteLine(($err | ConvertTo-Json -Compress))
    [Console]::Out.Flush()
    exit 1
}
Write-Log "sdkDir=$sdkDir"

# --- 2. Load SDK assemblies with a resolver for XtuCommon.dll etc. ---

[AppDomain]::CurrentDomain.add_AssemblyResolve({
    param($s, $a)
    $name = ($a.Name -split ',')[0]
    $candidate = Join-Path $sdkDir "$name.dll"
    if (Test-Path $candidate) { return [Reflection.Assembly]::LoadFrom($candidate) }
    return $null
})

try {
    Write-Log "loading SDK assembly..."
    $sdk = [Reflection.Assembly]::LoadFrom((Join-Path $sdkDir "IntelOverclockingSDK.dll"))
    Write-Log "loaded: $($sdk.FullName)"
    $tunType = $sdk.GetType("Intel.Overclocking.SDK.Tuning.TuningLibrary")
    Write-Log "got TuningLibrary type"
    $tun = $tunType.GetProperty("Instance",
        [Reflection.BindingFlags]"Public,NonPublic,Static").GetValue($null)
    Write-Log "got TuningLibrary singleton"
    $tun.Initialize() | Out-Null
    Write-Log "Initialize() returned"
    if (-not $tun.InitializeCheck()) {
        throw "InitializeCheck() returned False -- SDK could not connect."
    }
    # Cache the Tune(uint, decimal, bool) method-info; PS picks the wrong
    # overload otherwise (decimal vs uint signatures collide).
    $tuneM = $tunType.GetMethods() |
        Where-Object {
            $_.Name -eq "Tune" -and
            $_.GetParameters().Count -eq 3 -and
            $_.GetParameters()[0].ParameterType -eq [uint32] -and
            $_.GetParameters()[1].ParameterType -eq [decimal] -and
            $_.GetParameters()[2].ParameterType -eq [bool]
        } | Select-Object -First 1
    if (-not $tuneM) { throw "Tune(uint,decimal,bool) overload not found" }
    Write-Log "found Tune(uint,decimal,bool)"
} catch {
    $msg = "SDK init failed: $($_.Exception.GetType().Name): $($_.Exception.Message)"
    Write-Log $msg
    Write-Log "stacktrace: $($_.ScriptStackTrace)"
    # Also dump to stderr so Python sees it via its drain thread.
    [Console]::Error.WriteLine($msg)
    [Console]::Error.Flush()
    $err = @{ ok = $false; error = $msg }
    [Console]::Out.WriteLine(($err | ConvertTo-Json -Compress))
    [Console]::Out.Flush()
    exit 1
}

# --- 3. Helpers ---

function Send-Reply($obj) {
    # ConvertTo-Json with -Depth so nested fields don't get truncated.
    $json = $obj | ConvertTo-Json -Compress -Depth 5
    [Console]::Out.WriteLine($json)
    [Console]::Out.Flush()
}

function To-Float($d) {
    if ($null -eq $d) { return 0.0 }
    return [double]$d
}

function Snapshot-Control($cc) {
    return @{
        ok           = $true
        id           = [uint32]$cc.Id
        name         = [string]$cc.Name
        active       = (To-Float $cc.ActiveValue)
        boot         = (To-Float $cc.BootValue)
        default      = (To-Float $cc.DefaultValue)
        proposed     = (To-Float $cc.ProposedValue)
        units        = [string]$cc.Units
        control_type = [string]$cc.ControlType
        read_only    = [bool]$cc.ReadOnly
    }
}

# Signal ready -- Python side reads this before sending requests.
Write-Log "sending ready signal"
Send-Reply @{ ok = $true; ready = $true; sdk_dir = $sdkDir }
Write-Log "ready signal sent; entering loop"

# --- 4. Main loop ---

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }    # EOF
    $line = $line.Trim()
    if (-not $line) { continue }

    try {
        $req = $line | ConvertFrom-Json
    } catch {
        Send-Reply @{ ok = $false; error = "bad json: $($_.Exception.Message)" }
        continue
    }

    try {
        switch ($req.op) {
            "ping" {
                Send-Reply @{ ok = $true; pong = $true }
            }
            "get_control" {
                $cc = $tun.GetControl([uint32]$req.id)
                if ($null -eq $cc) {
                    Send-Reply @{ ok = $false; error = "GetControl returned null" }
                } else {
                    Send-Reply (Snapshot-Control $cc)
                }
            }
            "is_tunable" {
                $t = [bool]$tun.IsControlTunable([uint32]$req.id)
                Send-Reply @{ ok = $true; tunable = $t }
            }
            "tune" {
                # Decimal precision matters: round-trip the JSON number through
                # a string before turning it into a System.Decimal.
                $valStr = ([string]$req.value)
                $dec = [decimal]::Parse($valStr,
                    [System.Globalization.CultureInfo]::InvariantCulture)
                $rb  = [bool]$req.requires_reboot
                $r   = $tuneM.Invoke($tun, @([uint32]$req.id, $dec, $rb))
                $code = [string]$r.GeneralCode
                Send-Reply @{
                    ok = $true
                    success = ($code -eq "Success")
                    general_code = $code
                }
            }
            "apply" {
                $fr = [bool]$req.force_restart
                $r  = $tun.ApplyChanges($fr)
                $code = [string]$r.GeneralCode
                Send-Reply @{
                    ok = $true
                    success = ($code -eq "Success")
                    general_code = $code
                }
            }
            "discard" {
                $tun.DiscardChanges() | Out-Null
                Send-Reply @{ ok = $true }
            }
            "quit" {
                Send-Reply @{ ok = $true; bye = $true }
                exit 0
            }
            default {
                Send-Reply @{ ok = $false; error = "unknown op: $($req.op)" }
            }
        }
    } catch {
        Send-Reply @{
            ok = $false
            error = "$($_.Exception.GetType().Name): $($_.Exception.Message)"
        }
    }
}
