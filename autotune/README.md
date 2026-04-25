# autotune

Automatic stability-bounded overclocker for **Intel 12th-gen-or-newer K-SKU
CPUs on Windows**. Finds the highest stable P-core / E-core ratio your chip
can hold inside caps you set for voltage, temperature, and power. Drives
Intel XTU for the actual writes, y-cruncher for the stress load, and the
WHEA event log for silent-error detection.

**This tool can damage your hardware if you set the caps too aggressively.**
Read the whole README. Start with tight caps and relax them if needed.

## What "absolute limits" actually means

There is no software that can "just push to max" because:

- Every individual chip has a different stability ceiling (silicon lottery).
- The ceiling depends on your cooler, VRMs, case airflow, ambient temp.
- Voltage is a trade against long-term degradation -- sustained >1.40 V
  measurably shortens the chip's life on 13th/14th gen, even at low temps.

What this tool does: step the ratio up one notch at a time, find the
minimum stable voltage at each ratio (a voltage shmoo), and stop when it
hits any cap you set -- then run a long final stress test to confirm.

## Prerequisites

1. **Intel K-SKU 12th gen or newer** (e.g. 12600K, 13700K, 14900K).
   Non-K and OEM-locked boards will be detected and the tuner will bail.

2. **Windows 10/11, running as Administrator.**

3. **Intel XTU 7.x installed.**
   <https://www.intel.com/content/www/us/en/download/17881/intel-extreme-tuning-utility-intel-xtu.html>

4. **y-cruncher** (portable zip).
   <http://www.numberworld.org/y-cruncher/> -- unzip to `C:\Tools\y-cruncher`
   or set `ycruncher_path` in config.

5. **LibreHardwareMonitor** running in the background with its web-server
   enabled on port 8085 (Options → Remote Web Server → Run).
   <https://github.com/LibreHardwareMonitor/LibreHardwareMonitor>

6. **Python 3.10+**. No third-party packages required.

7. **BIOS settings.** In most BIOSes, set:
   - `CPU Overclocking Lock` = **Disabled** (lets XTU write ratios)
   - `SVID Behavior` = **Typical Scenario** or **Trained**
   - `Windows Fast Startup` = **Disabled** (so the boot-time watchdog fires)
   - Keep `XMP/EXPO` as configured; don't change memory while CPU-tuning.

8. **Recovery USB ready.** If something truly bad happens (BSOD loop before
   watchdog runs), you'll clear CMOS on the motherboard. Know how before
   you start. Usually: power off, unplug, press the CLR_CMOS button or
   jumper for 10 seconds.

## Install

### Option A: prebuilt `autotune.exe` (recommended)

Download `autotune.exe` from the GitHub release page and drop it
anywhere. The .exe is a single-file bundle -- no Python, no `pip install`,
config template included.

```
:: First-time bootstrap -- installs XTU (via winget), y-cruncher, LHM,
:: and writes a starter config.yaml next to the exe.
:: Prompts for UAC elevation and for XTU's reboot-required flag.
.\autotune.exe setup --write-config config.yaml

:: Sanity-check: every dependency healthy?
.\autotune.exe doctor

:: Install the boot-time crash watchdog
.\autotune.exe watchdog install

:: Run the tuner
.\autotune.exe run --config config.yaml
```

**Note on XTU:** Intel does not permit redistribution of XTU, so the .exe
does not bundle it. `autotune setup` calls `winget install
Intel.IntelExtremeTuningUtility` instead, pulling from Intel's official
package. winget ships with Windows 10 1809+ and Windows 11. If you're on
a stripped-down install without winget, install XTU manually from
<https://www.intel.com/content/www/us/en/download/17881/>, then re-run
`autotune setup --skip-xtu`.

y-cruncher (freeware) and LibreHardwareMonitor (MIT-licensed) are both
downloaded directly from their official sources into `C:\Tools\`.

### Option B: run from source (Python 3.10+)

```
git clone <this repo>
cd OC

