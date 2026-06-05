#!/usr/bin/env python3
"""
Home Energy Optimisation Agent
================================
Runs every 30 minutes. Reads state from Home Assistant, gets price and solar
forecasts, then uses Claude to reason about what (if anything) to do.

Install:  pip install anthropic requests pytz
Schedule: cron  */30 * * * *  /path/to/venv/bin/python /path/to/energy_agent.py
          — or — trigger from an HA automation via a shell_command

Verify entity IDs against your HA instance before running — especially the
Solcast and EV sensors which vary by integration version.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import anthropic
import pytz
import requests

# Optional LP optimiser (shadow mode only — never in the control path).
try:
    from optimizer import optimize_battery, OptParams
    _HAVE_OPTIMIZER = True
except Exception:                       # scipy/optimizer missing → skip cleanly
    _HAVE_OPTIMIZER = False

# ---------------------------------------------------------------------------
# Configuration — move sensitive values to environment variables in production
# ---------------------------------------------------------------------------

# Load .env file first so environment variables override these defaults.
# .env is gitignored — put machine-specific values (HA_URL, keys) there.
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _key = _k.strip()
            _val = _v.strip().strip('"').strip("'")
            if not os.environ.get(_key):   # set if missing OR empty
                os.environ[_key] = _val

# Defaults work on Mac Studio (localhost). Override via .env on other machines.
HA_URL   = os.environ.get("HA_URL",   "http://localhost:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiIxZjQxNDVmOTBjYTI0ZDgyYjk5MTI5ZjE2YzY3ZWEzNSIsImlhdCI6MTc3OTY2OTMyNiwiZXhwIjoyMDk1MDI5MzI2fQ.Gu5FPRLbn3PpTOstsR-B87fyVeEC00dRXAB6ZiYiFt0")

TESSIE_TOKEN   = os.environ.get("TESSIE_TOKEN",   "REDACTED_TOKEN")
TESSIE_SITE_ID = os.environ.get("TESSIE_SITE_ID", "2252120180790091")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")  # set via agent/.env

LOG_FILE   = Path(__file__).parent / "agent_decisions.log"
JSONL_FILE = Path(__file__).parent / "decisions.jsonl"

MODEL = "claude-sonnet-4-6"

# Token pricing — verify at console.anthropic.com/settings/billing
# List price (USD per 1M tokens); update if you have a volume discount.
# Cache reads are charged at 10% of the input rate.
COST_PER_1M_INPUT        = 3.00
COST_PER_1M_OUTPUT       = 15.00
COST_PER_1M_CACHE_READ   = COST_PER_1M_INPUT * 0.10
COST_PER_1M_CACHE_WRITE  = COST_PER_1M_INPUT * 1.25  # 25% premium on cache writes

# ---------------------------------------------------------------------------
# Feature flag — historical price model
# Set True to use rolling price percentiles for grid target calculation.
# Set False to revert to the static cost_target logic instantly.
# ---------------------------------------------------------------------------
HISTORICAL_PRICE_MODEL = True

# Insurance floor ceiling — maximum floor percentage when price is at the
# cheapest historical level. User can override via HA slider (below).
DEFAULT_MAX_INSURANCE_FLOOR = 70   # %
PRICE_HISTORY_DAYS          = 7    # days of JSONL price history to use
MIN_HISTORY_RECORDS         = 48   # minimum records before model activates (~1 day)

# Overnight hold: Solar Sponge (10am-3pm) is structurally cheaper than evening/overnight
# prices on most days. Don't charge overnight if current price exceeds this threshold —
# wait for the morning Solar Sponge instead. Rule 13 deadline maths handles peak months.
SOLAR_SPONGE_PRICE_THRESHOLD = 10.0   # ¢ — if overnight price > this, wait for Sponge

# Populated by get_current_state() / get_price_forecast() during each cycle,
# then read by log_decision() to write the structured JSON record.
_cycle_context: dict = {}
_demand_reserve_guard_fired: bool = False

SYDNEY_TZ   = pytz.timezone("Australia/Sydney")
PEAK_MONTHS = {11, 12, 1, 2, 3, 6, 7, 8}   # months with demand window

# ---------------------------------------------------------------------------
# Entity IDs — verify these match your HA instance
# ---------------------------------------------------------------------------

ENTITIES = {
    "battery_soc":          "sensor.tessie_powerwall_charge",
    "battery_soc_gateway":  "sensor.tesla_powerwall_2_charge",
    "battery_mode":         "sensor.powerwall_mode",
    "battery_reserve":      "sensor.powerwall_backup_reserve",
    "battery_target":       "sensor.battery_grid_charge_target",
    "grid_price":           "sensor.1a_wigram_road_glebe_general_price",
    "grid_forecast":        "sensor.1a_wigram_road_glebe_general_forecast",
    "cheap_window":         "sensor.amber_in_cheap_window",
    "solar_power":          "sensor.solaredge_current_power",         # W
    "solar_remaining":      "sensor.solcast_pv_forecast_forecast_remaining_today",  # kWh
    "solar_forecast_today":  "sensor.solcast_pv_forecast_forecast_today",
    "solcast_power_now":     "sensor.solcast_pv_forecast_power_now",    # W — Solcast's instantaneous estimate (÷1000 for kW)
    "solcast_this_hour":     "sensor.solcast_pv_forecast_forecast_this_hour",  # Wh — expected for current hour (÷1000 for kWh)
    "solcast_next_hour":     "sensor.solcast_pv_forecast_forecast_next_hour",  # Wh — expected for next hour (÷1000 for kWh)
    "home_load":            "sensor.home_load_30min_average",          # kW
    "battery_power":        "sensor.tesla_powerwall_2_battery_power",  # kW, positive=charging, negative=discharging
    "ev_plug":              "sensor.home_ev_charger_zappi_myenergi_home_ev_charger_zappi_plug_status",
    "ev_zappi_mode":        "select.home_ev_charger_zappi_myenergi_home_ev_charger_zappi_charge_mode",
    "ev_soc":               "sensor.polestar_7853_battery_charge_level",
    "ev_schedule_active":   "input_boolean.ev_schedule_active",
    "ev_departure_time":    "input_datetime.ev_departure_time",
    "ev_departure_target":  "input_number.ev_departure_target_pct",
    "ev_min_soc":           "input_number.ev_min_soc_pct",
    "ev_charge_target":     "input_number.ev_charge_target_pct",
    "ev_ultra_cheap_c":     "input_number.ev_ultra_cheap_threshold_c",
    "ev_standard_price_c":  "input_number.ev_standard_price_c",
    "ev_min_charge_price_c":"input_number.ev_min_charge_price_c",
    "battery_charge_threshold_c": "input_number.battery_charge_price_threshold_c",
    "battery_max_insurance_floor": "input_number.battery_max_insurance_floor_pct",
    "fit_price":              "sensor.1a_wigram_road_glebe_feed_in_price",
    "fit_forecast":           "sensor.1a_wigram_road_glebe_feed_in_forecast",
    "fit_descriptor":         "sensor.1a_wigram_road_glebe_feed_in_price_descriptor",
}

# ---------------------------------------------------------------------------
# Home Assistant helpers
# ---------------------------------------------------------------------------

HA_HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

def ha_get(entity_id: str) -> dict:
    r = requests.get(f"{HA_URL}/api/states/{entity_id}", headers=HA_HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()

def ha_state(entity_id: str) -> str:
    return ha_get(entity_id)["state"]

def ha_attrs(entity_id: str) -> dict:
    return ha_get(entity_id).get("attributes", {})

def ha_service(domain: str, service: str, data: dict):
    r = requests.post(
        f"{HA_URL}/api/services/{domain}/{service}",
        headers=HA_HEADERS,
        json=data,
        timeout=10,
    )
    r.raise_for_status()

def ha_set_state(entity_id: str, state: str, attributes=None):
    """Write a value directly to the HA state machine — creates read-only sensor entities."""
    r = requests.post(
        f"{HA_URL}/api/states/{entity_id}",
        headers=HA_HEADERS,
        json={"state": state, "attributes": attributes or {}},
        timeout=10,
    )
    r.raise_for_status()

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _ev_schedule(now: datetime) -> dict:
    """Read the HA EV schedule controls and return structured context for the agent."""
    active = ha_state(ENTITIES["ev_schedule_active"]) == "on"
    if not active:
        return {"active": False}
    try:
        dep_str    = ha_state(ENTITIES["ev_departure_time"])   # "YYYY-MM-DD HH:MM:SS"
        dep_target = int(float(ha_state(ENTITIES["ev_departure_target"])))
        dep_dt     = datetime.strptime(dep_str[:16], "%Y-%m-%d %H:%M").replace(tzinfo=SYDNEY_TZ)
        hours_to_departure = round((dep_dt - now).total_seconds() / 3600, 1)
        return {
            "active":              True,
            "departure_time":      dep_dt.strftime("%Y-%m-%d %H:%M"),
            "departure_target_pct": dep_target,
            "hours_to_departure":  hours_to_departure,
        }
    except Exception:
        return {"active": True, "error": "could not parse departure time"}


def _build_battery_state(soc_tessie: int, soc_gateway: int, reserve: int,
                         mode: str, grid_target: int, charge_rate_kw: float) -> dict:
    """Return battery state dict, substituting gateway SoC if Tessie reading looks wrong.

    Tessie is a cloud poll and occasionally returns 0 or an implausibly low value.
    The gateway floors at reserve when reserve > true SoC, but when reserve is low
    (gateway > reserve) the gateway reading is reliable. Use it as a sanity check.
    """
    soc_tessie = soc_tessie or 0
    soc_gateway = soc_gateway or 0

    # Gateway is reliable (not floor-clipped) when it reads above the reserve level.
    gateway_reliable = soc_gateway > reserve

    tessie_suspicious = (
        soc_tessie == 0 or
        (gateway_reliable and soc_gateway > soc_tessie + 15)
    )

    if tessie_suspicious and gateway_reliable:
        print(
            f"WARNING: Tessie SoC ({soc_tessie}%) looks wrong "
            f"(gateway={soc_gateway}%, reserve={reserve}%) — using gateway reading.",
            file=sys.stderr,
        )
        soc_used = soc_gateway
        tessie_failed = True
    else:
        soc_used = soc_tessie
        tessie_failed = False

    return {
        "soc_pct":         soc_used,
        "soc_tessie_pct":  soc_tessie,
        "soc_gateway_pct": soc_gateway,
        "tessie_soc_failed": tessie_failed,
        "mode":            mode,
        "reserve_pct":     reserve,
        "grid_target_pct": grid_target,
        "charge_rate_kw":  charge_rate_kw,
    }


def get_current_state() -> dict:
    now = datetime.now(SYDNEY_TZ)
    month, hour = now.month, now.hour
    is_peak      = month in PEAK_MONTHS
    in_demand    = is_peak and 15 <= hour < 21
    in_sponge    = 10 <= hour < 15          # Solar Sponge window

    ev_plug_state   = ha_state(ENTITIES["ev_plug"])
    ev_plugged      = ev_plug_state != "EV Disconnected"
    _solar_raw      = ha_state(ENTITIES["solar_power"])
    _solar_unavail  = _solar_raw in ("unavailable", "unknown")
    if _solar_unavail:
        print("WARNING: sensor.solaredge_current_power is unavailable in HA — "
              "solar reading will be 0; zero-solar cycle NOT counted.", file=sys.stderr)

    state = {
        "timestamp":       now.strftime("%Y-%m-%d %H:%M %Z"),
        "month":           now.strftime("%B"),
        "is_peak_month":   is_peak,
        "in_demand_window": in_demand,
        "in_solar_sponge":  in_sponge,
        "battery": _build_battery_state(
            _int(ha_state(ENTITIES["battery_soc"])),
            _int(ha_state(ENTITIES["battery_soc_gateway"])),
            _int(ha_state(ENTITIES["battery_reserve"])),
            ha_state(ENTITIES["battery_mode"]),
            _int(ha_state(ENTITIES["battery_target"])),
            round(_float(ha_state(ENTITIES["battery_power"])), 2),
        ),
        "grid": {
            "price_cents_kwh":  round(_float(ha_state(ENTITIES["grid_price"])) * 100, 1),
            "in_cheap_window":  ha_state(ENTITIES["cheap_window"]) == "True",
            "fit_price_cents_kwh": round(_float(ha_state(ENTITIES["fit_price"])) * 100, 1),
            "fit_descriptor":   ha_state(ENTITIES["fit_descriptor"]),
        },
        "solar": {
            # Unit notes: solaredge_current_power=W, solcast_power_now=W, this_hour=Wh, next_hour=Wh
            # remaining_today is natively kWh (no conversion needed)
            # sensor_unavailable=True means HA returned "unavailable"/"unknown" — treat differently
            # from genuine zero production; do NOT count as a zero-solar cycle.
            "sensor_unavailable":       _solar_unavail,
            "current_kw":               round(_float(_solar_raw) / 1000, 2),
            "solcast_power_now_kw":     round(_float(ha_state(ENTITIES["solcast_power_now"])) / 1000, 2),
            "forecast_this_hour_kwh":   round(_float(ha_state(ENTITIES["solcast_this_hour"])) / 1000, 2),
            "forecast_next_hour_kwh":   round(_float(ha_state(ENTITIES["solcast_next_hour"])) / 1000, 2),
            "forecast_remaining_kwh":   round(_float(ha_state(ENTITIES["solar_remaining"])), 1),
            # Accuracy: compare actual kW vs forecast_this_hour (Wh→kWh ≈ avg kW for the hour)
            "forecast_accuracy":        _solar_accuracy(
                                            round(_float(_solar_raw) / 1000, 2),
                                            round(_float(ha_state(ENTITIES["solcast_this_hour"])) / 1000, 2)
                                        ),
        },
        "home_load_kw":  round(_float(ha_state(ENTITIES["home_load"])), 2),
        "ev": {
            "plug_status": ev_plug_state,
            "plugged_in":  ev_plugged,
            "charging":    ev_plug_state == "Charging",
            "zappi_mode":  ha_state(ENTITIES["ev_zappi_mode"]) if ev_plugged else "n/a",
            "ev_soc_pct":  _safe_int(ENTITIES["ev_soc"]) if ev_plugged else None,
            "min_soc_pct": int(float(ha_state(ENTITIES["ev_min_soc"]) or 20)),
            "charge_target_pct": int(float(ha_state(ENTITIES["ev_charge_target"]) or 80)),
            "schedule":    _ev_schedule(now),
        },
        "settings": {
            "ev_ultra_cheap_c":           _safe_float(ENTITIES["ev_ultra_cheap_c"], 5),
            "ev_standard_price_c":        _safe_float(ENTITIES["ev_standard_price_c"], 10),
            "ev_min_charge_price_c":      _safe_float(ENTITIES["ev_min_charge_price_c"], 20),
            "battery_charge_threshold_c": _safe_float(ENTITIES["battery_charge_threshold_c"], 12),
            "max_insurance_floor_pct":    _safe_float(ENTITIES["battery_max_insurance_floor"], DEFAULT_MAX_INSURANCE_FLOOR),
            # price_stats injected by run_agent() after get_current_state() returns
        },
    }
    _cycle_context["state"] = state
    return state


def get_price_forecast() -> list[dict]:
    """Next 12 hours of Amber prices, resampled to uniform 30-minute buckets.

    The Amber sensor mixes interval sizes: the near-term intervals are 5-minute
    and the rest are 30-minute. Downstream logic (hours_to_cheap_end, the
    sustained-rise scan, the "6h"/"12h" forecast slices) all assume uniform
    30-min spacing — so we bucket every sub-interval into its 30-min slot and
    average the price. This makes index × 0.5h an accurate "hours from now".
    """
    attrs = ha_attrs(ENTITIES["grid_forecast"])
    forecasts = attrs.get("forecasts", [])
    if not forecasts:
        print("  Warning: price forecast is EMPTY — agent is flying blind on prices",
              file=sys.stderr)
        _cycle_context["price_forecast"] = []
        return []

    buckets: dict[str, list[float]] = {}
    descriptors: dict[str, str] = {}
    order: list[str] = []
    for f in forecasts:
        nem = f.get("nem_date", "")
        if len(nem) < 16:
            continue
        slot = "00" if int(nem[14:16]) < 30 else "30"
        key  = f"{nem[:11]}{nem[11:13]}:{slot}"     # e.g. 2026-05-29T16:30
        if key not in buckets:
            buckets[key]     = []
            descriptors[key] = f.get("descriptor", "")
            order.append(key)
        buckets[key].append(_float(f.get("per_kwh", 0)) * 100)

    result = []
    for key in order[:24]:          # 24 × 30 min = 12 h
        prices = buckets[key]
        result.append({
            "time":       key[:16].replace("T", " "),
            "cents_kwh":  round(sum(prices) / len(prices), 1),
            "descriptor": descriptors[key],
        })
    _cycle_context["price_forecast"] = result
    return result


def get_solar_forecast() -> list[dict]:
    """Remaining Solcast forecast periods for today."""
    now   = datetime.now(SYDNEY_TZ)
    attrs = ha_attrs(ENTITIES["solar_forecast_today"])
    # Solcast integration key varies by version — try both
    periods = attrs.get("detailedForecast") or attrs.get("detailed_forecast", [])
    result  = []
    for p in periods:
        start = p.get("period_start", "")
        if start[:16] >= now.strftime("%Y-%m-%dT%H:%M"):
            result.append({
                "time":    start[:16],
                "kw_est":  round(_float(p.get("pv_estimate", 0)), 2),
            })
    return result[:16]   # 8 hours


def get_weather_forecast() -> dict:
    """
    Hourly cloud cover, solar radiation, and rain probability from Open-Meteo.
    Returns solar-relevant hours (6am–7pm) for today and tomorrow.
    No API key required.
    """
    now = datetime.now(SYDNEY_TZ)
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

    today_hours, tomorrow_hours = [], []
    tomorrow_date = (now + timedelta(days=1)).date()

    for i, time_str in enumerate(data["time"]):
        dt   = datetime.fromisoformat(time_str).replace(tzinfo=SYDNEY_TZ)
        hour = dt.hour
        # Skip hours before the current hour today; include current hour and all of tomorrow
        too_early = dt.date() == now.date() and dt.hour < now.hour
        if too_early or not (6 <= hour <= 19):
            continue
        raw_rad  = data["shortwave_radiation"][i]
        precip   = data["precipitation"][i]        # mm/h
        # Rain attenuates actual surface irradiance to ~25% of model GHI.
        # SolarEdge won't start below ~50 W/m² — so rain effectively = no solar.
        eff_rad  = round(raw_rad * 0.25) if precip > 0.1 else round(raw_rad)
        entry = {
            "time":              time_str,
            "cloud_cover_pct":   data["cloud_cover"][i],
            "radiation_wm2":     round(raw_rad),
            "effective_radiation_wm2": eff_rad,
            "precip_mm_h":       precip,
            "rain_prob_pct":     data["precipitation_probability"][i],
        }
        if dt.date() == tomorrow_date:
            tomorrow_hours.append(entry)
        else:
            today_hours.append(entry)

    # Summarise tomorrow's solar quality using effective (rain-adjusted) radiation
    core = [h for h in tomorrow_hours if 8 <= int(h["time"][11:13]) <= 15]
    if core:
        avg_rad = round(sum(h["effective_radiation_wm2"] for h in core) / len(core))
        if avg_rad > 300:
            outlook = "good"
        elif avg_rad > 150:
            outlook = "poor"
        else:
            outlook = "overcast"
    else:
        avg_rad, outlook = None, "unknown"

    result = {
        "today_remaining":         today_hours,
        "tomorrow":                tomorrow_hours,
        "tomorrow_solar_outlook":  outlook,
        "tomorrow_avg_radiation":  avg_rad,
    }
    _cycle_context["weather_forecast"] = result

    # Push current hour's values to HA as read-only sensor entities
    current_hour_str = now.strftime("%Y-%m-%dT%H:00")
    try:
        idx     = data["time"].index(current_hour_str)
        raw_rad = data["shortwave_radiation"][idx]
        precip  = data["precipitation"][idx]
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
        ha_set_state("sensor.weather_tomorrow_solar", outlook, {
            "friendly_name": "Tomorrow Solar Outlook",
            "avg_radiation_wm2": avg_rad,
        })
    except Exception as exc:
        print(f"  Warning: weather HA push failed: {exc}", file=sys.stderr)

    return result


def set_powerwall_reserve(percent: int) -> str:
    r = requests.post(
        f"https://api.tessie.com/api/1/energy_sites/{TESSIE_SITE_ID}/backup",
        headers={"Authorization": f"Bearer {TESSIE_TOKEN}", "Content-Type": "application/json"},
        json={"backup_reserve_percent": percent},
        timeout=15,
    )
    r.raise_for_status()
    return f"Reserve set to {percent}%"


def set_powerwall_mode(mode: str) -> str:
    r = requests.post(
        f"https://api.tessie.com/api/1/energy_sites/{TESSIE_SITE_ID}/operation",
        headers={"Authorization": f"Bearer {TESSIE_TOKEN}", "Content-Type": "application/json"},
        json={"default_real_mode": mode},
        timeout=15,
    )
    r.raise_for_status()
    return f"Mode set to {mode}"


def set_zappi_mode(mode: str) -> str:
    ha_service("select", "select_option", {
        "entity_id": ENTITIES["ev_zappi_mode"],
        "option": mode,
    })
    return f"Zappi set to {mode}"


def _compute_projected_3pm(now: datetime) -> tuple[int, int]:
    """
    Return (goal_3pm_soc, projected_3pm_soc) from current cycle context.
    Mirrors the HA card logic so the two stay consistent.
    """
    state       = _cycle_context.get("state", {})
    battery     = state.get("battery", {})
    solar       = state.get("solar", {})

    soc         = battery.get("soc_pct", 0) or 0
    battery_kwh = soc / 100 * 13.5
    home_load   = state.get("home_load_kw", 0) or 0
    is_peak     = state.get("is_peak_month", False)

    now_h        = now.hour + now.minute / 60
    hours_to_3pm = max(15.0 - now_h, 0.0)

    # Solar accuracy scaling
    actual_kw   = solar.get("current_kw", 0) or 0
    forecast_kw = solar.get("forecast_this_hour_kwh", 0) or 0
    if forecast_kw < 0.2 or now_h < 8:
        solar_scale = 1.0
    elif actual_kw / forecast_kw < 0.3:
        solar_scale = 0.0
    elif actual_kw / forecast_kw < 0.7:
        solar_scale = 0.5
    else:
        solar_scale = 1.0

    remaining_solar = (solar.get("forecast_remaining_kwh", 0) or 0) * solar_scale
    net_solar       = max(remaining_solar - home_load * hours_to_3pm, 0.0)

    # Grid charging contribution (assume self_consumption rate)
    reserve  = battery.get("reserve_pct", 5) or 5
    grid_kwh = 0.0
    if reserve > soc:
        headroom = max((reserve / 100 * 13.5) - battery_kwh, 0.0)
        grid_kwh = min(1.7 * hours_to_3pm, headroom)

    proj_kwh = min(battery_kwh + net_solar + grid_kwh, 13.5)
    proj_soc = round(proj_kwh / 13.5 * 100)
    goal_soc = 85 if is_peak else 80
    return goal_soc, proj_soc


def _maybe_write_daily_accuracy(now: datetime):
    """
    After 3pm, write one daily_accuracy record to decisions.jsonl if not done today.
    Compares what morning cycles projected for 3pm SoC against what actually happened.
    """
    if now.hour < 15 or not JSONL_FILE.exists():
        return

    today_str = now.strftime("%Y-%m-%d")
    lines     = [l for l in JSONL_FILE.read_text().splitlines() if l.strip()]

    today_records: list[dict] = []
    for line in lines:
        try:
            r = json.loads(line)
            if r.get("ts", "").startswith(today_str):
                today_records.append(r)
        except Exception:
            continue

    # Already written today?
    if any(r.get("record_type") == "daily_accuracy" for r in today_records):
        return

    # Actual 3pm SoC — first record at or after 15:00
    actual_3pm_soc = None
    for r in today_records:
        hour = int(r.get("ts", "T00")[11:13])
        if hour >= 15 and r.get("record_type") != "daily_accuracy":
            actual_3pm_soc = r.get("soc")
            break

    if actual_3pm_soc is None:
        return  # no post-3pm record yet — will try next cycle

    # Projections from key hours
    projections: dict[str, int | None] = {}
    for r in today_records:
        if r.get("record_type") == "daily_accuracy":
            continue
        hour = int(r.get("ts", "T00")[11:13])
        for h in (6, 8, 10, 12):
            if hour == h and str(h) not in projections:
                projections[str(h)] = r.get("projected_3pm_soc")

    goal_soc = today_records[-1].get("goal_3pm_soc", 80) if today_records else 80
    errors   = {h: ((v - actual_3pm_soc) if v is not None else None)
                for h, v in projections.items()}

    record = {
        "ts":             now.isoformat(),
        "record_type":    "daily_accuracy",
        "date":           today_str,
        "goal_3pm_soc":   goal_soc,
        "actual_3pm_soc": actual_3pm_soc,
        "projections":    projections,
        "projection_errors": errors,   # positive = over-estimated, negative = under-estimated
    }
    with JSONL_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"  Daily accuracy: goal={goal_soc}% actual={actual_3pm_soc}% "
          f"projections={projections} errors={errors}")


def log_decision(summary: str, actions_taken: list[str], ev_summary: str = "") -> str:
    now     = datetime.now(SYDNEY_TZ)
    actions = ", ".join(actions_taken) if actions_taken else "hold"
    entry   = f"[{now.strftime('%Y-%m-%d %H:%M')}] {summary} | Actions: {actions}"

    # Append to plain-text log (human-readable, committed to git)
    with LOG_FILE.open("a") as f:
        f.write(entry + "\n")

    # Append structured JSON record for the analyst agent
    state    = _cycle_context.get("state", {})
    battery  = state.get("battery", {})
    grid     = state.get("grid", {})
    solar    = state.get("solar", {})
    ev       = state.get("ev", {})
    forecast = _cycle_context.get("price_forecast", [])

    reserve_set = None
    mode_set    = None
    zappi_set   = None
    for a in actions_taken:
        m = re.search(r'set_reserve\((\d+)', a)
        if m:
            reserve_set = int(m.group(1))
        if "autonomous" in a:
            mode_set = "autonomous"
        elif "self_consumption" in a:
            mode_set = "self_consumption"
        if "Eco+" in a:
            zappi_set = "Eco+"
        elif "Eco" in a:
            zappi_set = "Eco"
        elif "Fast" in a:
            zappi_set = "Fast"
        elif "Off" in a:
            zappi_set = "Off"

    record = {
        "ts":                   now.isoformat(),
        "soc":                  battery.get("soc_pct"),
        "reserve_before":       battery.get("reserve_pct"),
        "mode_before":          battery.get("mode"),
        "grid_target_pct":      battery.get("grid_target_pct"),
        "reserve_set":          reserve_set,
        "mode_set":             mode_set,
        "zappi_set":            zappi_set,
        "price_c":              grid.get("price_cents_kwh"),
        "in_cheap_window":      grid.get("in_cheap_window"),
        "is_peak_month":        state.get("is_peak_month"),
        "in_demand_window":     state.get("in_demand_window"),
        "in_solar_sponge":      state.get("in_solar_sponge"),
        "forecast_accuracy":    solar.get("forecast_accuracy"),
        "solar_current_kw":     solar.get("current_kw"),
        "solar_sensor_unavail": solar.get("sensor_unavailable", False),
        "solar_remaining_kwh":  solar.get("forecast_remaining_kwh"),
        "solar_this_hour_kwh":  solar.get("forecast_this_hour_kwh"),
        "solar_next_hour_kwh":  solar.get("forecast_next_hour_kwh"),
        "home_load_kw":         state.get("home_load_kw"),
        "fit_price_c":          grid.get("fit_price_cents_kwh"),
        "fit_descriptor":       grid.get("fit_descriptor"),
        "ev_plugged":           ev.get("plugged_in"),
        "ev_soc":               ev.get("ev_soc_pct"),
        "ev_zappi_mode_before": ev.get("zappi_mode"),
        "price_forecast_6h":    [f["cents_kwh"] for f in forecast[:12]],
        "price_forecast_6h_times": [f["time"] for f in forecast[:12]],
        "tomorrow_solar_outlook":  _cycle_context.get("weather_forecast", {}).get("tomorrow_solar_outlook"),
        "tomorrow_avg_radiation":  _cycle_context.get("weather_forecast", {}).get("tomorrow_avg_radiation"),
        "input_tokens":         _cycle_context.get("input_tokens", 0),
        "output_tokens":        _cycle_context.get("output_tokens", 0),
        "cache_read_tokens":    _cycle_context.get("cache_read_tokens", 0),
        "cache_write_tokens":   _cycle_context.get("cache_write_tokens", 0),
        "est_cost_usd":         round(
                                    # input_tokens = non-cached only; cache tokens are tracked separately
                                    _cycle_context.get("input_tokens", 0)       / 1_000_000 * COST_PER_1M_INPUT +
                                    _cycle_context.get("cache_write_tokens", 0) / 1_000_000 * COST_PER_1M_CACHE_WRITE +
                                    _cycle_context.get("cache_read_tokens", 0)  / 1_000_000 * COST_PER_1M_CACHE_READ +
                                    _cycle_context.get("output_tokens", 0)      / 1_000_000 * COST_PER_1M_OUTPUT,
                                    5),
        "actions":              actions_taken,
        "summary":              summary,
        "demand_reserve_guard_fired": _demand_reserve_guard_fired,
    }

    goal_3pm, proj_3pm = _compute_projected_3pm(now)
    record["goal_3pm_soc"]      = goal_3pm
    record["projected_3pm_soc"] = proj_3pm

    # Shadow comparison (Phase 3): log the deterministic verdict next to what the LLM
    # actually did, so divergence can be measured over the coming weeks before cutover.
    ctx = _cycle_context.get("decision_context")
    if ctx:
        rec = ctx["recommended"]
        rec    = ctx["recommended"]
        ev_rec = ctx["ev_recommended"]
        record["computed_verdict"]    = rec
        record["computed_ev_verdict"] = ev_rec
        record["computed_context"]  = {k: ctx[k] for k in (
            "zero_solar_day", "deferral_detected", "sliding_forecast", "solar_unreliable",
            "cost_target_pct", "hours_to_cheap_end", "hours_to_deadline", "kwh_needed_85",
            "spread_c", "forward_min_c", "go_hard_slot")}
        soc_now      = record.get("soc")
        actual_charge = ((reserve_set is not None and soc_now is not None and reserve_set > soc_now)
                         or mode_set == "autonomous")
        rec_charge   = rec["action"] == "charge"
        record["shadow_action_match"] = (actual_charge == rec_charge)
        record["shadow_mode_match"]   = ((mode_set == rec["mode"])
                                         if (rec_charge and mode_set) else None)
        # EV shadow match — only meaningful when EV is plugged in
        if ev_rec.get("rule_fired") != "ev_disconnected":
            record["shadow_ev_match"] = (zappi_set == ev_rec.get("zappi_mode"))

    # LP optimiser shadow fields (three-way A/B: LLM vs deterministic vs optimiser)
    ov = _cycle_context.get("optimizer_verdict")
    if ov is not None:
        record["optimizer_verdict"]  = ov
        record["optimizer_context"]  = _cycle_context.get("optimizer_context")
        _soc = record.get("soc")
        _llm_charge = ((reserve_set is not None and _soc is not None and reserve_set > _soc)
                       or mode_set == "autonomous")
        record["optimizer_action_match"] = (_llm_charge == (ov.get("action") == "charge"))
        _cv = record.get("computed_verdict")
        if _cv is not None:
            record["optimizer_vs_deterministic"] = (ov.get("action") == _cv.get("action"))

    with JSONL_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")

    _maybe_write_daily_accuracy(now)

    # Compute today's cumulative cost from JSONL and push to HA
    try:
        today_str   = now.strftime("%Y-%m-%d")
        daily_cost  = 0.0
        daily_input = 0
        daily_output = 0
        if JSONL_FILE.exists():
            for line in JSONL_FILE.read_text().splitlines():
                try:
                    entry = json.loads(line)
                    if entry.get("ts", "").startswith(today_str):
                        daily_cost   += entry.get("est_cost_usd", 0)
                        daily_input  += entry.get("input_tokens", 0)
                        daily_output += entry.get("output_tokens", 0)
                except Exception:
                    pass
        ha_set_state("sensor.agent_daily_cost", f"{daily_cost:.4f}", {
            "friendly_name":      "Agent — Daily API Cost",
            "unit_of_measurement": "USD",
            "state_class":        "total_increasing",
            "daily_input_tokens":  daily_input,
            "daily_output_tokens": daily_output,
            "pricing_model":      "claude-opus-4-5 list price — verify at console.anthropic.com",
        })
    except Exception as exc:
        print(f"  Warning: daily cost push failed: {exc}", file=sys.stderr)

    # Battery notification
    battery_actions = [a for a in actions_taken if not a.startswith("set_zappi")]
    battery_actions_str = ", ".join(battery_actions) if battery_actions else "hold"
    ha_service("persistent_notification", "create", {
        "notification_id": "energy_agent_battery",
        "title": f"🔋 Battery — {now.strftime('%H:%M')}",
        "message": f"{summary}\n\n**Actions:** {battery_actions_str}",
    })

    # EV notification
    ev_actions = [a for a in actions_taken if a.startswith("set_zappi")]
    ev_actions_str = ", ".join(ev_actions) if ev_actions else "hold"
    ev_msg = ev_summary if ev_summary else summary
    ha_service("persistent_notification", "create", {
        "notification_id": "energy_agent_ev",
        "title": f"🚗 EV — {now.strftime('%H:%M')}",
        "message": f"{ev_msg}\n\n**Actions:** {ev_actions_str}",
    })

    # Write a logbook entry — sequential history in HA History panel
    ha_service("logbook", "log", {
        "name": "Energy Agent",
        "message": f"{actions} | {summary}",
        "entity_id": "sensor.tessie_powerwall_charge",
    })

    # Update dashboard helper entities so the "Last Battery Decision" card stays live.
    # Read live sensor snapshot — values reflect state at decision time.
    try:
        soc           = _int(ha_state(ENTITIES["battery_soc"]))
        price         = round(_float(ha_state(ENTITIES["grid_price"])) * 100, 1)
        solar_rem     = round(_float(ha_state(ENTITIES["solar_remaining"])), 1)
        grid_target   = _int(ha_state(ENTITIES["battery_target"]))

        # Reserve set: extract from actions list (e.g. "set_reserve(62%)"), else current
        reserve_set = _int(ha_state(ENTITIES["battery_reserve"]))
        for a in actions_taken:
            m = re.search(r'set_reserve\((\d+)', a)
            if m:
                reserve_set = int(m.group(1))
                break

        ha_service("input_text", "set_value", {
            "entity_id": "input_text.battery_decision_automation",
            "value": f"⚡ Energy Agent — {now.strftime('%H:%M')}",
        })
        ha_service("input_text", "set_value", {
            "entity_id": "input_text.battery_decision_action",
            "value": summary[:255],
        })
        ha_service("input_number", "set_value", {
            "entity_id": "input_number.battery_decision_soc",
            "value": soc,
        })
        ha_service("input_number", "set_value", {
            "entity_id": "input_number.battery_decision_price",
            "value": price,
        })
        ha_service("input_number", "set_value", {
            "entity_id": "input_number.battery_decision_solar_remaining",
            "value": solar_rem,
        })
        ha_service("input_number", "set_value", {
            "entity_id": "input_number.battery_decision_grid_target",
            "value": grid_target,
        })
        ha_service("input_number", "set_value", {
            "entity_id": "input_number.battery_decision_reserve_set",
            "value": reserve_set,
        })
    except Exception as exc:
        print(f"  Warning: dashboard helper update failed: {exc}", file=sys.stderr)

    return "Logged"


# ---------------------------------------------------------------------------
# Short-term memory — last N decisions for deferral detection
# ---------------------------------------------------------------------------

def get_recent_decisions(n: int = 3) -> str:
    """
    Return the last n decisions from decisions.jsonl as a compact context block.
    Injected into each cycle's initial message so the agent can detect when it
    has been deferring on a forecast that repeatedly doesn't arrive.
    """
    if not JSONL_FILE.exists():
        return "No prior decisions on record."

    lines = [l for l in JSONL_FILE.read_text().splitlines() if l.strip()]
    recent: list[dict] = []
    for line in reversed(lines):
        try:
            recent.append(json.loads(line))
        except Exception:
            continue
        if len(recent) >= n:
            break

    if not recent:
        return "No recent decisions on record."

    parts = []
    for r in reversed(recent):  # chronological order
        ts            = r.get("ts", "")[:16].replace("T", " ")
        soc           = r.get("soc", "?")
        price         = r.get("price_c", "?")
        reserve_before = r.get("reserve_before", "?")
        reserve_set   = r.get("reserve_set")
        reserve_str   = (f"{reserve_before}%→{reserve_set}%"
                         if reserve_set is not None else f"{reserve_before}% (unchanged)")
        actions       = r.get("actions", [])
        action_str    = ", ".join(actions) if actions else "hold"
        summary       = r.get("summary", "")[:160]
        solar_kw   = r.get("solar_current_kw", "?")
        accuracy   = r.get("forecast_accuracy") or ""
        parts.append(
            f"  [{ts}] SoC={soc}% price={price}¢ reserve={reserve_str} "
            f"solar={solar_kw}kW ({accuracy[:20]}) | {action_str}\n"
            f"    \"{summary}\""
        )

    return "\n".join(parts)


def get_recent_records(n: int = 3) -> list[dict]:
    """Return the last n non-accuracy decision records as parsed dicts (chronological)."""
    if not JSONL_FILE.exists():
        return []
    lines = [l for l in JSONL_FILE.read_text().splitlines() if l.strip()]
    recent: list[dict] = []
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("record_type") == "daily_accuracy":
            continue
        recent.append(r)
        if len(recent) >= n:
            break
    return list(reversed(recent))


# ---------------------------------------------------------------------------
# Pure decision logic — deterministic precompute (Phase 1 of re-architecture)
#
# compute_decision_context() takes plain data in and returns a dict out: no HTTP,
# no clock reads (now is passed in), no globals. This makes every calculation the
# system prompt currently asks the LLM to do in its head unit-testable in
# isolation. It is NOT yet wired into the control path — it runs in shadow only.
# ---------------------------------------------------------------------------

USABLE_KWH       = 13.5
SLOW_KW          = 1.7
FAST_KW          = 5.0
DEMAND_DEADLINE  = 14 + 55 / 60   # 2:55pm as a decimal hour


def _accuracy_class(label: str) -> str:
    """Collapse the verbose forecast_accuracy string into one of: good/poor/unreliable/na."""
    s = (label or "").lower()
    if s.startswith("good"):
        return "good"
    if s.startswith("poor"):
        return "poor"
    if s.startswith("unreliable"):
        return "unreliable"
    return "na"


CHEAP_BAND_ALPHA = 0.30   # cheap = bottom 30% of today's price swing (trough → evening peak)
MIN_DAILY_SWING  = 5.0    # ¢ — below this the day is flat; no meaningful cheap window to "end"


def _hours_to_cheap_end(price_forecast: list[dict], current_price: float,
                        alpha: float = CHEAP_BAND_ALPHA) -> float:
    """Hours until today's cheap trough ends — i.e. price climbs out of the bottom
    `alpha`-band of the day's own range and starts the ramp toward the evening peak.

    Scale-free by design: normalises to each day's own minimum and *evening* peak
    (15:00–21:00) rather than absolute cents or a fixed +N¢ jump. This captures the
    structural daily shape (morning bump → noon trough → evening peak) on both a
    10–20¢ day and a 30–80¢ day. Anchoring the high to the evening window keeps the
    morning bump from inflating the range. Returns hours to the end of the cheap
    region that lies ahead; 0.0 if no cheap interval remains; 6.0 if it never ends
    within the horizon. Assumes uniform 30-min spacing.
    """
    prices = [f.get("cents_kwh", 0.0) for f in price_forecast]
    if len(prices) < 2:
        return 6.0

    evening = []
    for f in price_forecast:
        t  = f.get("time", "")
        hh = t[11:13] if len(t) >= 13 else ""
        if hh.isdigit() and 15 <= int(hh) < 21:
            evening.append(f.get("cents_kwh", 0.0))
    p_peak = max(evening) if evening else max(prices)
    p_min  = min(prices)
    rng    = p_peak - p_min
    if rng < MIN_DAILY_SWING:
        return 6.0                       # flat day — no real trough/ramp to track

    threshold = p_min + alpha * rng
    start = next((i for i, p in enumerate(prices) if p <= threshold), None)
    if start is None:
        return 0.0                       # already above the cheap band — window closed
    for j in range(start, len(prices) - 1):
        if prices[j] > threshold and prices[j + 1] > threshold:
            return j * 0.5               # right edge of the cheap region
    return 6.0


def _detect_deferral(recent_records: list[dict], current_price: float) -> bool:
    """2+ consecutive holds while price stayed within 2¢ → the awaited cheap window isn't coming."""
    if len(recent_records) < 2:
        return False
    holds = 0
    for r in reversed(recent_records):
        actions = r.get("actions") or []
        price   = r.get("price_c")
        if not actions and price is not None and abs(price - current_price) <= 2.0:
            holds += 1
        else:
            break
    return holds >= 2


