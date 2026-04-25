"""Sanity check the autotune monitor against live LHM data.

Prints what max_core_temp_c, pkg_power_w, and vcore_v would be right
now -- if any of these are absurd (>110C, >400W, >1.6V) the cap logic
will misfire. Run this before kicking off a stress test.
"""
from autotune.monitor import Monitor

m = Monitor()
print("Polling LHM 3x with 1s gaps...\n")
for i in range(3):
    s = m.sample()
    print(f"sample #{i+1}:")
    print(f"  max_core_temp_c = {s.max_core_temp_c}")
    print(f"  pkg_power_w     = {s.pkg_power_w}")
    print(f"  vcore_v         = {s.vcore_v}")
    print(f"  effective_mhz   = {s.effective_mhz}")
    print()
    if i < 2:
        import time; time.sleep(1)
