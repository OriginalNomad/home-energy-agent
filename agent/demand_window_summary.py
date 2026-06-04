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
import sys
from datetime import datetime
from pathlib import Path

import pytz
import requests

HA_URL   = "http://localhost:8123"
HA_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiIxZjQxNDVmOTBjYTI0ZDgyYjk5MTI5ZjE2YzY3ZWEzNSIsImlhdCI6MTc3OTY2OTMyNiwiZXhwIjoyMDk1MDI5MzI2fQ.Gu5FPRLbn3PpTOstsR-B87fyVeEC00dRXAB6ZiYiFt0"
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

    month_peaks = [dw(r).get("peak_30min_import_kw", 0) for r in records
                   if r.get("peak_day") and r.get("date", "").startswith(this_month)]
    month_peak_kw = round(max(month_peaks), 3) if month_peaks else 0.0

    peak_days = [r for r in recent if r.get("peak_day")]
    days_passed = sum(1 for r in peak_days if dw(r).get("passed"))
    days_failed = sum(1 for r in peak_days if dw(r).get("passed") is False)

    last = records[-1] if records else {}
    last_dw = dw(last)
    last_status = ("pass" if last_dw.get("passed") else "fail") if last.get("peak_day") else "off-peak"

    days = [{
        "date": r["date"],
        "peak_day": r.get("peak_day", False),
        "passed": dw(r).get("passed"),
        "peak_kw": dw(r).get("peak_30min_import_kw", 0.0),
        "peak_at": dw(r).get("peak_at", ""),
        "min_soc_pct": dw(r).get("min_soc_pct"),
    } for r in recent]

    return {
        "this_month": this_month,
        "this_month_peak_kw": month_peak_kw,
        "days_passed": days_passed,
        "days_failed": days_failed,
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
          f"({summary['days_passed']} passed / {summary['days_failed']} failed this window)")


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