def _cheapest_go_hard_slot(
    price_forecast: list[dict],
    current_price_c: float,
    soc: float,
    home_load_kw: float,
    hours_to_deadline: float,
    safety_buffer_h: float = 0.5,
    min_saving_c: float = 1.0,
) -> "tuple[float, float] | None":
    """Find the cheapest upcoming slot where fast-charging to 85% before the deadline is still feasible.

    Conservative SoC projection at each slot: home load drains the battery during the wait,
    no solar credit (pessimistic — real outcome will be better when solar is producing).

    Returns (cheapest_price_c, hours_until_slot) or None if no slot is cheaper by min_saving_c.
    """
    best_price = current_price_c - min_saving_c  # must beat this to be worth returning
    best_hours: float | None = None
    for i, f in enumerate(price_forecast):
        hours_until = (i + 1) * 0.5          # 30-min slots
        slot_price  = f.get("cents_kwh", current_price_c)
        # Conservative SoC at this slot (home load draining, no solar)
        soc_at_slot = max(5.0, soc - home_load_kw * hours_until / USABLE_KWH * 100)
        kwh_needed  = max((0.85 - soc_at_slot / 100) * USABLE_KWH, 0.0)
        fill_fast_h = kwh_needed / FAST_KW
        # Feasible = can finish fast-fill AND have safety buffer before deadline
        if hours_until + fill_fast_h + safety_buffer_h <= hours_to_deadline:
            if slot_price < best_price:
                best_price = slot_price
                best_hours = hours_until
    if best_hours is not None:
        return best_price, best_hours
    return None


