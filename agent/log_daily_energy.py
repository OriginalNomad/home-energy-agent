#!/usr/bin/env python3
"""
Daily energy journal
====================
A comprehensive daily record of everything a learning agent needs to understand
what happened, why, and whether the system made good decisions. Captures:

  - Solar: Solcast forecast vs actual inverter output (accuracy ratio)
  - Price: profiles across key tariff windows (overnight, Solar Sponge, demand, evening)
  - Battery: SoC trajectory at key moments, charge/discharge totals
  - Load: home consumption across the day
  - Grid: import/export totals, per-window breakdown
  - Demand window: billing-accurate pass/fail (EA116 peak 30-min avg kW)
  - Agent decisions: rules fired, modes used, charge cycles from decisions.jsonl

Persisted to `agent/daily_energy.jsonl` — the durable record that survives HA's
~10-day recorder rolloff. One JSON object per day, git-committed.

Data sources:
  - HA history API (sensor readings over the day)
  - agent/decisions.jsonl (agent cycle records for the day)

Run: cron at ~21:05 daily
    5 21 * * *  /usr/bin/python3 ".../agent/log_daily_energy.py"

Backfill (limited by HA recorder retention):
    python3 log_daily_energy.py --date 2026-06-01
    python3 log_daily_energy.py --days 7
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import os

import pytz
import requests

# Load .env if present (same pattern as energy_agent.py)
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            if not os.environ.get(_k.strip()):
                os.environ[_k.strip()] = _v.strip().strip('"').strip("'")

# --- Config (mirrors energy_agent.py) --------------------------------------
HA_URL   = os.environ.get("HA_URL",   "http://localhost:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiIxZjQxNDVmOTBjYTI0ZDgyYjk5MTI5ZjE2YzY3ZWEzNSIsImlhdCI6MTc3OTY2OTMyNiwiZXhwIjoyMDk1MDI5MzI2fQ.Gu5FPRLbn3PpTOstsR-B87fyVeEC00dRXAB6ZiYiFt0")
HA_HEADERS = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}

TZ = pytz.timezone("Australia/Sydney")

# --- Sensors ----------------------------------------------------------------
GRID_SENSOR    = "sensor.tesla_powerwall_2_site_power"        # kW, + = import
SOLAR_SENSOR   = "sensor.tesla_powerwall_2_solar_power"       # kW, actual inverter
LOAD_SENSOR    = "sensor.tesla_powerwall_2_load_power"        # kW, home consumption
BATTERY_SENSOR = "sensor.tesla_powerwall_2_battery_power"     # kW, + = discharge
SOC_SENSOR     = "sensor.tessie_powerwall_charge"             # %, true SoC
WINDOW_SENSOR  = "binary_sensor.1a_wigram_road_glebe_demand_window"
PRICE_SENSOR   = "sensor.1a_wigram_road_glebe_general_price"  # $/kWh
FIT_SENSOR     = "sensor.1a_wigram_road_glebe_feed_in_price"  # $/kWh
SOLCAST_SENSOR = "sensor.solcast_pv_forecast_forecast_today"  # kWh, day forecast

# --- Time windows -----------------------------------------------------------
DEMAND_START_H = 15
DEMAND_END_H   = 21
SPONGE_START_H = 10
SPONGE_END_H   = 15
PASS_KW_THRESHOLD = 0.10   # EA116: peak 30-min-average import kW

# --- Paths ------------------------------------------------------------------
OUT_FILE      = Path(__file__).parent / "daily_energy.jsonl"
DECISIONS_FILE = Path(__file__).parent / "decisions.jsonl"


# ---------------------------------------------------------------------------
# HA history helpers
# ---------------------------------------------------------------------------

def ha_history(entity_id: str, start: datetime, end: datetime) -> list:
    url = f"{HA_URL}/api/history/period/{start.isoformat()}"
    params = {"filter_entity_id": entity_id, "end_time": end.isoformat()}
    r = requests.get(url, headers=HA_HEADERS, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data or not data[0]:
        return []
    out = []
    for s in data[0]:
        ts = datetime.fromisoformat(s["last_changed"]).astimezone(TZ)
        out.append((ts, s.get("state")))
    return out


def to_numeric(raw: list) -> list:
    out = []
    for ts, st in raw:
        try:
            out.append((ts, float(st)))
        except (TypeError, ValueError):
            continue
    return out


def integrate(series: list, start: datetime, end: datetime,
              clamp_positive: bool = False) -> float:
    """Trapezoidal integral of power (kW) → energy (kWh) over [start, end]."""
    if not series:
        return 0.0
    pts = []
    last_before = None
    for ts, val in series:
        if ts <= start:
            last_before = (start, val)
        elif ts < end:
            pts.append((ts, val))
        else:
            break
    if last_before:
        pts = [last_before] + pts
    elif pts:
        pts = [(start, pts[0][1])] + pts
    else:
        return 0.0
    pts.append((end, pts[-1][1]))

    kwh = 0.0
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        dt_h = (t1 - t0).total_seconds() / 3600.0
        v0 = max(p0, 0.0) if clamp_positive else p0
        v1 = max(p1, 0.0) if clamp_positive else p1
        kwh += (v0 + v1) / 2.0 * dt_h
    return round(kwh, 3)


def peak_30min_import(series: list, start: datetime, end: datetime) -> tuple:
    """EA116 billed metric: max clock-aligned 30-min average import (kW)."""
    peak_kw = 0.0
    peak_at = ""
    block = start
    while block < end:
        nxt = block + timedelta(minutes=30)
        kwh = integrate(series, block, nxt, clamp_positive=True)
        avg_kw = kwh / 0.5
        if avg_kw > peak_kw:
            peak_kw, peak_at = avg_kw, block.strftime("%H:%M")
        block = nxt
    return round(peak_kw, 3), peak_at


def soc_at(series: list, target: datetime) -> Optional[int]:
    """SoC at a specific time (nearest reading at or before target)."""
    best = None
    for ts, val in series:
        if ts <= target:
            best = val
        else:
            break
    return round(best) if best is not None else None


def cost_cents(grid_series: list, price_series: list,
               fit_series: list, start: datetime, end: datetime) -> Optional[float]:
    """Estimate net electricity cost in ¢ over [start, end].
    Import cost (grid_kw > 0, price $/kWh) minus export FIT credit (grid_kw < 0, fit $/kWh).
    """
    if not grid_series or not price_series:
        return None

    # Build a unified timeline of all events
    events: list[datetime] = sorted({ts for ts, _ in grid_series + price_series + fit_series
                                      if start <= ts <= end})
    if not events:
        return None

    def val_at(series: list, t: datetime) -> Optional[float]:
        best = None
        for ts, v in series:
            if ts <= t:
                best = v
            else:
                break
        return best

    total_c = 0.0
    for i in range(len(events) - 1):
        t0, t1 = events[i], events[i + 1]
        dt_h = (t1 - t0).total_seconds() / 3600.0
        g = val_at(grid_series, t0)
        p = val_at(price_series, t0)   # $/kWh
        f = val_at(fit_series, t0)     # $/kWh
        if g is None or p is None:
            continue
        if g > 0:
            total_c += g * (p * 100) * dt_h   # import cost in ¢
        elif g < 0 and f is not None:
            total_c += g * (f * 100) * dt_h   # export credit (g negative → subtracts)
    return round(total_c, 1)


def price_stats(series: list, start: datetime, end: datetime) -> dict:
    """Min/max/mean price (¢/kWh) over a window. Input is $/kWh from Amber."""
    vals = []
    for ts, p in series:
        if start <= ts < end:
            vals.append(p * 100)  # $ → ¢
    if not vals:
        return {"min_c": None, "max_c": None, "mean_c": None}
    return {
        "min_c": round(min(vals), 1),
        "max_c": round(max(vals), 1),
        "mean_c": round(sum(vals) / len(vals), 1),
    }


# ---------------------------------------------------------------------------
# Agent decision rollup from decisions.jsonl
# ---------------------------------------------------------------------------

def load_day_cycles(day) -> list:
    day_str = day.isoformat()
    cycles = []
    if not DECISIONS_FILE.exists():
        return cycles
    for line in DECISIONS_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if 'soc' not in r:
            continue
        ts = r.get('ts', '')
        if ts[:10] == day_str:
            cycles.append(r)
    return sorted(cycles, key=lambda r: r.get('ts', ''))


def agent_summary(cycles: list) -> dict:
    if not cycles:
        return {}

    charge_cycles = [c for c in cycles if c.get('actions') and
                     any('reserve' in str(a).lower() for a in c['actions'])]

    rules_fired = {}
    for c in cycles:
        cv = c.get('computed_verdict') or {}
        rf = cv.get('rule_fired')
        if rf:
            rules_fired[rf] = rules_fired.get(rf, 0) + 1

    opt_rules = {}
    for c in cycles:
        ov = c.get('optimizer_verdict') or {}
        rf = ov.get('rule_fired')
        if rf:
            opt_rules[rf] = opt_rules.get(rf, 0) + 1

    modes_used = set()
    for c in cycles:
        ms = c.get('mode_set')
        if ms:
            modes_used.add(ms)
        mb = c.get('mode_before')
        if mb:
            modes_used.add(mb)

    shadow_checks = [c for c in cycles if c.get('shadow_action_match') is not None]
    shadow_matches = sum(1 for c in shadow_checks if c['shadow_action_match'])
    opt_checks = [c for c in cycles if c.get('optimizer_action_match') is not None]
    opt_matches = sum(1 for c in opt_checks if c['optimizer_action_match'])

    forecasts = [c.get('forecast_accuracy', '') for c in cycles if c.get('forecast_accuracy')]
    unreliable_count = sum(1 for f in forecasts if 'unreliable' in f.lower())

    goal = None
    projected = None
    for c in reversed(cycles):
        ts_h = int(c.get('ts', '')[11:13] or 0)
        if 12 <= ts_h <= 14:
            goal = c.get('goal_3pm_soc')
            projected = c.get('projected_3pm_soc')
            break

    costs = [c.get('est_cost_usd', 0) for c in cycles if c.get('est_cost_usd')]

    # Overnight LLM charges: cycles where LLM took a charge action between 20:00–07:00
    overnight_llm_charges = sum(
        1 for c in cycles
        if c.get('actions')
        and int((c.get('ts', '') or '')[11:13] or 12) >= 20
           or int((c.get('ts', '') or '')[11:13] or 12) < 7
    )
    # Fix: rebuild with proper hour check
    overnight_llm_charges = 0
    for c in cycles:
        ts_str = c.get('ts', '') or ''
        try:
            h = int(ts_str[11:13])
        except (ValueError, IndexError):
            continue
        if (h >= 20 or h < 7) and c.get('actions'):
            overnight_llm_charges += 1

    # Demand reserve guard: any cycle where the pre-flight backstop fired
    demand_reserve_guard_fired = any(c.get('demand_reserve_guard_fired') for c in cycles)

    # Forecast accuracy category: what fraction of daytime cycles had unreliable solar?
    daytime_cycles = [c for c in cycles if 9 <= int((c.get("ts", "") or "")[11:13] or 0) < 20]
    if daytime_cycles and len(daytime_cycles) >= 3:
        unreliable_rate = unreliable_count / len(daytime_cycles)
        forecast_accuracy_category = (
            "unreliable" if unreliable_rate > 0.5 else
            "poor"       if unreliable_rate > 0.2 else
            "good"
        )
    else:
        forecast_accuracy_category = None

    return {
        "total_cycles": len(cycles),
        "charge_cycles": len(charge_cycles),
        "modes_used": sorted(modes_used),
        "deterministic_rules_fired": rules_fired,
        "optimizer_rules_fired": opt_rules,
        "shadow_action_match_rate": f"{shadow_matches}/{len(shadow_checks)}" if shadow_checks else None,
        "optimizer_action_match_rate": f"{opt_matches}/{len(opt_checks)}" if opt_checks else None,
        "forecast_unreliable_cycles": unreliable_count,
        "forecast_accuracy_category": forecast_accuracy_category,
        "goal_3pm_soc": goal,
        "projected_3pm_soc_at_midday": projected,
        "agent_cost_usd": round(sum(costs), 3) if costs else None,
        "overnight_llm_charges": overnight_llm_charges,
        "demand_reserve_guard_fired": demand_reserve_guard_fired,
    }


# ---------------------------------------------------------------------------
# Core: build one day's record
# ---------------------------------------------------------------------------

def compute_day(day) -> dict:
    day_start = TZ.localize(datetime(day.year, day.month, day.day, 0, 0))
    day_end   = day_start + timedelta(days=1)
    demand_start = TZ.localize(datetime(day.year, day.month, day.day, DEMAND_START_H, 0))
    demand_end   = TZ.localize(datetime(day.year, day.month, day.day, DEMAND_END_H, 0))
    sponge_start = TZ.localize(datetime(day.year, day.month, day.day, SPONGE_START_H, 0))
    sponge_end   = TZ.localize(datetime(day.year, day.month, day.day, SPONGE_END_H, 0))

    # --- Fetch all HA history for the day ---
    grid_num  = to_numeric(ha_history(GRID_SENSOR, day_start, day_end))
    solar_num = to_numeric(ha_history(SOLAR_SENSOR, day_start, day_end))
    load_num  = to_numeric(ha_history(LOAD_SENSOR, day_start, day_end))
    soc_num   = to_numeric(ha_history(SOC_SENSOR, day_start, day_end))
    price_raw = to_numeric(ha_history(PRICE_SENSOR, day_start, day_end))
    fit_raw   = to_numeric(ha_history(FIT_SENSOR, day_start, day_end))
    win_hist  = ha_history(WINDOW_SENSOR, demand_start, demand_end)

    # --- Solcast forecast (snapshot at the time, if available) ---
    solcast_raw = ha_history(SOLCAST_SENSOR, day_start, day_end)
    solcast_vals = []
    for _, st in solcast_raw:
        try:
            solcast_vals.append(float(st))
        except (TypeError, ValueError):
            continue
    solcast_forecast_kwh = round(max(solcast_vals), 1) if solcast_vals else None

    # --- Solar: forecast vs actual ---
    solar_actual_kwh = integrate(solar_num, day_start, day_end)
    solar_accuracy = None
    if solcast_forecast_kwh and solcast_forecast_kwh > 0:
        solar_accuracy = round(solar_actual_kwh / solcast_forecast_kwh, 2)

    # --- Load ---
    load_total_kwh = integrate(load_num, day_start, day_end)
    load_demand_kwh = integrate(load_num, demand_start, demand_end)

    # --- Grid: import and export (split positive/negative) ---
    grid_import_kwh = integrate(grid_num, day_start, day_end, clamp_positive=True)
    grid_export_kwh = abs(integrate(
        [(ts, min(v, 0)) for ts, v in grid_num], day_start, day_end))
    grid_import_sponge_kwh = integrate(grid_num, sponge_start, sponge_end,
                                       clamp_positive=True)
    grid_import_demand_kwh = integrate(grid_num, demand_start, demand_end,
                                       clamp_positive=True)

    # --- Battery SoC trajectory ---
    soc_midnight = soc_at(soc_num, day_start + timedelta(minutes=5))
    soc_9am  = soc_at(soc_num, TZ.localize(datetime(day.year, day.month, day.day, 9, 0)))
    soc_2pm  = soc_at(soc_num, TZ.localize(datetime(day.year, day.month, day.day, 14, 0)))
    soc_3pm  = soc_at(soc_num, demand_start)
    soc_9pm  = soc_at(soc_num, demand_end)
    soc_min_demand = None
    demand_socs = [v for ts, v in soc_num if demand_start <= ts <= demand_end]
    if demand_socs:
        soc_min_demand = round(min(demand_socs))

    # --- Price profiles by window ---
    overnight_start = day_start
    overnight_end = TZ.localize(datetime(day.year, day.month, day.day, 7, 0))

    price_overnight = price_stats(price_raw, overnight_start, overnight_end)
    price_sponge    = price_stats(price_raw, sponge_start, sponge_end)
    price_demand    = price_stats(price_raw, demand_start, demand_end)
    price_day       = price_stats(price_raw, day_start, day_end)

    # FIT summary
    fit_day = price_stats(fit_raw, day_start, day_end)

    # --- Demand window verdict ---
    peak_day = any(state == "on" for _, state in win_hist)
    peak_kw, peak_at = (0.0, "")
    if peak_day:
        peak_kw, peak_at = peak_30min_import(grid_num, demand_start, demand_end)

    # --- Agent decision rollup ---
    cycles = load_day_cycles(day)
    agent = agent_summary(cycles)

    record = {
        "date": day.isoformat(),
        "peak_day": peak_day,

        # --- Demand window (Rule 2) ---
        "demand_window": {
            "passed": peak_kw < PASS_KW_THRESHOLD if peak_day else None,
            "peak_30min_import_kw": peak_kw,
            "peak_at": peak_at,
            "import_kwh": grid_import_demand_kwh,
            "min_soc_pct": soc_min_demand,
        },

        # --- Solar ---
        "solar": {
            "forecast_kwh": solcast_forecast_kwh,
            "actual_kwh": solar_actual_kwh,
            "forecast_vs_actual_ratio": solar_accuracy,
        },

        # --- Battery ---
        "battery": {
            "soc_midnight": soc_midnight,
            "soc_9am": soc_9am,
            "soc_2pm": soc_2pm,
            "soc_3pm": soc_3pm,
            "soc_9pm": soc_9pm,
            "soc_min_demand_window": soc_min_demand,
        },

        # --- Home load ---
        "load": {
            "total_kwh": load_total_kwh,
            "demand_window_kwh": load_demand_kwh,
        },

        # --- Grid ---
        "grid": {
            "import_kwh": grid_import_kwh,
            "export_kwh": grid_export_kwh,
            "import_sponge_kwh": grid_import_sponge_kwh,
            "import_demand_kwh": grid_import_demand_kwh,
            "net_cost_c": cost_cents(grid_num, price_raw, fit_raw, day_start, day_end),
        },

        # --- Prices ---
        "price": {
            "day": price_day,
            "overnight": price_overnight,
            "solar_sponge": price_sponge,
            "demand_window": price_demand,
            "fit": fit_day,
        },

        # --- Agent decisions ---
        "agent": agent,
    }

    return record


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def load_existing() -> dict:
    out = {}
    if OUT_FILE.exists():
        for line in OUT_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                out[r["date"]] = r
            except (json.JSONDecodeError, KeyError):
                continue
    return out


def write_all(records: dict) -> None:
    lines = [json.dumps(records[d]) for d in sorted(records)]
    OUT_FILE.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Log daily energy journal to daily_energy.jsonl")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--days", type=int, help="Backfill the last N days (overrides --date)")
    args = ap.parse_args()

    today = datetime.now(TZ).date()
    if args.days:
        days = [today - timedelta(days=i) for i in range(args.days - 1, -1, -1)]
    elif args.date:
        days = [datetime.strptime(args.date, "%Y-%m-%d").date()]
    else:
        days = [today]

    records = load_existing()
    for day in days:
        try:
            rec = compute_day(day)
        except requests.RequestException as e:
            print(f"[{day}] HA history request failed: {e}", file=sys.stderr)
            continue
        records[rec["date"]] = rec
        dw = rec["demand_window"]
        sol = rec["solar"]
        batt = rec["battery"]
        grid = rec["grid"]
        px = rec["price"]["day"]

        status = ("PASS" if dw["passed"] else "FAIL") if rec["peak_day"] else "off-pk"
        print(f"[{rec['date']}] {status}"
              f"  solar={sol['actual_kwh']}/{sol['forecast_kwh']}kWh"
              f"  soc={batt['soc_midnight']}→{batt['soc_9am']}→{batt['soc_3pm']}→{batt['soc_9pm']}%"
              f"  grid_in={grid['import_kwh']}kWh out={grid['export_kwh']}kWh"
              f"  price={px['min_c']}–{px['max_c']}¢"
              f"  demand_pk={dw['peak_30min_import_kw']}kW")

    write_all(records)
    print(f"-> {OUT_FILE} ({len(records)} day records)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
