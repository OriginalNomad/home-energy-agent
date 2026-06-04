#!/usr/bin/env python3
"""
push_virtual_sensors.py
=======================
Restores virtual sensors that are lost on every HA restart (REST API-pushed
sensors are not persisted by HA). Run on HA startup via shell_command automation.

Pushes:
  - sensor.weather_radiation_now    (Open-Meteo current effective radiation)
  - sensor.weather_precip_now       (Open-Meteo current precipitation)
  - sensor.weather_tomorrow_solar   (tomorrow's solar outlook summary)
  - sensor.demand_window_monitor    (demand window pass/fail card data)
"""

import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytz
import requests

# --- Config (mirrors energy_agent.py) -----------------------------------------
HA_URL     = "http://localhost:8123"
HA_TOKEN   = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiIxZjQxNDVmOTBjYTI0ZDgyYjk5MTI5ZjE2YzY3ZWEzNSIsImlhdCI6MTc3OTY2OTMyNiwiZXhwIjoyMDk1MDI5MzI2fQ.Gu5FPRLbn3PpTOstsR-B87fyVeEC00dRXAB6ZiYiFt0"
HA_HEADERS = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
TZ         = pytz.timezone("Australia/Sydney")

DEMAND_SUMMARY = Path(__file__).parent / "demand_window_summary.py"

DEMAND_SUMMARY = Path(__file__).parent / "demand_window_summary.py"


# ------------------------------------------------------------------------------

def ha_set_state(entity_id: str, state: str, attributes=None):
    r = requests.post(
        f"{HA_URL}/api/states/{entity_id}",
        headers=HA_HEADERS,
        json={"state": state, "attributes": attributes or {}},
        timeout=10,
    )
    r.raise_for_status()


# ------------------------------------------------------------------------------
# Weather sensors
# ------------------------------------------------------------------------------

def push_weather():
    now = datetime.now(TZ)
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude":  -33.88,
            "longitude": 151.19,
            "hourly":    "cloud_cover,shortwave_radiation,precipitation_probability,precipitation",
            "timezone":  "Australia/Sydney",
            "forecast_days": 2,
        },
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()["hourly"]

    tomorrow_date = (now + timedelta(days=1)).date()
    tomorrow_hours = []

    current_hour_str = now.strftime("%Y-%m-%dT%H:00")
    current_idx = None

    for i, time_str in enumerate(data["time"]):
        dt   = datetime.fromisoformat(time_str).replace(tzinfo=TZ)
        hour = dt.hour
        if time_str == current_hour_str:
            current_idx = i
        too_early = dt.date() == now.date() and dt.hour < now.hour
        if too_early or not (6 <= hour <= 19):
            continue
        raw_rad = data["shortwave_radiation"][i]
        precip  = data["precipitation"][i]
        eff_rad = round(raw_rad * 0.25) if precip > 0.1 else round(raw_rad)
        if dt.date() == tomorrow_date:
            tomorrow_hours.append({
                "effective_radiation_wm2": eff_rad,
                "time": time_str,
            })

    # Tomorrow outlook
    core = [h for h in tomorrow_hours if 8 <= int(h["time"][11:13]) <= 15]
    if core:
        avg_rad = round(sum(h["effective_radiation_wm2"] for h in core) / len(core))
        outlook = "good" if avg_rad > 300 else ("poor" if avg_rad > 150 else "overcast")
    else:
        avg_rad, outlook = None, "unknown"

    # Current hour radiation
    if current_idx is not None:
        raw_rad = data["shortwave_radiation"][current_idx]
        precip  = data["precipitation"][current_idx]
        eff_rad = round(raw_rad * 0.25 if precip > 0.1 else raw_rad)
        ha_set_state("sensor.weather_radiation_now", str(eff_rad), {
            "friendly_name": "Solar Radiation Now (effective)",
            "unit_of_measurement": "W/m²",
            "state_class": "measurement",
            "raw_radiation_wm2": round(raw_rad),
            "precip_mm_h": round(precip, 1),
            "rain_adjusted": precip > 0.1,
        })
        ha_set_state("sensor.weather_precip_now", str(round(precip, 1)), {
            "friendly_name": "Precipitation Now",
            "unit_of_measurement": "mm/h",
            "state_class": "measurement",
        })
        print(f"  pushed sensor.weather_radiation_now = {eff_rad} W/m²")
        print(f"  pushed sensor.weather_precip_now = {precip} mm/h")

    ha_set_state("sensor.weather_tomorrow_solar", outlook, {
        "friendly_name": "Tomorrow Solar Outlook",
        "avg_radiation_wm2": avg_rad,
    })
    print(f"  pushed sensor.weather_tomorrow_solar = {outlook} ({avg_rad} W/m²)")


# ------------------------------------------------------------------------------
# Demand window monitor — delegate to demand_window_summary.py
# ------------------------------------------------------------------------------

def push_demand_window():
    result = subprocess.run(
        [sys.executable, str(DEMAND_SUMMARY), "--post"],
        capture_output=True, text=True,
    )
    if result.stdout:
        print(f"  {result.stdout.strip()}")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "demand_window_summary.py failed")


# ------------------------------------------------------------------------------

def main():
    errors = []

    print("push_virtual_sensors: restoring HA virtual sensors after restart")

    try:
        push_weather()
    except Exception as e:
        print(f"  ERROR pushing weather sensors: {e}", file=sys.stderr)
        errors.append(e)

    try:
        push_demand_window()
    except Exception as e:
        print(f"  ERROR pushing demand window sensor: {e}", file=sys.stderr)
        errors.append(e)

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