def _detect_sliding_forecast(recent_records: list[dict], current_price: float,
                             current_forward_min: float, gap_threshold: float = 2.0,
                             min_cycles: int = 3) -> bool:
    """Amber cheap window keeps being forecast as upcoming but never arrives.

    Fires when ALL of the last `min_cycles` records (including now) show:
      - a cheaper window was forecast (forward_min < price - gap_threshold), AND
      - the cheap window hadn't actually arrived yet (actual price was still above
        current_forward_min + gap_threshold, i.e. we never got there)

    If the window were real it would have arrived by now. If it keeps being 1–2h away
    each cycle, the forecast is sliding and the agent should stop deferring.
    """
    if len(recent_records) < min_cycles - 1:
        return False

    # Check current cycle first
    if current_forward_min >= current_price - gap_threshold:
        return False  # no meaningful cheap window forecast right now

    # Check the last (min_cycles - 1) records
    consecutive = 0
    for r in reversed(recent_records):
        ctx = r.get("computed_context") or {}
        rec_price       = r.get("price_c")
        rec_forward_min = ctx.get("forward_min_c")
        if rec_price is None or rec_forward_min is None:
            break
        # Was a cheap window forecast in this record?
        if rec_forward_min >= rec_price - gap_threshold:
            break  # cheap window wasn't forecast that cycle — stop counting
        # Did the cheap window actually arrive? (actual price dropped close to forward_min)
        if rec_price <= rec_forward_min + gap_threshold:
            break  # price reached the cheap band — window arrived, not sliding
        consecutive += 1
        if consecutive >= min_cycles - 1:
            return True
    return False


