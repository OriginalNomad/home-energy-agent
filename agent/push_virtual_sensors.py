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

import json
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

DAILY_ENERGY_FILE = Path(__file__).parent / "daily_energy.jsonl"

PEAK_MONTHS = {11, 12, 1, 2, 3, 6, 7, 8}
PASS_KW_THRESHOLD = 0.10


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
# Demand window monitor (mirrors demand_window_summary.py)
# ------------------------------------------------------------------------------

def push_demand_window():
    now = datetime.now(TZ)
    is_peak = now.month in PEAK_MONTHS

    records = {}
    if DAILY_ENERGY_FILE.exists():
        for line in DAILY_ENERGY_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                records[r["date"]] = r
            except (json.JSONDecodeError, KeyError):
                continue

    # Build rolling history for the current demand window month group
    history = []
    for date_str in sorted(records):
        rec = records[date_str]
        if not rec.get("peak_day"):
            continue
        dw = rec.get("demand_window", {})
        passed = dw.get("passed")
        peak_kw = dw.get("peak_30min_import_kw", 0.0)
        min_soc = dw.get("min_soc_pct")
        history.append({
            "date": date_str,
            "passed": passed,
            "peak_kw": peak_kw,
            "min_soc_pct": min_soc,
        })

    history = history[-14:]  # last 14 peak days

    passed_count  = sum(1 for h in history if h["passed"] is True)
    failed_count  = sum(1 for h in history if h["passed"] is False)
    latest_peak   = history[-1]["peak_kw"] if history else 0.0
    month_max_kw  = max((h["peak_kw"] for h in history), default=0.0)

    state = f"{round(month_max_kw, 3)} kW"
    attributes = {
        "friendly_name": "Demand Window Monitor",
        "passed_count":   passed_count,
        "failed_count":   failed_count,
        "latest_peak_kw": latest_peak,
        "month_max_kw":   round(month_max_kw, 3),
        "is_peak_month":  is_peak,
        "history":        history,
        "unit_of_measurement": "kW",
    }
    ha_set_state("sensor.demand_window_monitor", state, attributes)
    print(f"  pushed sensor.demand_window_monitor = {state} "
          f"({passed_count} passed / {failed_count} failed)")


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
