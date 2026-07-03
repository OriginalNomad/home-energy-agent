#!/usr/bin/env python3
"""
Demand-window summary → HA sensor
=================================
Reads agent/daily_energy.jsonl (written by log_daily_energy.py) and publishes
a rolling summary into Home Assistant as `sensor.demand_window_monitor` via the
REST API. No HA config change required — the state is pushed from the host, so
this sidesteps the container/repo config divergence entirely.

The sensor's STATE is this calendar month's peak 30-minute import (kW) — the
single number that EA116 actually bills (ea116_tariff.md §1: monthly max). Its
attributes carry the rolling per-day history the dashboard cards render.

State pushed via /api/states is not persisted across HA restarts, so run this on
a short cron (hourly) to keep the sensor alive between the daily 21:05 recompute.

    python3 demand_window_summary.py            # print JSON summary
    python3 demand_window_summary.py --post     # push to HA
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytz
import requests

# Single source of truth for the demand-window bands / rate lives in
# log_daily_energy.py so the JSONL writer and this publisher never drift.
try:
    from log_daily_energy import (
        classify_demand, demand_cost_estimate, DEMAND_RATE_DOLLARS_PER_KW,
    )
except Exception:  # pragma: no cover - fallback if run in isolation
    DEMAND_MARGINAL_KW, DEMAND_BREACH_KW = 0.50, 1.50
    DEMAND_RATE_DOLLARS_PER_KW = 11.5479

    def classify_demand(peak_kw: float) -> str:
        if peak_kw >= DEMAND_BREACH_KW:
            return "breach"
        if peak_kw >= DEMAND_MARGINAL_KW:
            return "marginal"
        return "pass"

    def demand_cost_estimate(peak_kw: float) -> float:
        return round(max(peak_kw, 0.0) * DEMAND_RATE_DOLLARS_PER_KW, 2)

# Load .env if present (same pattern as energy_agent.py)
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            if not os.environ.get(_k.strip()):
                os.environ[_k.strip()] = _v.strip().strip('"').strip("'")

HA_URL   = os.environ.get("HA_URL",   "http://localhost:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiIxZjQxNDVmOTBjYTI0ZDgyYjk5MTI5ZjE2YzY3ZWEzNSIsImlhdCI6MTc3OTY2OTMyNiwiZXhwIjoyMDk1MDI5MzI2fQ.Gu5FPRLbn3PpTOstsR-B87fyVeEC00dRXAB6ZiYiFt0")
HA_HEADERS = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
TZ = pytz.timezone("Australia/Sydney")

JSONL  = Path(__file__).parent / "daily_energy.jsonl"
ENTITY = "sensor.demand_window_monitor"
ROLLING_DAYS = 30


def load_records() -> list[dict]:
    out = []
    if JSONL.exists():
        for line in JSONL.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return sorted(out, key=lambda r: r.get("date", ""))


def build_summary(records: list[dict]) -> dict:
    now = datetime.now(TZ)
    today_str = now.strftime("%Y-%m-%d")
    # Exclude today's record if the demand window hasn't closed yet (before 21:05).
    # An intra-day run produces a partial result that would show a false pass/fail.
    if records and records[-1].get("date") == today_str and now.hour < 21:
        records = records[:-1]

    recent = records[-ROLLING_DAYS:]
    this_month = now.strftime("%Y-%m")

    def dw(r):
        return r.get("demand_window") or {}

    def status_of(r):
        # Always classify from the billed 30-min-avg kW so old records (written
        # before the three-way bands existed) are re-scored under current rules.
        return classify_demand(dw(r).get("peak_30min_import_kw", 0.0) or 0.0)

    month_peaks = [dw(r).get("peak_30min_import_kw", 0) for r in records
                   if r.get("peak_day") and r.get("date", "").startswith(this_month)]
    month_peak_kw = round(max(month_peaks), 3) if month_peaks else 0.0
    month_cost_est = demand_cost_estimate(month_peak_kw)

    peak_days = [r for r in recent if r.get("peak_day")]
    days_passed   = sum(1 for r in peak_days if status_of(r) == "pass")
    days_marginal = sum(1 for r in peak_days if status_of(r) == "marginal")
    days_breached = sum(1 for r in peak_days if status_of(r) == "breach")

    last = records[-1] if records else {}
    last_status = status_of(last) if last.get("peak_day") else "off-peak"

    days = [{
        "date": r["date"],
        "peak_day": r.get("peak_day", False),
        "status": status_of(r) if r.get("peak_day") else None,
        "passed": (status_of(r) == "pass") if r.get("peak_day") else None,
        "peak_kw": dw(r).get("peak_30min_import_kw", 0.0),
        "peak_at": dw(r).get("peak_at", ""),
        "cost_est_dollars": demand_cost_estimate(dw(r).get("peak_30min_import_kw", 0.0) or 0.0)
                            if r.get("peak_day") else None,
        "min_soc_pct": dw(r).get("min_soc_pct"),
    } for r in recent]

    return {
        "this_month": this_month,
        "this_month_peak_kw": month_peak_kw,
        "this_month_cost_est_dollars": month_cost_est,
        "demand_rate_dollars_per_kw": round(DEMAND_RATE_DOLLARS_PER_KW, 4),
        "days_passed": days_passed,
        "days_marginal": days_marginal,
        "days_failed": days_breached,   # kept as days_failed for card back-compat: breaches only
        "days_breached": days_breached,
        "last_date": last.get("date"),
        "last_status": last_status,
        "days": days,
    }


def post_to_ha(summary: dict) -> None:
    body = {
        "state": summary["this_month_peak_kw"],
        "attributes": {
            "unit_of_measurement": "kW",
            "friendly_name": "Demand Window — month peak 30min import",
            "icon": "mdi:transmission-tower-import",
            **summary,
        },
    }
    r = requests.post(f"{HA_URL}/api/states/{ENTITY}", headers=HA_HEADERS,
                      json=body, timeout=15)
    r.raise_for_status()
    print(f"posted {ENTITY} = {summary['this_month_peak_kw']} kW "
          f"(~${summary['this_month_cost_est_dollars']}/mo demand charge) — "
          f"{summary['days_passed']} pass / {summary['days_marginal']} marginal / "
          f"{summary['days_failed']} breach this window")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true", help="push to HA (default: print)")
    args = ap.parse_args()

    summary = build_summary(load_records())
    if args.post:
        post_to_ha(summary)
    else:
        print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