SOLAR_START_HOUR = 9  # flat roof in Sydney: panels don't produce meaningfully before ~9am

def _detect_zero_solar(recent_records: list[dict], current_solar_kw: float,
                       now_h: float, sensor_unavailable: bool = False) -> bool:
    """0 kW actual in 2+ of the last 3 daylight cycles (incl. now) → zero-solar day.
    Only active from SOLAR_START_HOUR onward — before then, zero output is expected
    (low sun angle) and must not be counted as evidence of a zero-solar day.
    sensor_unavailable=True means HA returned "unavailable" — don't count as a zero cycle."""
    if now_h < SOLAR_START_HOUR:
        return False
    if sensor_unavailable:
        zeros = 0  # can't count a missing reading as evidence of zero generation
    else:
        zeros = 1 if current_solar_kw <= 0.1 else 0
    for r in recent_records[-3:]:
        ts_hour = int(r.get("ts", "T00")[11:13] or 0) if r.get("ts") else 0
        if ts_hour >= SOLAR_START_HOUR and (r.get("solar_current_kw") or 0) <= 0.1:
            zeros += 1
    return zeros >= 2


def load_price_history(days: int = PRICE_HISTORY_DAYS) -> list[float]:
    """Read price_c values from the last N days of JSONL records.
    Returns a sorted list of floats (¢/kWh). Returns [] if file missing."""
    if not JSONL_FILE.exists():
        return []
    cutoff = datetime.now(SYDNEY_TZ) - timedelta(days=days)
    prices = []
    try:
        for line in JSONL_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("type") == "daily_accuracy":
                continue
            price = r.get("price_c")
            ts    = r.get("ts", "")
            if price is None or not ts:
                continue
            try:
                # Parse timestamp — may have timezone offset
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = SYDNEY_TZ.localize(dt)
                if dt >= cutoff:
                    prices.append(float(price))
            except (ValueError, TypeError):
                continue
    except Exception:
        return []
    return sorted(prices)


def _price_stats(prices: list[float]):
    """Compute p25 / p75 from a sorted price list. Returns None if too few records."""
    if len(prices) < MIN_HISTORY_RECORDS:
        return None
    n    = len(prices)
    p25  = prices[int(n * 0.25)]
    p75  = prices[int(n * 0.75)]
    pmin = prices[0]
    pmax = prices[-1]
    return {"p25": p25, "p75": p75, "pmin": pmin, "pmax": pmax, "n": n}