:: Install the boot-time watchdog
python -m autotune watchdog install

:: Run
python -m autotune run --config autotune/config.yaml
```

### Option C: build your own exe

```
:: From the OC/ directory (must be on Windows for a real .exe):
.\build.ps1
:: Output: dist\autotune.exe
```

`build.ps1` handles installing PyInstaller and runs the build. For
automated builds, the repo includes a GitHub Actions workflow at
`.github/workflows/build.yml` that produces the exe on every push to
`main` and attaches it to releases on tag pushes.

## Configure

Copy `config.example.yaml` to `config.yaml` and set your caps.
**Start tight.** Sensible starting points:

- `max_vcore_offset_mv`: `50` (you can widen later)
- `max_vcore_v`: `1.40` (hard ceiling; 1.45 is long-term degradation)
- `max_temp_c`: `95` (5 °C off Tjmax)
- `max_power_w`: whatever your cooler can sustain -- if you don't know,
  start at 180W and work up
- `max_pcore_ratio` / `max_ecore_ratio`: your chip's official Max Turbo
  ratios as a hard ceiling

## Run

```
# Dry-run first -- doesn't touch the CPU, just validates paths and config:
python -m autotune run --config config.yaml --dry-run

# For real:
python -m autotune run --config config.yaml
```

Expected runtime: for a 13900K with default 10-min-per-step and 60-min
final, plan for **6-10 hours**. Close everything; let it run overnight.

The tuner prints progress and writes:

- `C:\ProgramData\autotune\autotune.log` -- full log
- `C:\ProgramData\autotune\history.csv` -- every attempt, reason,
  temps/power/Vcore peaks. Open in Excel.
- `C:\ProgramData\autotune\baseline.json` -- your original settings
- `C:\ProgramData\autotune\last_known_good.json` -- best stable so far

## Recovery

**If the machine is hanging, BSODing on boot, or otherwise bricked-feeling:**

1. Power off. Clear CMOS (motherboard manual). Power back on.
2. After successful boot, from an Administrator shell:

   ```
   python -m autotune revert
   ```

   That writes the original `baseline.json` back through XTU. If `revert`
   can't run (because of a corrupted state dir or XTU removal), open
   Intel XTU's GUI and click "Restore Defaults."

**If Windows boots but the OC is unstable and you want out:**

```
python -m autotune revert
```

## How it works (the search)

Pseudocode:

    capture baseline; save as rollback target
    run y-cruncher at stock for `preflight_minutes` -- bail if that fails
    for cluster in (P-core, E-core):
        r = baseline.ratio
        v = baseline.vcore_offset
        while r < user.max_ratio:
            candidate = r + 1
            v_stable = voltage_shmoo(candidate)   # find min-stable Vcore
            if v_stable is None: break            # can't stabilize
            r, v = candidate, v_stable
    run y-cruncher for `final_confirm_minutes` on the result
    if pass: commit; else revert to baseline

Every applied profile is written to `pending.json` with a commit deadline.
If the machine reboots unexpectedly, the boot-time watchdog sees pending
+ expired and reverts to last-known-good BEFORE your desktop session loads.

## Known limitations

- XTU can only write a subset of what BIOS exposes. We don't touch LLC,
  AC/DC loadline, or per-core V/F points -- BIOS is better for those.
- Some OEM prebuilts lock XTU writes even on K CPUs. The tuner will
  detect this at startup and refuse.
- We don't tune memory/IMC or uncore/ring ratio. Those are riskier and
  out of scope.
- We use the same voltage offset for P-cores and E-cores. Different-per-
  cluster voltages require BIOS-level V/F point control.

## Why this isn't a one-liner

A tool that "just OCs to max" without an empirical stability search doesn't
exist, because the stability ceiling is a physical property of each chip
that you can only discover by running tests. What's automated here is
*doing the tests systematically* and *rolling back safely when they fail*.

## License

MIT.
