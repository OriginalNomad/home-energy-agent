#!/usr/bin/env python3
"""Controlled reserve-SoC gap experiment: does the Powerwall's self_consumption
grid-charge rate taper as SoC approaches reserve, or is it a flat ~5 kW?

Reads tokens from env (TESSIE_TOK, HA_TOK) so nothing is printed. Protects the
run with manual override and ALWAYS restores reserve=85 + override off.
"""
import os, json, time, urllib.request
from datetime import datetime

TESSIE = os.environ["TESSIE_TOK"]
HA = os.environ["HA_TOK"]
SITE = "2252120180790091"
HA_URL = "http://localhost:8123"
LOG = open("/tmp/gap_experiment.log", "w")


def p(*a):
    m = " ".join(str(x) for x in a)
    print(m); print(m, file=LOG); LOG.flush()


def tessie_reserve(pct):
    req = urllib.request.Request(
        f"https://api.tessie.com/api/1/energy_sites/{SITE}/backup",
        data=json.dumps({"backup_reserve_percent": int(pct)}).encode(),
        headers={"Authorization": f"Bearer {TESSIE}", "Content-Type": "application/json"},
        method="POST")
    return urllib.request.urlopen(req, timeout=30).read()


def ha_state(entity):
    req = urllib.request.Request(f"{HA_URL}/api/states/{entity}",
                                 headers={"Authorization": f"Bearer {HA}"})
    return json.load(urllib.request.urlopen(req, timeout=20))["state"]


def ha_service(domain, service, entity):
    req = urllib.request.Request(
        f"{HA_URL}/api/services/{domain}/{service}",
        data=json.dumps({"entity_id": entity}).encode(),
        headers={"Authorization": f"Bearer {HA}", "Content-Type": "application/json"},
        method="POST")
    return urllib.request.urlopen(req, timeout=20).read()


def soc():   return float(ha_state("sensor.tessie_powerwall_charge"))
def power(): return float(ha_state("sensor.tesla_powerwall_2_battery_power"))
def grid():  return float(ha_state("sensor.tesla_powerwall_2_site_power"))
def mode():  return ha_state("sensor.powerwall_mode")


try:
    p("=== GAP EXPERIMENT", datetime.now().strftime("%H:%M:%S"), "===")
    p(f"baseline: SoC={soc():.0f} mode={mode()} batt_power={power():.2f}kW grid={grid():.2f}kW")
    ha_service("input_boolean", "turn_off", {"entity_id": "input_boolean.agent_active"})
    p("Agent Control OFF — agent PAUSED (will compute but not command)\n")

    for gap in [40, 20, 10, 5, 3]:
        s = soc()
        target = min(int(round(s)) + gap, 100)
        tessie_reserve(target)
        p(f"-- reserve set to {target}  (SoC {s:.0f}, intended gap {gap}) --")
        time.sleep(90)   # let Tessie relay + Powerwall respond
        for i in range(4):
            sc, bp, g = soc(), power(), grid()
            p(f"   t+{90+i*20}s  SoC={sc:.0f}  gap={target-sc:.0f}  "
              f"batt_power={bp:+.2f}kW  grid={g:+.2f}kW")
            if i < 3:
                time.sleep(20)
        p("")
finally:
    try:
        tessie_reserve(85)
        p("restored reserve -> 85")
    except Exception as e:
        p("!! restore reserve FAILED:", e, "-- SET IT MANUALLY")
    try:
        ha_service("input_boolean", "turn_on", {"entity_id": "input_boolean.agent_active"})
        p("Agent Control ON — agent resumes next cycle")
    except Exception as e:
        p("!! agent-resume FAILED:", e, "-- TURN Agent Control BACK ON MANUALLY")
    p("=== DONE", datetime.now().strftime("%H:%M:%S"), "===")