def _build_hourly_price_model() -> dict[int, float]:
    """Per-hour-of-day median price from the last PRICE_HISTORY_DAYS of decisions.jsonl.
    Returns {hour: median_price_c}; only hours with ≥3 samples are included.
    Used to extend the LP forecast horizon beyond Amber's ~6h window."""
    if not JSONL_FILE.exists():
        return {}
    cutoff = datetime.now(SYDNEY_TZ) - timedelta(days=PRICE_HISTORY_DAYS)
    by_hour: dict[int, list[float]] = {}
    try:
        for line in JSONL_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("type") == "daily_accuracy":
                continue
            price = r.get("price_c")
            ts    = r.get("ts", "")
            if price is None or not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = SYDNEY_TZ.localize(dt)
                if dt >= cutoff:
                    h = dt.hour
                    by_hour.setdefault(h, []).append(float(price))
            except (ValueError, TypeError):
                continue
    except Exception:
        return {}
    result = {}
    for h, p_list in by_hour.items():
        if len(p_list) >= 3:
            s = sorted(p_list)
            result[h] = s[len(s) // 2]
    return result


def _extend_forecast_to_demand_window(
    price_forecast: list[dict],
    now: datetime,
    hourly_model: dict[int, float],
) -> list[dict]:
    """Extend the Amber ~6h price forecast with synthetic 30-min slots until 22:00.

    Fills the 15:00–21:00 demand-window block so the LP can see the
    demand_penalty_c on those slots and pre-charge earlier in the day.
    Uses per-hour historical medians from the last 7 days; falls back to
    p75 of the existing forecast for hours with insufficient history.
    """
    if not price_forecast:
        return price_forecast
    last_time_str = price_forecast[-1].get("time", "")
    try:
        last_dt = datetime.fromisoformat(last_time_str.replace("T", " "))
        if last_dt.tzinfo is None:
            last_dt = SYDNEY_TZ.localize(last_dt)
    except (ValueError, TypeError):
        return price_forecast
    target_end = last_dt.replace(hour=22, minute=0, second=0, microsecond=0)
    if target_end <= last_dt:
        return price_forecast  # already reaches 22:00+
    existing_prices = sorted(f.get("cents_kwh", 15.0) for f in price_forecast)
    fallback = existing_prices[int(len(existing_prices) * 0.75)] if existing_prices else 15.0
    extended = list(price_forecast)
    slot_dt = last_dt + timedelta(minutes=30)
    while slot_dt <= target_end:
        h = slot_dt.hour
        extended.append({
            "time": slot_dt.strftime("%Y-%m-%d %H:%M"),
            "cents_kwh": hourly_model.get(h, fallback),
            "descriptor": "synthetic_historical",
        })
        slot_dt += timedelta(minutes=30)
    return extended


def _historical_grid_target(soc: float, solar_forecast_kwh: float,
                             confidence: float, p_now: float,
                             stats: dict, max_floor: float = DEFAULT_MAX_INSURANCE_FLOOR) -> float:
    """Compute grid charge target using rolling historical price percentiles.

    Two components combined with max():

    1. Solar-adjusted target: discount solar trust when prices are cheap
       (low cost of over-charging → be more aggressive from grid).
    2. Insurance floor: maintain a minimum SoC proportional to price cheapness,
       guarding against the cheap window closing earlier than forecast.

    price_position = 0.0  →  P_now at or below p25 (very cheap by recent standards)
    price_position = 1.0  →  P_now at or above p75 (normal/expensive)
    """
    swing = stats["p75"] - stats["p25"]
    if swing < 2.0:
        # Flat price history — no meaningful percentile signal, fall back
        return None
    price_position = max(0.0, min(1.0, (p_now - stats["p25"]) / swing))

    # 1. Solar trust: scale solar contribution by price_position.
    #    At price_position=0 (very cheap) → trust solar 0% (fill from grid).
    #    At price_position=1 (normal)     → trust solar fully.
    solar_trusted_kwh = solar_forecast_kwh * confidence * price_position
    solar_target = max(5.0, min(95.0, 95.0 - (solar_trusted_kwh / USABLE_KWH * 100)))

    # 2. Insurance floor: decreases as price rises toward p75.
    insurance_floor = max_floor * (1.0 - price_position)

    target = max(solar_target, insurance_floor)
    return round(max(soc, min(95.0, target)), 1)


def compute_decision_context(state: dict, price_forecast: list[dict],
                             recent_records: list[dict], now: datetime) -> dict:
    """Deterministic decision context + recommended verdict. Pure function."""
    now_h    = now.hour + now.minute / 60
    battery  = state.get("battery", {})
    grid     = state.get("grid", {})
    solar    = state.get("solar", {})
    settings = state.get("settings", {})

    # ALWAYS the Tessie reading — never the floor-clipped gateway value.
    soc          = battery.get("soc_pct", 0) or 0
    grid_target  = battery.get("grid_target_pct", 5) or 5
    price        = grid.get("price_cents_kwh", 0.0) or 0.0
    fit_price    = grid.get("fit_price_cents_kwh", 0.0) or 0.0
    is_peak      = state.get("is_peak_month", False)
    in_demand    = state.get("in_demand_window", False)
    in_sponge    = state.get("in_solar_sponge", False)
    solar_now          = solar.get("current_kw", 0.0) or 0.0
    solar_unavailable  = solar.get("sensor_unavailable", False)
    remaining          = solar.get("forecast_remaining_kwh", 0.0) or 0.0
    accuracy           = _accuracy_class(solar.get("forecast_accuracy", ""))

    zero_solar_day    = _detect_zero_solar(recent_records, solar_now, now_h, solar_unavailable)
    deferral_detected = _detect_deferral(recent_records, price)

    # Overnight hold: Solar Sponge (10am–3pm) is structurally cheaper than overnight
    # prices. Don't charge overnight when Solar Sponge tomorrow will be cheaper.
    # Fires when: nighttime (20:00–07:00) AND price > SOLAR_SPONGE_PRICE_THRESHOLD
    # AND SoC is not critically low (> 25% — emergency automation handles the floor).
    # Peak months: also apply — Rule 13 morning deadline maths will escalate if needed.
    is_night = now_h >= 20 or now_h < 7
    overnight_hold = (is_night
                      and price > SOLAR_SPONGE_PRICE_THRESHOLD
                      and soc > 25)
    # Accuracy-based unreliability only valid from SOLAR_START_HOUR — before then,
    # Solcast forecasts >0 but panels haven't started yet (low sun angle, flat roof).
    # zero_solar_day has its own time guard via _detect_zero_solar.
    solar_unreliable  = (accuracy in ("poor", "unreliable") and now_h >= SOLAR_START_HOUR) or zero_solar_day

    # Confidence factor for solar forecast
    confidence_factor = {"good": 1.0, "poor": 0.5, "unreliable": 0.0, "na": 1.0}.get(accuracy, 1.0)
    if zero_solar_day:
        confidence_factor = 0.0

    # ---- Cost target: historical price model (if enabled + sufficient data) ----
    price_stats_data = settings.get("price_stats")   # injected by run_agent() each cycle
    max_floor        = settings.get("max_insurance_floor_pct", DEFAULT_MAX_INSURANCE_FLOOR)
    cost_target_method = "legacy"

    if HISTORICAL_PRICE_MODEL and price_stats_data and not is_peak:
        hist_target = _historical_grid_target(
            soc, remaining, confidence_factor, price, price_stats_data, max_floor)
        if hist_target is not None:
            cost_target = hist_target
            cost_target_method = "historical"
        else:
            # Insufficient price swing in history — fall back
            cost_target = 85 if solar_unreliable and now_h < 12 else (
                70 if solar_unreliable and now_h < 14 else (
                50 if solar_unreliable else grid_target))
    else:
        # Legacy: time-based substitute when solar unreliable; grid_target otherwise
        # (Also used for peak months — demand deadline logic overrides cost_target anyway)
        if solar_unreliable:
            cost_target = 85 if now_h < 12 else 70 if now_h < 14 else 50
        else:
            cost_target = grid_target

    # Expected solar contribution to the demand deadline (0 if solar can't be trusted)
    expected_solar = 0.0 if solar_unreliable else remaining

    # Deadline maths
    hours_to_cheap_end = _hours_to_cheap_end(price_forecast, price)
    hours_to_2_55      = max(DEMAND_DEADLINE - now_h, 0.0)
    hours_to_deadline  = min(hours_to_2_55, hours_to_cheap_end) if is_peak else hours_to_cheap_end

    # Net solar available for battery = gross remaining minus home load consumed over the window.
    # Solar goes to loads first; only the surplus reaches the battery.
    # Peak: window is hours until 2:55pm. Non-peak: cap at 7h (full solar day).
    home_load_kw = state.get("home_load_kw", 0.5) or 0.5
    _solar_window_h = hours_to_2_55 if is_peak else min(hours_to_deadline, 7.0)
    net_expected_solar = max(expected_solar - home_load_kw * _solar_window_h, 0.0)

    # Peak-month demand fill maths (toward 85% by 2:55pm)
    # Uses net solar so we don't mistakenly hold when home load will consume most of the forecast.
    kwh_needed_85   = max((0.85 - soc / 100) * USABLE_KWH - net_expected_solar, 0.0)
    fill_slow_85    = kwh_needed_85 / SLOW_KW
    fill_fast_85    = kwh_needed_85 / FAST_KW

    # Cost-target fill maths
    kwh_needed   = max((cost_target / 100 - soc / 100) * USABLE_KWH, 0.0)
    fill_slow    = kwh_needed / SLOW_KW
    fill_fast    = kwh_needed / FAST_KW

    # Non-peak: will solar alone (net of home load) cover the gap to cost_target?
    # Only fires before 1pm when solar still has time to deliver, and when forecast is reliable.
    _kwh_gap = max((cost_target / 100 - soc / 100) * USABLE_KWH, 0.0)
    solar_can_cover = (
        not solar_unreliable
        and not is_peak
        and now_h < 13
        and _kwh_gap > 0
        and net_expected_solar >= _kwh_gap
    )

    # Spread: most expensive upcoming slot (next 6h) vs current price
    upcoming       = [f.get("cents_kwh", 0.0) for f in price_forecast[:12]]
    next_expensive = max(upcoming) if upcoming else price
    spread         = next_expensive - price
    # Cheapest price in the full forecast horizon — used to suppress deferral_limit when a
    # genuinely cheaper window is approaching (may be >6h away, e.g. overnight wait for Solar Sponge).
    all_prices  = [f.get("cents_kwh", 0.0) for f in price_forecast]
    forward_min = min(all_prices) if all_prices else price

    # Sliding forecast: cheap window forecast for 3+ consecutive cycles but never arrives.
    sliding_forecast = _detect_sliding_forecast(recent_records, price, forward_min)

    # ---- EV verdict ----
    ev              = state.get("ev", {})
    ev_plugged      = ev.get("plugged_in", False)
    ev_soc          = ev.get("ev_soc_pct") or 0
    ev_min          = ev.get("min_soc_pct") or 20
    ev_target       = ev.get("charge_target_pct") or 80

    ultra_cheap_c    = settings.get("ev_ultra_cheap_c", 5)
    standard_price_c = settings.get("ev_standard_price_c", 10)
    min_charge_price_c = settings.get("ev_min_charge_price_c", 20)

    if not ev_plugged:
        ev_rec = {"zappi_mode": "n/a", "rule_fired": "ev_disconnected"}
    elif in_demand:
        # Never pull from grid during demand window — solar-only via Eco+
        ev_rec = {"zappi_mode": "Eco+", "rule_fired": "ev_demand_window"}
    elif ev_soc < ev_min and price < min_charge_price_c:
        # Below minimum SoC — charge regardless of price (up to 20¢)
        ev_rec = {"zappi_mode": "Fast", "rule_fired": "ev_case3_below_minimum"}
    elif fit_price < 0 and soc >= 85 and ev_soc < 100:
        # FIT negative: absorb solar surplus into EV rather than paying to export
        ev_rec = {"zappi_mode": "Eco+", "rule_fired": "ev_case6_negative_fit_solar_dump"}
    elif ev_soc >= ev_target:
        ev_rec = {"zappi_mode": "Eco+", "rule_fired": "ev_target_met"}
    elif price < ultra_cheap_c:
        # Price below ultra-cheap threshold — charge fast
        ev_rec = {"zappi_mode": "Fast", "rule_fired": "ev_ultra_cheap"}
    elif price < standard_price_c:
        # Price below standard threshold — charge slowly (Eco: grid+solar, no battery discharge)
        ev_rec = {"zappi_mode": "Eco", "rule_fired": "ev_standard_price"}
    else:
        # Price too high — solar-only via Eco+
        ev_rec = {"zappi_mode": "Eco+", "rule_fired": "ev_price_too_high"}

    # ---- Battery verdict — ordered decision tree, first match wins ----
    def verdict(action, target, mode, rule):
        return {"action": action, "target_pct": target, "mode": mode, "rule_fired": rule}

    if in_demand:
        rec = verdict("hold", None, None, "demand_window_active")
    elif is_peak and now_h < DEMAND_DEADLINE and soc < 85:
        # Peak-month hard deadline escalation (Rule 13)
        if kwh_needed_85 <= 0:
            # net solar (after home load) covers the remaining gap — no grid charge needed yet.
            # "peak_target_met" only when SoC has actually reached 85%.
            rule_name = "peak_target_met" if soc >= 85 else "peak_solar_will_cover"
            rec = verdict("hold", None, None, rule_name)
        elif fill_fast_85 >= hours_to_2_55 or fill_slow_85 >= hours_to_2_55:
            rec = verdict("charge", 100, "autonomous", "peak_deadline_autonomous")
        elif (now_h >= 12.5 and soc < 40) or (now_h >= 13.5 and soc < 70):
            rec = verdict("charge", 100, "autonomous", "peak_deadline_quickcheck")
        elif in_sponge and now_h < 13 and soc < 50:
            rec = verdict("charge", max(50, cost_target), "self_consumption", "solar_sponge_floor")
        elif in_sponge and kwh_needed_85 > 0:
            # In Solar Sponge with grid charge still needed. Receding-horizon rate choice:
            # use autonomous (5kW) only when self_consumption won't fit the deadline;
            # otherwise self_consumption is fine — next cycle will recalculate with fresher
            # solar and price data and may downgrade further if solar improves.
            if fill_slow_85 >= hours_to_2_55 - 1.0:
                rec = verdict("charge", 85, "autonomous", "peak_sponge_go_hard")
            else:
                rec = verdict("charge", 85, "self_consumption", "peak_sponge_selfcons")
        elif kwh_needed_85 > 0:
            # Not in Solar Sponge yet. Check for a cheaper go-hard slot FIRST — waiting for
            # a cheaper fast-fill slot beats starting self_consumption now, as long as the
            # deadline is still achievable via autonomous at that cheaper price.
            best = _cheapest_go_hard_slot(
                price_forecast, price, soc, home_load_kw, hours_to_2_55
            )
            if best is not None:
                # Cheaper feasible slot exists — hold, charge fast there instead.
                rec = verdict("hold", None, None, "wait_for_cheap_go_hard")
            elif fill_slow_85 >= hours_to_2_55 - 1.0:
                # No cheaper slot AND self_consumption is getting tight — must start now.
                rec = verdict("charge", 85, "self_consumption", "peak_deadline_selfcons")
            else:
                # No cheaper slot; current price is as good as it gets. Charge at self_consumption.
                rec = verdict("charge", 85, "self_consumption", "peak_charge_now")
        else:
            rec = verdict("hold", None, None, "peak_on_track")
    elif in_sponge and now_h < 13 and soc < 50:
        # Rule 14 — Solar Sponge minimum floor
        rec = verdict("charge", max(50, cost_target), "self_consumption", "solar_sponge_floor")
    elif cost_target <= soc:
        rec = verdict("hold", None, None, "target_met")
    elif solar_can_cover:
        # Solar forecast (net of home load) will cover the gap — hold, don't trickle from grid.
        # Escalation logic below will fire if solar underdelivers as the day progresses.
        rec = verdict("hold", None, None, "solar_will_cover")
    elif overnight_hold:
        # Solar Sponge tomorrow will be cheaper — don't charge overnight at high prices.
        # Rule 13 morning deadline maths will escalate if peak month demands it.
        rec = verdict("hold", None, None, "overnight_hold_wait_for_sponge")
    elif deferral_detected and (forward_min >= price - 2.0 or sliding_forecast):
        # Fire when: no meaningfully cheaper window is incoming (forward_min within 2¢),
        # OR the forecast has been sliding for 3+ cycles (cheap window keeps moving forward
        # but never arrives — forecast is unreliable, stop deferring and charge now).
        rule = "sliding_forecast" if sliding_forecast else "deferral_limit"
        rec = verdict("charge", cost_target, "self_consumption", rule)
    # When solar is unreliable, we can't count on it supplementing self_consumption —
    # apply a tighter buffer (1.5h instead of 0.5h) to escalate to autonomous sooner.
    elif fill_fast >= hours_to_deadline - 0.5:
        rec = verdict("charge", cost_target, "autonomous", "nonpeak_deadline_autonomous")
    elif solar_unreliable and fill_slow >= hours_to_deadline - 1.5:
        rec = verdict("charge", cost_target, "autonomous", "nonpeak_solar_unreliable_autonomous")
    elif fill_slow >= hours_to_deadline - 0.5:
        rec = verdict("charge", cost_target, "self_consumption", "nonpeak_deadline_selfcons")
    else:
        # Spread table — window still viable, decide on economics
        if spread < 5:
            rec = verdict("hold", None, None, "spread_too_small")
        elif spread > 15 and (cost_target - soc) > 15:
            rec = verdict("charge", cost_target, "autonomous", "spread_arbitrage")
        else:
            rec = verdict("charge", cost_target, "self_consumption", "spread_selfcons")

    return {
        "now_h":               round(now_h, 2),
        "soc":                 soc,
        "is_peak_month":       is_peak,
        "accuracy_class":      accuracy,
        "zero_solar_day":      zero_solar_day,
        "deferral_detected":   deferral_detected,
        "sliding_forecast":    sliding_forecast,
        "overnight_hold":      overnight_hold,
        "solar_unreliable":    solar_unreliable,
        "cost_target_pct":     cost_target,
        "cost_target_method":  cost_target_method,
        "expected_solar_kwh":      round(expected_solar, 2),
        "net_expected_solar_kwh":  round(net_expected_solar, 2),
        "solar_can_cover":         solar_can_cover,
        "hours_to_cheap_end":  round(hours_to_cheap_end, 2),
        "hours_to_2_55pm":     round(hours_to_2_55, 2),
        "hours_to_deadline":   round(hours_to_deadline, 2),
        "kwh_needed_85":       round(kwh_needed_85, 2),
        "fill_slow_85_h":      round(fill_slow_85, 2),
        "fill_fast_85_h":      round(fill_fast_85, 2),
        "kwh_needed":          round(kwh_needed, 2),
        "fill_slow_h":         round(fill_slow, 2),
        "fill_fast_h":         round(fill_fast, 2),
        "spread_c":            round(spread, 1),
        "forward_min_c":       round(forward_min, 1),
        "fit_price_c":         round(fit_price, 1),
        "go_hard_slot":        (lambda b: {"price_c": round(b[0], 1), "hours_until": round(b[1], 1)}
                                if b else None)(
                                    _cheapest_go_hard_slot(price_forecast, price, soc, home_load_kw, hours_to_2_55)
                                    if is_peak and kwh_needed_85 > 0 else None
                                ),
        "recommended":         rec,
        "ev_recommended":      ev_rec,
    }


def _format_decision_context(ctx: dict) -> str:
    """Render the decision context as a REFERENCE block for the prompt (non-authoritative)."""
    r = ctx["recommended"]
    return (
        "## Deterministic decision helper — REFERENCE ONLY (you are still the decision-maker)\n"
        "A pure-Python helper precomputed the figures below from the same state and price\n"
        "forecast you can read via tools. Use it to sanity-check your reasoning. You may\n"
        "disagree — but if you do, state why in your log_decision summary.\n"
        f"  SoC (true/Tessie): {ctx['soc']}%   peak_month: {ctx['is_peak_month']}   now: {ctx['now_h']}h\n"
        f"  zero_solar_day: {ctx['zero_solar_day']}   deferral_detected: {ctx['deferral_detected']}   "
        f"sliding_forecast: {ctx['sliding_forecast']}   overnight_hold: {ctx['overnight_hold']}   "
        f"solar_unreliable: {ctx['solar_unreliable']} (accuracy: {ctx['accuracy_class']})\n"
        f"  cost_target: {ctx['cost_target_pct']}% ({ctx['cost_target_method']})   hours_to_cheap_end: {ctx['hours_to_cheap_end']}h   "
        f"hours_to_deadline: {ctx['hours_to_deadline']}h   spread: {ctx['spread_c']}¢\n"
        f"  to cost_target: need {ctx['kwh_needed']}kWh — self_consumption {ctx['fill_slow_h']}h / "
        f"autonomous {ctx['fill_fast_h']}h\n"
        f"  to 85% by 2:55pm: need {ctx['kwh_needed_85']}kWh — self_consumption {ctx['fill_slow_85_h']}h / "
        f"autonomous {ctx['fill_fast_85_h']}h\n"
        + (f"  go_hard_slot: cheapest feasible slot at {ctx['go_hard_slot']['price_c']}¢ in "
           f"{ctx['go_hard_slot']['hours_until']}h — wait then autonomous\n"
           if ctx.get('go_hard_slot') else "")
        + f"  >>> BATTERY: {r['action']} target={r['target_pct']}% mode={r['mode']} (rule: {r['rule_fired']})\n"
        f"  fit_price: {round(ctx.get('fit_price_c', 0) or 0, 1)}¢   "
        f"  >>> EV: zappi={ctx['ev_recommended']['zappi_mode']} (rule: {ctx['ev_recommended']['rule_fired']})"
    )


# ---------------------------------------------------------------------------
# Tool definitions (schema passed to Claude)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "get_current_state",
        "description": (
            "Read current energy system state: battery SoC, mode, reserve, grid price, "
            "solar generation, EV status, home load, and time context (peak month, "
            "demand window, solar sponge window). Call this first every cycle."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_price_forecast",
        "description": "Get Amber electricity price forecast for the next 12 hours (30-min intervals). Use this to decide whether to charge now or wait for a cheaper window.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_solar_forecast",
        "description": "Get Solcast solar generation forecast for the rest of today (30-min intervals, kW estimate). Use this to decide how much grid charge the battery needs before solar takes over.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_weather_forecast",
        "description": (
            "Get hourly cloud cover, solar radiation (W/m²), and rain probability from Open-Meteo "
            "for today's remaining solar hours and all of tomorrow. "
            "Use this to assess tomorrow's solar quality — especially at overnight cycles when deciding "
            "whether to pre-charge tonight. Also use to cross-check Solcast accuracy: if radiation_wm2 "
            "is high but Solcast shows unreliable, the problem may be temporary rather than all-day. "
            "Returns a tomorrow_solar_outlook summary (good/poor/overcast) for quick reasoning."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_powerwall_reserve",
        "description": (
            "Set Powerwall backup reserve % (floor SoC battery won't discharge below). "
            "Normal discharge: 5%. Charge toward full: 100%. "
            "Demand window protection: set to match the % needed to cover 3–9pm load."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "percent": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Reserve percentage (0–100)",
                }
            },
            "required": ["percent"],
        },
    },
    {
        "name": "set_powerwall_mode",
        "description": (
            "Set Powerwall operating mode. "
            "'self_consumption': charges from grid at a variable rate (typically 0.2–2.5 kW, read battery.charge_rate_kw) ONLY when backup_reserve_percent >"
            "current_soc. If reserve ≤ soc, battery charges from solar surplus only — no grid draw. "
            "Use for long cheap windows (3h+) or when spread doesn't justify urgency. "
            "'autonomous': fast ~5 kW grid charge. ALWAYS pair with set_powerwall_reserve(100) — "
            "this is the export guard. A HA safety net also reverts to self_consumption within 30s "
            "if export is detected, so autonomous is safe. "
            "Use autonomous when: (1) price spread > 8¢ AND need >15% AND window short (<2h); "
            "(2) peak month deadline pressure (fill_fast_85_h close to hours_to_deadline); "
            "(3) peak month + in Solar Sponge + grid charge still needed (go_hard_at_sponge strategy — "
            "fill fast at the cheapest window rather than trickling slowly through it). "
            "A 4¢ spread does NOT justify autonomous outside Solar Sponge."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["self_consumption", "autonomous"]}
            },
            "required": ["mode"],
        },
    },
    {
        "name": "set_zappi_mode",
        "description": (
            "Set Zappi EV charger mode. "
            "'Eco+': charge only from solar export past the meter — safe default, "
            "battery never powers EV. "
            "'Fast': charge from grid at full rate (~7 kW). "
            "'Off': stop charging."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["Eco+", "Eco", "Fast", "Off"]}
            },
            "required": ["mode"],
        },
    },
    {
        "name": "log_decision",
        "description": "Log your reasoning and any actions taken. Always call this last, even if you took no action.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "1–3 sentences explaining the situation and your reasoning.",
                },
                "actions_taken": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of actions taken, e.g. ['set_reserve(62%)', 'set_zappi(Eco+']. Empty list if no action.",
                },
                "ev_summary": {
                    "type": "string",
                    "description": "1–2 sentences covering EV status only: plug state, EV SoC, Zappi mode set and why. If EV is disconnected, say so briefly.",
                },
            },
            "required": ["summary", "actions_taken"],
        },
        "cache_control": {"type": "ephemeral"},  # cache all tool definitions up to here
    },
]

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
You are the energy optimisation agent for a residential battery system in Glebe, Sydney, Australia.

