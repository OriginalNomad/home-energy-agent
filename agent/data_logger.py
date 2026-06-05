#!/usr/bin/env python3
"""
Closed-loop data logger for the energy optimisation agent.

Writes one observation row per agent cycle (every ~30 min) to a local SQLite
database alongside this file: agent/energy_log.db

Two tables:
  observations    — system state snapshot + agent decision each cycle
  price_forecasts — Amber forecast slots linked to each observation

This is the foundation for self-calibrating forecast models (solar corrector,
charge rate model, price corrector, home load model) described in PRODUCT.md.

Usage from energy_agent.py:
    import data_logger
    data_logger.init_db()                          # call once at startup
    cycle_id = data_logger.log_cycle_start(state)  # after get_current_state()
    data_logger.log_price_forecast(cycle_id, fcast) # after get_price_forecast()
    data_logger.log_agent_decision(cycle_id, ...)   # inside log_decision handler

Run directly to inspect DB health:
    python data_logger.py
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import pytz

SYDNEY_TZ = pytz.timezone("Australia/Sydney")
DB_PATH   = Path(__file__).parent / "energy_log.db"

_DDL = """
CREATE TABLE IF NOT EXISTS observations (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                      TEXT    NOT NULL,   -- ISO8601 Sydney local time
    month                   INTEGER NOT NULL,
    hour                    INTEGER NOT NULL,
    day_of_week             INTEGER NOT NULL,   -- 0=Monday … 6=Sunday
    is_peak_month           INTEGER NOT NULL,   -- 0/1
    in_demand_window        INTEGER NOT NULL,   -- 0/1
    in_solar_sponge         INTEGER NOT NULL,   -- 0/1

    battery_soc_pct         REAL,
    battery_mode            TEXT,
    battery_reserve_pct     REAL,
    battery_grid_target_pct REAL,

    grid_price_cents        REAL,
    in_cheap_window         INTEGER,            -- 0/1

    solar_actual_kw         REAL,
    solcast_power_now_kw    REAL,
    solcast_this_hour_kwh   REAL,
    solcast_next_hour_kwh   REAL,
    solcast_remaining_kwh   REAL,
    forecast_accuracy       TEXT,

    home_load_kw            REAL,

    ev_plugged              INTEGER,            -- 0/1
    ev_soc_pct              REAL,              -- NULL when not plugged in
    ev_zappi_mode           TEXT,

    -- Filled in when the agent calls log_decision at end of cycle
    action_summary          TEXT,
    actions_taken_json      TEXT               -- JSON array, e.g. ["set_reserve(62%)"]
);

CREATE TABLE IF NOT EXISTS price_forecasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id  INTEGER NOT NULL REFERENCES observations(id),
    slot_ts         TEXT    NOT NULL,   -- ISO8601 start of forecast slot
    horizon_slots   INTEGER NOT NULL,   -- slots ahead from observation time (1 = next 30 min)
    forecast_cents  REAL    NOT NULL,
    descriptor      TEXT
);

CREATE INDEX IF NOT EXISTS idx_obs_ts ON observations    (ts);
CREATE INDEX IF NOT EXISTS idx_pf_obs ON price_forecasts (observation_id);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")  # safe for concurrent reads; resilient to crashes
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables and indexes. Safe to call on every agent startup."""
    with _conn() as conn:
        conn.executescript(_DDL)


def log_cycle_start(state: dict) -> int:
    """
    Insert an observation row from the state dict returned by get_current_state().
    Returns the row id — pass it to log_price_forecast() and log_agent_decision().
    """
    now     = datetime.now(SYDNEY_TZ)
    battery = state.get("battery", {})
    grid    = state.get("grid", {})
    solar   = state.get("solar", {})
    ev      = state.get("ev", {})

    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO observations (
                ts, month, hour, day_of_week,
                is_peak_month, in_demand_window, in_solar_sponge,
                battery_soc_pct, battery_mode, battery_reserve_pct, battery_grid_target_pct,
                grid_price_cents, in_cheap_window,
                solar_actual_kw, solcast_power_now_kw, solcast_this_hour_kwh,
                solcast_next_hour_kwh, solcast_remaining_kwh, forecast_accuracy,
                home_load_kw,
                ev_plugged, ev_soc_pct, ev_zappi_mode
            ) VALUES (?,?,?,?, ?,?,?, ?,?,?,?, ?,?, ?,?,?,?,?,?, ?, ?,?,?)
            """,
            (
                now.isoformat(),
                now.month, now.hour, now.weekday(),
                int(state.get("is_peak_month", False)),
                int(state.get("in_demand_window", False)),
                int(state.get("in_solar_sponge", False)),
                battery.get("soc_pct"),
                battery.get("mode"),
                battery.get("reserve_pct"),
                battery.get("grid_target_pct"),
                grid.get("price_cents_kwh"),
                int(grid.get("in_cheap_window", False)),
                solar.get("current_kw"),
                solar.get("solcast_power_now_kw"),
                solar.get("forecast_this_hour_kwh"),
                solar.get("forecast_next_hour_kwh"),
                solar.get("forecast_remaining_kwh"),
                solar.get("forecast_accuracy"),
                state.get("home_load_kw"),
                int(ev.get("plugged_in", False)),
                ev.get("soc_pct"),
                ev.get("zappi_mode") if ev.get("plugged_in") else None,
            ),
        )
        return cur.lastrowid


