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
from datetime import datetime
from pathlib import Path

import anthropic
import pytz
import requests

# ---------------------------------------------------------------------------
# Configuration — move sensitive values to environment variables in production
# ---------------------------------------------------------------------------

HA_URL   = "http://localhost:8123"
HA_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiIxZjQxNDVmOTBjYTI0ZDgyYjk5MTI5ZjE2YzY3ZWEzNSIsImlhdCI6MTc3OTY2OTMyNiwiZXhwIjoyMDk1MDI5MzI2fQ.Gu5FPRLbn3PpTOstsR-B87fyVeEC00dRXAB6ZiYiFt0"

TESSIE_TOKEN   = "BEVtCQYyFhwkEu02WK4ONIAoyiJwGP9z"
TESSIE_SITE_ID = "2252120180790091"

# Load .env file if present (gitignored — keeps keys out of the repo)
# Uses direct assignment so .env wins over empty shell exports; a non-empty
# shell variable (e.g. set for testing) still takes precedence.
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

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")  # set via agent/.env

LOG_FILE   = Path(__file__).parent / "agent_decisions.log"
JSONL_FILE = Path(__file__).parent / "decisions.jsonl"

# Populated by get_current_state() / get_price_forecast() during each cycle,
# then read by log_decision() to write the structured JSON record.
_cycle_context: dict = {}

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
    "ev_plug":              "sensor.home_ev_charger_zappi_myenergi_home_ev_charger_zappi_plug_status",
    "ev_zappi_mode":        "select.home_ev_charger_zappi_myenergi_home_ev_charger_zappi_charge_mode",
    "ev_soc":               "sensor.polestar_7853_battery_charge_level",
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

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def get_current_state() -> dict:
    now = datetime.now(SYDNEY_TZ)
    month, hour = now.month, now.hour
    is_peak      = month in PEAK_MONTHS
    in_demand    = is_peak and 15 <= hour < 21
    in_sponge    = 10 <= hour < 15          # Solar Sponge window

    ev_plug_state = ha_state(ENTITIES["ev_plug"])
    ev_plugged = ev_plug_state != "EV Disconnected"

    state = {
        "timestamp":       now.strftime("%Y-%m-%d %H:%M %Z"),
        "month":           now.strftime("%B"),
        "is_peak_month":   is_peak,
        "in_demand_window": in_demand,
        "in_solar_sponge":  in_sponge,
        "battery": {
            "soc_pct":          _int(ha_state(ENTITIES["battery_soc"])),
            "soc_gateway_pct":  _int(ha_state(ENTITIES["battery_soc_gateway"])),
            "mode":             ha_state(ENTITIES["battery_mode"]),
            "reserve_pct":      _int(ha_state(ENTITIES["battery_reserve"])),
            "grid_target_pct":  _int(ha_state(ENTITIES["battery_target"])),
        },
        "grid": {
            "price_cents_kwh":  round(_float(ha_state(ENTITIES["grid_price"])) * 100, 1),
            "in_cheap_window":  ha_state(ENTITIES["cheap_window"]) == "True",
        },
        "solar": {
            # Unit notes: solaredge_current_power=W, solcast_power_now=W, this_hour=Wh, next_hour=Wh
            # remaining_today is natively kWh (no conversion needed)
            "current_kw":               round(_float(ha_state(ENTITIES["solar_power"])) / 1000, 2),
            "solcast_power_now_kw":     round(_float(ha_state(ENTITIES["solcast_power_now"])) / 1000, 2),
            "forecast_this_hour_kwh":   round(_float(ha_state(ENTITIES["solcast_this_hour"])) / 1000, 2),
            "forecast_next_hour_kwh":   round(_float(ha_state(ENTITIES["solcast_next_hour"])) / 1000, 2),
            "forecast_remaining_kwh":   round(_float(ha_state(ENTITIES["solar_remaining"])), 1),
            # Accuracy: compare actual kW vs forecast_this_hour (Wh→kWh ≈ avg kW for the hour)
            "forecast_accuracy":        _solar_accuracy(
                                            round(_float(ha_state(ENTITIES["solar_power"])) / 1000, 2),
                                            round(_float(ha_state(ENTITIES["solcast_this_hour"])) / 1000, 2)
                                        ),
        },
        "home_load_kw":  round(_float(ha_state(ENTITIES["home_load"])), 2),
        "ev": {
            "plug_status": ev_plug_state,
            "plugged_in":  ev_plugged,
            "charging":    ev_plug_state == "Charging",
            "zappi_mode":  ha_state(ENTITIES["ev_zappi_mode"]) if ev_plugged else "n/a",
            "soc_pct":     _safe_int(ENTITIES["ev_soc"]) if ev_plugged else None,
        },
    }
    _cycle_context["state"] = state
    return state