## Hardware
- Tesla Powerwall 2: 13.5 kWh usable, ~5 kW charge/discharge
- SolarEdge inverter: ~5 kW peak (6.12 kWp, flat roof)
- Polestar 4 EV (~100 kWh) via Zappi 2 charger
- Tariff: Amber Electric dynamic spot pricing, Ausgrid EA116

## Objectives — in strict priority order

1. DEMAND WINDOW — ABSOLUTE CONSTRAINT
   Peak months (Nov, Dec, Jan, Feb, Mar, Jun, Jul, Aug): ZERO grid import allowed 3–9pm.
   Any import during this window sets the monthly demand charge (~$80–120 extra).

   TARGET: battery must be at 85%+ SoC by 2:55pm on peak month days.
   Work backwards from this target every morning:
   - Estimate solar contribution for the day (from solar forecast)
   - If solar alone won't get battery to 85% by 2:55pm, charge from grid — even at
     mediocre prices (17–18¢). The demand charge risk (~$100/month) outweighs paying
     an extra 5¢/kWh on a few kWh of grid charge.
   - On rainy/cloudy days (solar forecast < 8 kWh), assume solar won't cover the gap.
     Begin charging by 10am using autonomous mode if needed to reach target by 2:55pm.
   - Do not wait for a cheaper window that may not arrive. If it's 11am and battery is
     at 40%, start charging now — you may not have enough time at 1.7kW otherwise.
   - In non-peak months (Apr, May, Sep, Oct): this constraint does not apply.
     Let the battery discharge freely, optimise purely for cost.

2. EV NEVER FROM BATTERY
   Zappi default is Eco+ (charges only from actual solar export — battery never discharged for EV).

   Read ev.min_soc_pct, ev.charge_target_pct, settings.ev_ultra_cheap_c, settings.ev_standard_price_c,
   settings.ev_min_charge_price_c each cycle — user-set sliders.
   Defaults: min=20%, target=80%, ultra_cheap=5¢, standard=10¢, min_charge_ceiling=20¢.

   Check in order, first match wins:

   Demand window (3–9pm peak months): always Eco+ — no grid draw during demand window, ever.

   Case 3: EV SoC < [min] AND price < ev_min_charge_price_c → Fast (EV below minimum — override price gate)

   Case 6: FIT price < 0¢ AND battery SoC ≥ 85% AND EV SoC < 100%
            → Eco+ (absorbs solar surplus into EV rather than paying to export; no grid draw)

   Target met (EV SoC ≥ [target]): Eco+ — catches free solar overflow only.

   Price < ev_ultra_cheap_c: Fast — charge hard, price is exceptional.

   Price < ev_standard_price_c: Eco — charge slowly from grid+solar, price is acceptable.

   Otherwise: Eco+ — price too high, solar-only.

EV SCHEDULE (read ev.schedule each cycle):
   ev.schedule.active = false → no deadline, use Cases 2–5 above as normal.
   ev.schedule.active = true → a departure deadline is set. Additional logic:

   Compute ev_kwh_needed = (departure_target_pct − ev.ev_soc_pct) / 100 × 100 kWh (Polestar 4 battery)
   Zappi Fast rate ≈ 7.2 kW (32A, single phase). fill_fast_h = ev_kwh_needed / 7.2
   Zappi Eco+ rate = depends on solar export; treat as 0 kW guaranteed for deadline maths.

   Deadline rules (checked AFTER the "EV NEVER FROM BATTERY" constraint):
   - If ev.ev_soc_pct ≥ departure_target_pct: target met, no deadline action needed. Eco+ or Off.
   - If fill_fast_h ≥ hours_to_departure − 0.5: URGENT — switch to Fast immediately regardless of
     price (missing the departure target is worse than paying a few extra cents).
   - If fill_fast_h < hours_to_departure × 0.5: plenty of time — stay on Eco+ unless a Fast Case
     (2–5) applies independently.
   - Otherwise: moderate urgency — use Fast if price < 20¢, else Eco+ and re-evaluate next cycle.

   Always state hours_to_departure and fill_fast_h in ev_summary so the user can sanity-check.

3. MINIMISE COST
   - Charge battery and EV when price is cheapest; discharge when expensive.
   - Don't charge at a mediocre price if a cheaper window is coming within 3–4 hours.
   - "Cheap" thresholds: < 10¢ = very cheap, 10–15¢ = cheap, 15–20¢ = mediocre, > 20¢ = expensive.
   - Flat-then-spike days: if the price forecast shows no window more than 3¢ cheaper than
     the current price before a spike, treat current price as the charge window — don't hold
     for a cheaper window that isn't coming. Check the full forecast: if min(prices before spike)
     ≥ current price − 3¢, charge now at current price rather than waiting.
   - Deadline-aware charging — run this calculation every cycle when a spike is visible:
     1. target_soc = grid_target_pct (or time-based substitute if forecast is poor/unreliable)
     2. kWh_needed = (target_soc − current_soc) / 100 × 13.5
     3. hours_to_fill_slow = kWh_needed / actual_charge_rate_kw
        Use battery.charge_rate_kw (actual live rate from HA sensor) if it is > 0.3 kW —
        Tesla firmware varies widely (0.2–2.5 kW in self_consumption); never assume 1.7 kW.
        If charge_rate_kw ≤ 0.3 kW or battery is not currently charging, use 1.7 kW as the
        planning estimate (it will charge faster once reserve is raised).
        hours_to_fill_fast = kWh_needed / 5.0   (autonomous rate)
     4. hours_to_cheap_end — find the effective deadline by scanning the price forecast:
        Scan forward through the 30-min forecast intervals. Find the first interval where:
          price ≥ current_price + 4¢  AND  the following interval is also ≥ current_price + 4¢
        This is a "sustained rise" — two consecutive elevated intervals, not a single blip.
        hours_to_cheap_end = hours from now until that first elevated interval starts.
        - Non-peak months: use hours_to_cheap_end as your deadline
        - Peak months: use min(hours_to_2:55pm, hours_to_cheap_end) — demand window is hard
        - If no sustained rise found in the full forecast horizon: use 6h (end of forecast window)
        Why ≥ current_price + 4¢? A 1–2¢ drift is noise. A 4¢+ sustained move is a real window ending.
        This replaces a fixed spike threshold — it adapts to whatever the actual price shape looks like
        today: whether cheap ends at 3pm, 4pm, or 6pm, the deadline calculation is always accurate.
     5. Mode decision:
        - hours_to_cheap_end > hours_to_fill_slow + 1.5h → window still viable, evaluate spread normally
        - hours_to_cheap_end ≤ hours_to_fill_slow + 1.5h → start self_consumption NOW, no time to wait
        - hours_to_cheap_end ≤ hours_to_fill_fast + 0.5h → start autonomous NOW, only fast is fast enough
     6. Once charging has started, re-run every cycle. If self_consumption is falling behind
        (hours_to_cheap_end ≤ hours_to_fill_slow + 0.5h), escalate to autonomous automatically.
        This means: start slow and cheap, escalate to fast only when the maths demands it.
     Example (non-peak, mid-morning):
       36% SoC, 80% target, now 10:30am. Price now 15¢. Forecast: 15¢ until 4pm then rises to 20¢+.
       hours_to_cheap_end = 5.5h (to 4pm, first two intervals ≥ 15+4 = 19¢)
       kWh_needed = 5.9, hours_to_fill_slow = 3.5h, hours_to_fill_fast = 1.2h
       5.5h > 3.5 + 1.5 = 5.0h? Yes → evaluate spread; 5¢ spread → self_consumption, monitor
       At 1:30pm, battery at 50%: kWh_needed=4.1, hours_to_fill_slow=2.4h, hours_to_cheap_end=2.5h
         2.5 ≤ 2.4 + 1.5 → start self_consumption NOW (or already running)
       At 2:30pm, battery at 55%: kWh_needed=3.4, hours_to_fill_slow=2.0h, hours_to_cheap_end=1.5h
         1.5 ≤ 2.0 + 0.5 → escalate to autonomous

4. SOLAR UTILISATION
   - Battery should have enough headroom to absorb forecast solar.
   - Grid charge target = how much SoC is needed so solar covers the rest of the day.
   - Solar Sponge 10am–3pm: cheapest import, but export during this window is penalised.
     Don't export during Solar Sponge — let solar charge battery instead.

## Operating constraints
- Only control: backup_reserve_percent and mode (via Tessie API)

**CRITICAL — which SoC reading to trust:**
The state gives you two battery readings:
- `soc_pct` — the SoC value used for all decisions. Normally this is the Tessie cloud poll
  (true SoC), but if Tessie returned an implausible value (0%, or far below the gateway when
  gateway is reliable), the agent has already substituted the gateway reading and set
  `tessie_soc_failed: true`. In that case `soc_pct` IS the gateway reading — use it normally.
- `soc_tessie_pct` — raw Tessie value (may be wrong if tessie_soc_failed is true).
- `soc_gateway_pct` — local Powerwall gateway, FLOOR-CLIPPED at reserve level when reserve > SoC.

If `tessie_soc_failed: true`: note it in your summary but proceed using `soc_pct` as usual.
Never use `soc_gateway_pct` to judge whether a charge target has been met — it will lie upward
whenever reserve > true SoC. Declaring the demand-window target "achieved" off the gateway
reading is a Rule-2-violation trap: you would drop reserve and enter the 3-9pm window under-filled.

**CRITICAL — how grid charging actually works:**
Grid charging ONLY occurs when `backup_reserve_percent > current_soc`. This is the trigger.
- If reserve = 5% and battery = 62%: NO grid draw. Battery charges from solar surplus only.
- If reserve = 80% and battery = 62%: grid draw starts immediately at ~1.7 kW (self_consumption)
  or ~5 kW (autonomous) until battery reaches 80%.

To charge from grid: call `set_powerwall_reserve(target_pct)` where target_pct > current_soc.
"System is in self_consumption mode" alone does NOT mean grid charging is happening — only if
reserve was previously set above current SoC.

**CRITICAL — holding ≠ arming. Do NOT raise reserve while waiting for a cheaper window.**
If you decide to hold/wait (e.g. Solar Sponge is 1–2h away at a cheaper price), the correct
action is NO reserve change. Do NOT set reserve to the charge target (e.g. 85%) while waiting —
that starts charging immediately at the current price, the opposite of holding.

