"""Quick diagnostic: walk the LHM JSON tree and print every °C reading
with its full sensor path. Tells us which sensor is reporting 108°C."""
import json, urllib.request, sys

URL = "http://localhost:8085/data.json"

with urllib.request.urlopen(URL, timeout=3) as r:
    data = json.load(r)

print(f"\n=== TEMPERATURE SENSORS ({URL}) ===\n")
print(f"{'Path':<70} {'Value':<15}")
print("-" * 90)


def walk(node, path=()):
    text = node.get("Text", "")
    val  = node.get("Value", "")
    full = path + ((text,) if text else ())
    if val and "°C" in val:
        try:
            num = float(val.split()[0].replace(",", "."))
        except ValueError:
            num = None
        path_str = " / ".join(full)
        marker = ""
        if num is not None and num >= 95:
            marker = "  <-- THIS WOULD TRIP 95°C CAP"
        print(f"{path_str:<70} {val:<15}{marker}")
    for c in node.get("Children", []):
        walk(c, full)


walk(data)

# Also print non-temp °C-adjacent oddities
print(f"\n=== ALL VOLTAGES, POWERS, CLOCKS (for cross-check) ===\n")


def walk2(node, path=()):
    text = node.get("Text", "")
    val  = node.get("Value", "")
    full = path + ((text,) if text else ())
    if val and (val.endswith(" V") or val.endswith(" W") or "MHz" in val):
        path_str = " / ".join(full)
        print(f"{path_str:<70} {val}")
    for c in node.get("Children", []):
        walk2(c, full)


walk2(data)