def get_price_forecast() -> list[dict]:
    """Next 12 hours of Amber price forecasts from HA forecast sensor attributes."""
    attrs = ha_attrs(ENTITIES["grid_forecast"])
    forecasts = attrs.get("forecasts", [])
    result = []
    for f in forecasts[:24]:        # 24 × 30 min = 12 h
        result.append({
            "time":       f.get("nem_date", "")[:16],
            "cents_kwh":  round(_float(f.get("per_kwh", 0)) * 100, 1),
            "descriptor": f.get("descriptor", ""),
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


def log_decision(summary: str, actions_taken: list[str]) -> str:
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
        "solar_remaining_kwh":  solar.get("forecast_remaining_kwh"),
        "solar_this_hour_kwh":  solar.get("forecast_this_hour_kwh"),
        "solar_next_hour_kwh":  solar.get("forecast_next_hour_kwh"),
        "home_load_kw":         state.get("home_load_kw"),
        "ev_plugged":           ev.get("plugged_in"),
        "ev_soc":               ev.get("soc_pct"),
        "ev_zappi_mode_before": ev.get("zappi_mode"),
        "price_forecast_6h":    [f["cents_kwh"] for f in forecast[:12]],
        "actions":              actions_taken,
        "summary":              summary,
    }
    with JSONL_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")

    # Overwrite the "last decision" notification — quick at-a-glance in HA
    ha_service("persistent_notification", "create", {
        "notification_id": "energy_agent_latest",
        "title": f"⚡ Agent — {now.strftime('%H:%M')}",
        "message": f"{summary}\n\n**Actions:** {actions}",
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
            "'self_consumption': normal ~1.7 kW grid charge rate. Use for long cheap windows (3h+) "
            "or when the price spread doesn't justify urgency. "
            "'autonomous': fast ~5 kW grid charge. ALWAYS pair with set_powerwall_reserve(100) — "
            "this is the export guard. A HA safety net also reverts to self_consumption within 30s "
            "if export is detected, so autonomous is safe. "
            "Use autonomous only when the price spread justifies urgency: spread > 8¢ AND need "
            ">15% SoC AND window is short (<2h), OR peak month demand window risk (see system prompt). "
            "A 4¢ spread does NOT justify autonomous — use self_consumption or hold."
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
                "mode": {"type": "string", "enum": ["Eco+", "Fast", "Off"]}
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
            },
            "required": ["summary", "actions_taken"],
        },
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
   Zappi must stay in Eco+ unless one of these is true:
   - Price < 10¢/kWh (ultra-cheap, fine to use Fast)
   - EV SoC < 30% AND price < 20¢ (low battery, acceptable cost)
   - Battery is actively charging from grid (can't discharge anyway — Fast is safe)
   Never switch Zappi to Fast during Solar Sponge (10am–3pm) if battery is discharging.

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
     3. hours_to_fill_slow = kWh_needed / 1.7   (self_consumption rate)
        hours_to_fill_fast = kWh_needed / 5.0   (autonomous rate)
     4. hours_to_spike = hours until price first exceeds 30¢ in forecast
     5. Mode decision:
        - hours_to_spike > hours_to_fill_slow + 1.5h → cheaper window may still be viable, evaluate spread
        - hours_to_spike ≤ hours_to_fill_slow + 1.5h → start self_consumption NOW, no time to wait
        - hours_to_spike ≤ hours_to_fill_fast + 0.5h → start autonomous NOW, only fast mode is fast enough
     6. Once charging has started, re-run this every cycle. If self_consumption is falling behind
        (hours_to_spike ≤ hours_to_fill_slow + 0.5h), escalate to autonomous automatically.
        This means: start slow and cheap, escalate to fast only when the maths demands it.
     Example: 36% SoC, 85% target, spike at 3pm, now 10:30am (4.5h to spike)
       kWh_needed=6.6, hours_to_fill_slow=3.9h, hours_to_fill_fast=1.3h
       4.5h > 3.9 + 1.5 = 5.4h? No → start self_consumption now
       Next cycle (11am, 4h to spike, battery at ~38%): still enough for self_consumption? Recalculate.
       By 1pm if battery at 55%: kWh_needed=4.1, hours_to_fill_slow=2.4h, spike in 2h → escalate to autonomous

4. SOLAR UTILISATION
   - Battery should have enough headroom to absorb forecast solar.
   - Grid charge target = how much SoC is needed so solar covers the rest of the day.
   - Solar Sponge 10am–3pm: cheapest import, but export during this window is penalised.
     Don't export during Solar Sponge — let solar charge battery instead.

## Operating constraints
- Only control: backup_reserve_percent and mode (via Tessie API)
- self_consumption mode: ~1.7 kW grid charge rate. Needs ~4–6h to charge 20%→80%.
- autonomous mode: ~5 kW grid charge rate (fast). Always pair with reserve=100% as export guard.
  A HA safety net also monitors for export and reverts within 30s if firmware misbehaves.
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

## Overnight low battery logic
In non-peak months there is NO demand window penalty, so letting the battery drain to zero
overnight is perfectly acceptable if cheaper grid prices are coming. Do not charge just
because the battery is low — check the price forecast first:
- If a cheaper window (≥3¢ cheaper than now) is coming within 4 hours: hold, let it drain,
  charge when the cheap window arrives
- If no cheaper window is coming and battery is below 20%: charge now to ~80% at current price
- Only override this logic (charge immediately regardless of price) if battery is below 5%
  AND no cheaper window within 2 hours — i.e. genuinely about to go flat with nothing better coming

## Your task each cycle
1. Call get_current_state() — understand where things are
2. Call get_price_forecast() — understand what's coming price-wise
3. Call get_solar_forecast() if timing of solar is relevant to a decision
4. Decide if action is needed. "Hold" is often correct — don't churn settings.
5. Call set_* tools if action is needed
6. Always call log_decision() with your reasoning, even if you did nothing

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
    "set_powerwall_reserve":lambda a: set_powerwall_reserve(a["percent"]),
    "set_powerwall_mode":   lambda a: set_powerwall_mode(a["mode"]),
    "set_zappi_mode":       lambda a: set_zappi_mode(a["mode"]),
    "log_decision":         lambda a: log_decision(a["summary"], a["actions_taken"]),
}


def run_agent(dry_run: bool = False):
    """
    Run one optimisation cycle.
    dry_run=True: reads state and prints the agent's reasoning but does NOT
    call any set_* or log_decision tools — safe for testing.
    """
    _cycle_context.clear()
    client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    messages = [{"role": "user", "content": "Run your energy optimisation cycle now."}]

    print(f"\n{'='*60}")
    print(f"Energy agent — {datetime.now(SYDNEY_TZ).strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"{'='*60}")
    if dry_run:
        print("DRY RUN — no writes will be made\n")

    while True:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

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