def log_price_forecast(cycle_id: int, forecast: List[dict]):
    """Store price forecast slots linked to this cycle's observation."""
    rows = [
        (
            cycle_id,
            slot.get("time", ""),
            i + 1,
            slot.get("cents_kwh", 0),
            slot.get("descriptor", ""),
        )
        for i, slot in enumerate(forecast)
    ]
    with _conn() as conn:
        conn.executemany(
            """
            INSERT INTO price_forecasts (observation_id, slot_ts, horizon_slots, forecast_cents, descriptor)
            VALUES (?,?,?,?,?)
            """,
            rows,
        )


def log_agent_decision(cycle_id: int, summary: str, actions: List[str]):
    """Update the observation row with the agent's decision. Called when log_decision fires."""
    with _conn() as conn:
        conn.execute(
            "UPDATE observations SET action_summary=?, actions_taken_json=? WHERE id=?",
            (summary, json.dumps(actions), cycle_id),
        )


# ---------------------------------------------------------------------------
# CLI diagnostics — run this file directly to check DB health
# ---------------------------------------------------------------------------

def print_stats():
    if not DB_PATH.exists():
        print(f"No database yet at {DB_PATH}")
        print("It will be created automatically on the next agent cycle.")
        return

    with _conn() as conn:
        obs = conn.execute(
            "SELECT COUNT(*) as n, MIN(ts) as first, MAX(ts) as last FROM observations"
        ).fetchone()
        undecided = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE action_summary IS NULL"
        ).fetchone()[0]
        pf_count = conn.execute("SELECT COUNT(*) FROM price_forecasts").fetchone()[0]

        recent = conn.execute(
            """
            SELECT ts, battery_soc_pct, grid_price_cents, solar_actual_kw,
                   battery_mode, action_summary
            FROM observations ORDER BY id DESC LIMIT 5
            """
        ).fetchall()

    size_kb = DB_PATH.stat().st_size / 1024
    print(f"DB path  : {DB_PATH}")
    print(f"Size     : {size_kb:.1f} KB")
    print(f"Obs rows : {obs['n']}  ({obs['first']} → {obs['last']})")
    print(f"Undecided: {undecided}  (rows where agent didn't call log_decision — check for crashes)")
    print(f"PF rows  : {pf_count}  (price forecast slots)")

    if recent:
        print("\nMost recent 5 observations:")
        print(f"  {'Timestamp':<22} {'SoC':>4} {'¢/kWh':>6} {'Solar':>6} {'Mode':<18} Decision")
        print(f"  {'-'*22} {'-'*4} {'-'*6} {'-'*6} {'-'*18} {'-'*30}")
        for r in recent:
            soc   = f"{r['battery_soc_pct']:.0f}%" if r['battery_soc_pct'] is not None else "—"
            price = f"{r['grid_price_cents']:.1f}"  if r['grid_price_cents']  is not None else "—"
            solar = f"{r['solar_actual_kw']:.2f}"   if r['solar_actual_kw']   is not None else "—"
            mode  = r['battery_mode'] or "—"
            summary = (r['action_summary'] or "—")[:50]
            print(f"  {r['ts'][:22]:<22} {soc:>4} {price:>6} {solar:>6} {mode:<18} {summary}")


if __name__ == "__main__":
    print_stats()