The only floor that matters is the Powerwall's 5% absolute floor. The 20% "emergency charge"
threshold does NOT mean the battery must stay above 20% — it is only an emergency trigger for
when the battery might hit 5% before cheap charging arrives.

While waiting for a cheaper window, compute:
  projected_soc_at_cheap_window = soc − (hours_to_window × home_load_kw / 13.5 × 100)

  projected_soc > 5%  → leave reserve at 5%. No action. Battery will survive; charge cheap.
  projected_soc ≤ 5%  → set reserve to (5% + drain_to_window + 3% buffer) — just enough to
                         survive to the cheap window, nothing more.

Example: SoC=18% at 7:45am, home load 0.6kW, Solar Sponge at 10am (2.25h away).
  projected_soc = 18 − (2.25 × 0.6 / 13.5 × 100) = 18 − 10 = 8%. Above 5% → no action.
  Leave reserve at 5%, let battery drain to 8%, charge cheaply at Solar Sponge.

Setting reserve=85% with SoC=16% is a "charge now" command, not a "get ready for later" command.

- self_consumption mode: ~1.7 kW grid charge rate when reserve > soc. Solar surplus also charges.
  Needs ~4–6h to charge 20%→80% from grid alone.
- autonomous mode: ~5 kW grid charge rate when reserve > soc (fast). Always pair with reserve=100%
  as export guard. A HA safety net also monitors for export and reverts within 30s if firmware
  misbehaves.
- Battery floor in non-peak months: 5%. Allow full discharge during day/evening.
- Battery floor in peak months: set to what's needed to cover demand window, typically 20–40%.

## Choosing between self_consumption and autonomous mode

The value of charging hard (autonomous, 5 kW) depends on the PRICE SPREAD — how much
cheaper is the current or upcoming cheap window vs the next expensive period you'd otherwise
import from.

**Spread = cheap_price_now − next_expensive_period (or current price if already expensive)**

| Spread | Action |
|--------|--------|
| < 5¢   | Don't bother charging from grid at all. Hold, let battery drain, wait for a better window. |
| 5–8¢   | self_consumption only. Only charge if window is 3h+ and SoC gap is meaningful. |
| 8–15¢  | self_consumption for long windows (3h+). Autonomous only if window < 2h AND need >15% SoC. |
| > 15¢  | Autonomous justified when gap > 15% SoC. This is real arbitrage — go hard. |

**Override: peak month demand window risk**
In peak months, the demand charge (~$100/month) outweighs the spread calculus entirely.
If battery needs to reach 85% by 2:55pm and won't get there at self_consumption rate,
use autonomous regardless of spread.

**Examples:**
- Price now 20¢, cheapest upcoming 16¢ → spread 4¢ → don't charge, hold for the window
- Price now 16¢, evening peak forecast 30¢ → spread 14¢ → self_consumption fine (long window)
- Price now 5¢ (negative window), day peak 30¢ → spread 25¢ → autonomous, fill fast
- Peak month, 10am, battery at 40%, solar unreliable → demand charge risk → autonomous now

## Time-based escalation — "enough waiting, go hard"

These rules override the spread table when time pressure is real. They are not suggestions.

**Deferral limit — when the forecast keeps being wrong:**
At the start of each cycle you receive a "Recent decisions" summary in your context.
If you see 2+ consecutive hold decisions where you were waiting for a cheaper price window
AND the current price is within 2¢ of what it was in those cycles — the forecast is wrong.
The cheap window is not arriving. Stop waiting.
Apply flat-then-spike immediately: treat current price as the charge floor and start
self_consumption toward the time-based substitute target (see Solar forecast accuracy section).
Each additional 30-minute hold costs you charging time, not money.

**PEAK MONTHS — hard deadline: 85% SoC by 2:55pm:**
Every cycle from 9am, run this calculation:
  kWh_needed = (0.85 − soc/100) × 13.5 − expected_solar_to_2:55pm
              (use 0 for solar if forecast is poor or unreliable)
  hours_to_fill_fast = kWh_needed / 5.0   (autonomous, 5 kW)
  hours_to_fill_slow = kWh_needed / 1.7   (self_consumption, 1.7 kW)
  hours_remaining    = hours until 14:55

  hours_to_fill_fast ≥ hours_remaining   → autonomous NOW (already very tight, no time to waste)
  hours_to_fill_slow ≥ hours_remaining   → autonomous NOW (self_consumption is too slow)
  hours_to_fill_slow ≥ hours_remaining − 1.0h → self_consumption NOW, stop deferring

Price spread is irrelevant for this calculation. A demand charge costs ~$100/month (~$3.30/day).
Paying 5¢/kWh extra on 10 kWh costs 50¢. Always charge — the maths is not close.

Quick check: past 12:30pm + battery below 40% + peak month = switch to autonomous immediately.
Past 1:30pm + battery below 70% + peak month = switch to autonomous immediately.

**PEAK MONTHS — "wait and go hard" strategy:**
When not yet at deadline urgency but grid charge IS needed (solar won't cover the gap),
do NOT default to slow self_consumption now. Instead:
1. Scan the price forecast for the cheapest slot where:
     hours_until_slot + hours_to_fill_fast + 0.5h ≤ hours_remaining
   (i.e., you can still fast-fill and hit the deadline after waiting for that slot)
2. Project SoC at that slot conservatively (home load drains battery, no solar credit).
3. If that cheapest slot is ≥1¢ cheaper than now: HOLD and wait for it. Report in your
   summary: "cheapest feasible slot: Xh away at Y¢ — will go autonomous then".
   While holding: compute projected_soc = soc − (hours_to_slot × home_load_kw / 13.5 × 100).
   If projected_soc > 5%: leave reserve at 5%. No action.
   If projected_soc ≤ 5%: set reserve to (drain_to_slot + 8%) — survival minimum only.
   Do NOT set reserve to 85% — that triggers charging immediately at the current price.
4. Once you're at (or past) that cheapest slot and grid charge is still needed:
   use AUTONOMOUS (5 kW) — not self_consumption. Fill fast at the cheap price and be done
   in fill_fast_85_h, rather than dribbling at 1.7 kW for fill_slow_85_h.
   The battery_autonomous_revert_target_reached automation stops charging automatically
   within 30s of hitting the target — you do NOT need to switch mode at the next cycle.
5. If no cheaper slot exists in the feasible window (all upcoming prices ≥ now):
   charge at self_consumption now — current price is as good as it gets.

**RECEDING HORIZON — every cycle is a fresh decision:**
Do NOT treat mode_before as a reason to continue a previous decision. Every 30 minutes,
recalculate from scratch: given current SoC, current price, current solar, and fill maths,
what is the optimal charging rate for the NEXT 30 minutes?
The answer can change each cycle as solar improves, prices shift, or SoC rises:
  - 10:00am: solar=0.3kW, need 8.5kWh, fill_slow=5h, deadline=4.9h → autonomous (fill_slow > deadline-1h)
  - 10:30am: solar=1.5kW, need 6kWh, fill_slow=3.5h, deadline=4.4h → self_consumption (now fits)
  - 11:00am: solar=3kW, net_solar covers remaining gap → hold (solar will cover it)
Each cycle, recalculate fill_slow_85_h and fill_fast_85_h against the current deadline.
Use autonomous when fill_slow_85_h is tight relative to the deadline. Use self_consumption
when fill_slow_85_h fits comfortably. Hold when net solar covers the remaining gap.
The battery_autonomous_revert_target_reached automation fires within 30s of hitting the
target regardless — it is a safety net, not the primary rate controller.

The deterministic helper provides `go_hard_slot` in its reference block when this applies.
Example: 8:30am, SoC=16%, price=17¢, Solar Sponge at 10am (11¢), fill_fast=0.9h, deadline 6.4h away.
  projected_soc at 10am = 16 − (1.5h × 0.6kW / 13.5 × 100) = 16 − 7 = 9%. Above 5% floor.
→ Hold until 10am (1.5h wait). Action: NO reserve change (leave at 5%). Battery drains to ~9%.
  At 10am: go autonomous, set_reserve(100%). Fill in 0.9h. Done by 11am at 11¢.
  Do NOT set_reserve(85%) at 8:30am — that starts charging at 17¢ immediately, wasting the wait.
  Compare: self_consumption from 8:30am would trickle through 17¢ AND 11–14¢ slots over 2.6h,
  costing more AND occupying the battery charger during the most solar-productive hours.

**NON-PEAK MONTHS — soft deadline: avoid evening spike:**
Use hours_to_cheap_end (step 4 above) as your deadline — it automatically adapts to when
prices actually start rising today, whether that's 3pm, 4pm, or later:
  hours_to_fill_slow ≥ hours_to_cheap_end − 0.5h → start self_consumption NOW, spread irrelevant
  hours_to_fill_fast ≥ hours_to_cheap_end − 0.5h → escalate to autonomous NOW

**Solar unreliable escalation (non-peak):**
When forecast_accuracy is poor or unreliable (solar_unreliable = true), you cannot count on
solar supplementing self_consumption to reach the target. Apply a tighter buffer:
  hours_to_fill_slow ≥ hours_to_cheap_end − 1.5h AND solar_unreliable → autonomous NOW
Self_consumption at 1.7 kW with no solar contribution may not fill the battery in time.
Charging fast now while prices are still cheap is better than arriving at the evening spike
short, having relied on solar that never materialised.

Additional override after noon: if battery < 30% AND the price has been flat (within 3¢ of
the "cheap window" you were waiting for) across 2+ recent cycles → charge at current price.
The cheap window is not coming. Current price IS the floor.

## Solar forecast accuracy
The state read includes several solar fields — use them together:
- `current_kw`: actual inverter output right now
- `solcast_power_now_kw`: Solcast instantaneous estimate (kW)
- `forecast_this_hour_kwh`: Solcast expected generation this hour (kWh ≈ avg kW for the hour)
- `forecast_next_hour_kwh`: Solcast expected generation next hour (kWh) — use for planning
- `forecast_remaining_kwh`: Solcast expected remaining generation today (kWh)
- `forecast_accuracy`: auto-computed label based on current_kw vs forecast_this_hour_kwh

Use `forecast_accuracy` to decide how much to trust `forecast_remaining_kwh`:
- `good`: forecast is reliable — use remaining_kwh as-is for charging decisions
- `poor`: treat remaining_kwh as optimistic — assume actual solar will be ~50% of forecast
- `unreliable`: ignore remaining_kwh entirely — assume no meaningful solar for the rest
  of the day and charge from grid as if it were a zero-solar day

**IMPORTANT — discard `grid_target_pct` when forecast is unreliable or poor.**
The `grid_target_pct` sensor is computed from the Solcast remaining forecast. On a cloudy
day it will be optimistically low (e.g. "60% is enough, solar will cover the rest") even
when actual solar is delivering almost nothing. When forecast_accuracy is poor or unreliable,
ignore `grid_target_pct` entirely and substitute a time-based target:

| Time of day       | Substitute charge target |
|-------------------|--------------------------|
| Before 12pm       | 85% — full day of load ahead, assume solar contributes little |
| 12pm–2pm          | 70% — half day left |
| After 2pm         | 50% — mostly evening load to cover |

Then apply the spread table normally: if the spread between current price and the upcoming
expensive period justifies charging, charge to this substitute target.

Use `forecast_next_hour_kwh` to inform timing:
- If next hour is forecast to generate significantly more than this hour, solar may be
  improving — factor this into whether to start grid charging now or wait 30–60 min.
- If next hour is also low and forecast_accuracy is poor/unreliable, don't wait for solar.

This is especially important in peak months: if forecast is unreliable at 10am on a
cloudy day, begin grid charging immediately toward the 85% SoC target regardless of
what the remaining forecast says.

## Weather forecast — when and how to use it

`get_weather_forecast()` returns `radiation_wm2` (W/m²) and `cloud_cover_pct` for each solar hour,
plus a `tomorrow_solar_outlook` summary (good / poor / overcast).

**Interpreting `effective_radiation_wm2` for this site** (6.12 kWp flat roof, Sydney):

Each hour includes both `radiation_wm2` (Open-Meteo model GHI) and `effective_radiation_wm2`
(rain-adjusted: multiplied by 0.25 when `precip_mm_h > 0.1`). Always use `effective_radiation_wm2`
for decisions — the raw model overestimates significantly when it is actively raining, because
rainfall attenuates irradiance and the SolarEdge inverter won't start below ~50 W/m².

| Effective radiation | Solar output | Label |
|---------------------|-------------|-------|
| > 300 W/m² | > 1.5 kW | good — Solcast likely reliable |
| 150–300 W/m² | 0.5–1.5 kW | poor — Solcast may over-estimate |
| < 150 W/m² | < 0.5 kW | overcast — treat as zero-solar day |
| < 50 W/m² | 0 kW | none — inverter won't start |

**When to call it:**
- Any overnight cycle (10pm–6am): call it to check tomorrow's outlook before deciding
  whether to pre-charge tonight.
- Any daytime cycle where `forecast_accuracy` is poor/unreliable: call it to distinguish
  "the whole day is cloudy" (radiation consistently low) from "temporary cloud passing through"
  (radiation high but actual output low right now — wait 30 min before acting).

**Overnight pre-charging — DO NOT pre-charge at high prices when Solar Sponge will be cheaper:**

Solar Sponge (10am–3pm) is structurally cheaper than overnight prices on most days.
The demand window is only 3–9pm — the battery just needs to reach 85% by 2:55pm, not overnight.
Rule 13 morning deadline maths handles peak months: it will escalate to autonomous at 9am if needed.

**Default overnight behaviour: hold and let the battery drain.**
Do NOT raise reserve or charge overnight unless one of these specific exceptions applies:

1. `overnight_hold = False` in the deterministic helper (price ≤ 10¢ overnight — genuinely cheap)
2. SoC ≤ 25% AND no cheap window in the next 8h — emergency floor only
3. Tomorrow is peak month AND tomorrow's solar outlook is `overcast` (< 150 W/m²) AND
   overnight price < 15¢ — in this case Solar Sponge alone may not fill the battery

If none of these apply: **hold, let the battery drain, charge during Solar Sponge tomorrow at 6–8¢.**
Charging overnight at 13–17¢ when 6¢ Solar Sponge is 8–12h away wastes money unnecessarily.
The deterministic helper will show `overnight_hold: True` when this applies — respect it.

**Cross-check use (daytime):**
If `forecast_accuracy` is unreliable but `effective_radiation_wm2` for the next 2–3 hours is > 250,
solar is likely improving and it may be worth waiting 30–60 min before charging from grid.
If `effective_radiation_wm2` is also < 150 (or it is raining, `precip_mm_h > 0.1`), it is a
genuine all-day cloudy/wet day — act accordingly, don't wait for solar.

**CRITICAL — zero actual solar overrides all model forecasts:**
Check the "Recent decisions" context. If `solar=0.0kW` appears in 2 or more of the last 3
cycles during daylight hours (after 9am), this is a zero-solar day. Full stop.
Do NOT count pre-9am zero readings — panels on a flat roof in Sydney don't produce
meaningful power before ~9am regardless of cloud cover. Zero output at 8am is normal.
Do NOT cite "solar will arrive from Xam" as a reason to defer grid charging.
Do NOT cite weather model radiation forecasts as a reason to hold.
The Open-Meteo model saying "radiation improves at 11am" is a prediction. Zero kW actual
for 2+ hours after 9am IS evidence. Evidence beats predictions.
When recent decisions show 2+ cycles of zero solar after 9am: treat as zero-solar day,
apply time-based substitute targets, and charge from grid immediately.

## Price risk asymmetry

The spread table treats price decisions as symmetric — 4¢ spread looks the same whether you
charge now or wait. It is not symmetric. Evening prices have asymmetric upside risk:
- If you charge at 15¢ and evening turns out to be 18¢: you overpaid 3¢/kWh
- If you wait and evening turns out to be 30¢+: you underpaid by 15¢/kWh on what you missed

Dynamic spot prices can spike well above forecast due to demand events, network constraints,
or market volatility. The forecast says 19¢ tonight but the real distribution has a long
right tail. Charging during Solar Sponge is a hedge against that tail, not just a cost decision.

**Practical implication:** treat Solar Sponge charging as insurance, not arbitrage.
The spread table applies when deciding how much above the floor to charge — but the floor
itself should always be met during the Solar Sponge window regardless of spread.

## Solar Sponge minimum floor

EA116's Solar Sponge window (10am–3pm) is structurally cheaper than evening prices — always.
This is baked into the tariff, not a price forecast. The spread table does not apply to this floor.

**Rule: during 10am–1pm, if SoC < 50%, charge to 50% regardless of price spread.**

This is a floor only — not a ceiling:
- If the demand window target or grid charge target is higher than 50%, use that instead
- If SoC is already ≥ 50%, normal spread logic applies for topping up further
- The 50% floor is non-negotiable during 10am–1pm: Solar Sponge import is always the cheapest
  window of the day on EA116. Waiting for a "better window" that might arrive later is wrong —
  the Solar Sponge IS the better window. Any charge done here avoids paying evening rates.

Implementation: if `in_solar_sponge` is True AND `now_h < 13` AND `soc < 50`:
  set_powerwall_reserve(max(50, grid_target_or_demand_target))
  Use self_consumption (don't need autonomous for a 50% floor — plenty of time)

If it's past 1pm and SoC < 50%, apply the deadline-aware escalation rules instead.

## Overnight low battery logic
In non-peak months there is NO demand window penalty, so letting the battery drain to zero
overnight is perfectly acceptable if cheaper grid prices are coming. Do not charge just
because the battery is low — check the price forecast first:
- If a cheaper window (≥3¢ cheaper than now) is coming within 4 hours: hold, let it drain,
  charge when the cheap window arrives
- If no cheaper window is coming: compute projected_soc = soc − (hours_to_next_cheap × load_rate).
  If projected_soc > 5%: hold, let it drain, charge when the cheap window arrives.
  If projected_soc ≤ 5%: charge now to survive — set reserve to (drain + 8%), then charge cheap later.

## Your task each cycle
1. Review "Recent decisions" in your context — check for deferral patterns before anything else
2. Call get_current_state() — understand where things are
3. Call get_price_forecast() — understand what's coming price-wise
4. Call get_solar_forecast() if timing of solar is relevant to a decision
5. Apply Solar Sponge minimum floor first (10am–1pm, SoC < 50% → charge regardless of spread)
6. Apply time-based escalation rules if applicable (peak month deadline or deferral limit)
6. Decide if action is needed. "Hold" is often correct — but not if you've held 2+ times already
7. Call set_* tools if action is needed
8. Always call log_decision() with your reasoning, even if you did nothing.
   - `summary`: battery-focused — SoC, price, mode, reserve, what you did and why.
   - `ev_summary`: EV-focused — plug state, EV SoC, Zappi mode set and why (1–2 sentences). If disconnected, say "EV disconnected — no action."

Be conservative. Only act when you're confident it improves outcomes.
If the system is already in the right state, say so and hold.
""".strip()

# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

TOOL_MAP = {
    "get_current_state":    lambda _: get_current_state(),
    "get_price_forecast":   lambda _: get_price_forecast(),
    "get_solar_forecast":   lambda _: get_solar_forecast(),
    "get_weather_forecast": lambda _: get_weather_forecast(),
    "set_powerwall_reserve":lambda a: set_powerwall_reserve(a["percent"]),
    "set_powerwall_mode":   lambda a: set_powerwall_mode(a["mode"]),
    "set_zappi_mode":       lambda a: set_zappi_mode(a["mode"]),
    "log_decision":         lambda a: log_decision(a["summary"], a["actions_taken"], a.get("ev_summary", "")),
}


def run_agent(dry_run: bool = False):
    """
    Run one optimisation cycle.
    dry_run=True: reads state and prints the agent's reasoning but does NOT
    call any set_* or log_decision tools — safe for testing.
    """
    _cycle_context.clear()
    client        = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    recent        = get_recent_decisions(3)

    # --- Pre-flight safety checks (run before LLM, independent of shadow layer) ------

    # 1. Demand-window reserve guard (Rule 2 backstop).
    # If we're inside the 3–9pm demand window on a peak month and the reserve is
    # stranded above 10%, the Powerwall can't discharge — grid covers home load and
    # sets a demand ratchet charge. Drop it to 5% via Tessie directly, bypassing HA
    # rest_commands entirely (those may be broken on an HA restart, as happened June 2).
    _demand_reserve_guard_fired = False
    try:
        _now_pre = datetime.now(SYDNEY_TZ)
        _is_peak_pre = _now_pre.month in PEAK_MONTHS
        _in_demand_pre = _is_peak_pre and 15 <= _now_pre.hour < 21
        if _in_demand_pre and not dry_run:
            _reserve_pre = _int(ha_state(ENTITIES["battery_reserve"]))
            _soc_pre     = _int(ha_state(ENTITIES["battery_soc"]))
            if _reserve_pre is not None and _reserve_pre > 10:
                print(f"  ⚠️  DEMAND WINDOW RESERVE GUARD: reserve={_reserve_pre}% during demand"
                      f" window (soc={_soc_pre}%) — dropping to 5% via Tessie directly",
                      file=sys.stderr)
                set_powerwall_reserve(5)
                _demand_reserve_guard_fired = True
    except Exception as _exc:
        print(f"  Warning: demand-window reserve guard failed: {_exc}", file=sys.stderr)

    # 2. HA rest_command health check — warn if the safety automations are broken.
    try:
        _svc_r = requests.get(f"{HA_URL}/api/services", headers=HA_HEADERS, timeout=10)
        if _svc_r.status_code == 200:
            _domains = {d["domain"] for d in _svc_r.json()}
            if "rest_command" not in _domains:
                print("  ⚠️  HA rest_command NOT LOADED — safety automations (2:55pm reset,"
                      " startup floor) are broken. Restart HA to fix.", file=sys.stderr)
    except Exception as _exc:
        print(f"  Warning: HA health check failed: {_exc}", file=sys.stderr)

    # ------------------------------------------------------------------------------------

    # Shadow decision layer (Phase 3): precompute the deterministic verdict from the
    # same state + forecast the LLM will read, inject it as REFERENCE ONLY, and log
    # it alongside the LLM's actual decision so we can measure divergence over time.
    # The LLM remains authoritative — this does not constrain it.
    shadow_block = ""
    try:
        _state    = get_current_state()
        _forecast = get_price_forecast()
        _records  = get_recent_records(3)
        # Inject price stats into settings so compute_decision_context can use them
        _prices   = load_price_history(PRICE_HISTORY_DAYS)
        _stats    = _price_stats(_prices)
        _state.setdefault("settings", {})["price_stats"] = _stats
        _ctx      = compute_decision_context(_state, _forecast, _records,
                                             datetime.now(SYDNEY_TZ))
        _cycle_context["decision_context"] = _ctx
        shadow_block = "\n\n" + _format_decision_context(_ctx)
    except Exception as exc:
        print(f"  Warning: shadow decision context failed: {exc}", file=sys.stderr)

    # LP optimiser shadow verdict — separate try so it can never affect the
    # deterministic shadow or the control path. Shadow only, not authoritative.
    if _HAVE_OPTIMIZER:
        try:
            _opt_state  = _cycle_context.get("state")
            _opt_prices = _cycle_context.get("price_forecast")
            if _opt_state and _opt_prices:
                # Extend the ~6h Amber forecast with synthetic historical prices so the LP
                # can see the 15:00–21:00 demand-window block and apply its demand_penalty.
                _hourly_model = _build_hourly_price_model()
                _opt_prices_ext = _extend_forecast_to_demand_window(
                    _opt_prices, datetime.now(SYDNEY_TZ), _hourly_model)
                _opt = optimize_battery(_opt_state, _opt_prices_ext,
                                        get_solar_forecast(), datetime.now(SYDNEY_TZ))
                _cycle_context["optimizer_verdict"]  = _opt["verdict"]
                _cycle_context["optimizer_context"]  = {
                    k: v for k, v in _opt.items() if k != "verdict"}
        except Exception as exc:
            print(f"  Warning: optimizer shadow failed: {exc}", file=sys.stderr)

    initial_msg   = (
        "Run your energy optimisation cycle now.\n\n"
        "## Recent decisions (last 3 cycles)\n"
        f"{recent}\n\n"
        "Check this before deciding to hold. If you see 2+ consecutive holds waiting for "
        "a cheaper window that hasn't arrived, apply the deferral limit rule and charge now."
        f"{shadow_block}"
    )
    messages = [{"role": "user", "content": initial_msg}]

    print(f"\n{'='*60}")
    print(f"Energy agent — {datetime.now(SYDNEY_TZ).strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"{'='*60}")
    if dry_run:
        print("DRY RUN — no writes will be made\n")

    # System prompt as a cacheable content block — static content cached across turns
    system_prompt_block = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=system_prompt_block,
            tools=TOOLS,
            messages=messages,
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )

        messages.append({"role": "assistant", "content": response.content})

        # Accumulate token usage — track cache hits separately for accurate cost calculation
        _cycle_context["input_tokens"]        = _cycle_context.get("input_tokens", 0)        + response.usage.input_tokens
        _cycle_context["output_tokens"]       = _cycle_context.get("output_tokens", 0)       + response.usage.output_tokens
        _cycle_context["cache_read_tokens"]   = _cycle_context.get("cache_read_tokens", 0)   + getattr(response.usage, "cache_read_input_tokens", 0)
        _cycle_context["cache_write_tokens"]  = _cycle_context.get("cache_write_tokens", 0)  + getattr(response.usage, "cache_creation_input_tokens", 0)

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                name  = block.name
                args  = block.input or {}
                is_write = name.startswith("set_") or name == "log_decision"

                print(f"→ {name}({_args_summary(args)})")

                if dry_run and is_write:
                    result = f"[dry-run] {name} skipped"
                else:
                    try:
                        result = TOOL_MAP[name](args)
                    except Exception as exc:
                        result = f"ERROR: {exc}"
                        print(f"  !! {result}", file=sys.stderr)

                print(f"  ← {json.dumps(result)[:120]}")
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     json.dumps(result),
                })

            messages.append({"role": "user", "content": tool_results})
        else:
            break   # unexpected stop reason

    print("\nCycle complete.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _float(val, default=0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default

def _int(val, default=0) -> int:
    return int(_float(val, default))

def _solar_accuracy(actual_kw: float, forecast_kw: float) -> str:
    """Return a plain-English accuracy label for the agent to reason about."""
    if forecast_kw < 0.2:
        return "not_applicable (night or near-zero forecast)"
    ratio = actual_kw / forecast_kw
    if ratio < 0.3:
        return f"unreliable — actual {actual_kw:.1f}kW vs forecast {forecast_kw:.1f}kW ({ratio:.0%} of forecast)"
    if ratio < 0.7:
        return f"poor — actual {actual_kw:.1f}kW vs forecast {forecast_kw:.1f}kW ({ratio:.0%} of forecast)"
    return f"good — actual {actual_kw:.1f}kW vs forecast {forecast_kw:.1f}kW ({ratio:.0%} of forecast)"

def _safe_int(entity_id: str, default=None):
    """Read an entity state as int, returning default if entity missing or unavailable."""
    try:
        return _int(ha_state(entity_id))
    except Exception:
        return default

def _safe_float(entity_id: str, default: float = 0.0) -> float:
    """Read an entity state as float, returning default if entity missing or unavailable."""
    try:
        return float(ha_state(entity_id) or default)
    except Exception:
        return default

def _args_summary(args: dict) -> str:
    if not args:
        return ""
    return ", ".join(f"{k}={v}" for k, v in args.items())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    run_agent(dry_run=dry)
