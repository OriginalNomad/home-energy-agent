#!/usr/bin/env python3
"""
billing_data.py
===============
Fetches monthly billing data from the Amber Electric API and pushes
it to HA sensors for the billing dashboard.

Pushes:
  - sensor.billing_monthly_data   (monthly bill totals + demand charges as attributes)

Run daily via cron on the Pi, or manually:
    cd ~/home-energy-agent/agent && source venv/bin/activate && python3 billing_data.py
"""

import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import pytz
import requests

_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

HA_URL     = os.environ.get("HA_URL", "http://localhost:8123")
HA_TOKEN   = os.environ.get("HA_TOKEN", "")
HA_HEADERS = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}

AMBER_API_KEY = os.environ.get("AMBER_API_KEY", "")
AMBER_BASE    = "https://api.amber.com.au/v1"
AMBER_HEADERS = {"accept": "application/json", "Authorization": f"Bearer {AMBER_API_KEY}"}
SITE_ID       = "01E8RD8PYWENHX1CKR9MX38K69"

TZ = pytz.timezone("Australia/Sydney")

# Peak demand months (Ausgrid EA116): Nov-Mar (summer) + Jun-Aug (winter)
DEMAND_MONTHS = {1, 2, 3, 6, 7, 8, 11, 12}

# Fixed charges (ex-GST, from recent bills)
DAILY_SUPPLY_RATE = 1.0994       # $/day (Jul 2026 rate)
AMBER_MEMBERSHIP  = 23.16        # $/month (Jul 2026)
GST_RATE          = 0.10

# Historical bill data from PDFs (months before API coverage).
# Format: {month: {bill_total, usage_cost, supply_cost, demand_cost, amber_fee,
#                   gst, export_credit, usage_kwh, export_kwh}}
HISTORICAL_BILLS = {
    "2026-01": {"bill_total": 128.46, "usage_cost": 23.89, "supply_cost": 29.74,
                "demand_cost": 88.59,  "amber_fee": 23.16, "gst": 16.53,
                "export_credit": 53.45, "usage_kwh": 283.9, "export_kwh": 219.7},
    "2026-02": {"bill_total": 111.31, "usage_cost": 34.09, "supply_cost": 26.86,
                "demand_cost": 62.60,  "amber_fee": 20.92, "gst": 14.43,
                "export_credit": 47.59, "usage_kwh": 344.3, "export_kwh": 88.9},
    "2026-03": {"bill_total": 193.33, "usage_cost": 37.23, "supply_cost": 29.74,
                "demand_cost": 89.35,  "amber_fee": 23.16, "gst": 17.94,
                "export_credit": 4.09, "usage_kwh": 340.8, "export_kwh": 60.2},
    "2026-04": {"bill_total": 82.80,  "usage_cost": 24.62, "supply_cost": 28.77,
                "demand_cost": 0,      "amber_fee": 22.42, "gst": 7.58,
                "export_credit": 0.59, "usage_kwh": 284.3, "export_kwh": 12.4},
    "2026-05": {"bill_total": 119.09, "usage_cost": 55.93, "supply_cost": 29.74,
                "demand_cost": 0,      "amber_fee": 23.16, "gst": 10.90,
                "export_credit": 0.64, "usage_kwh": 508.6, "export_kwh": 8.3},
    "2026-06": {"bill_total": 140.41, "usage_cost": 46.28, "supply_cost": 28.77,
                "demand_cost": 31.11,  "amber_fee": 22.42, "gst": 12.86,
                "export_credit": 1.03, "usage_kwh": 341.4, "export_kwh": 11.1},
    "2026-07": {"bill_total": 132.97, "usage_cost": 58.16, "supply_cost": 34.08,
                "demand_cost": 6.49,   "amber_fee": 23.16, "gst": 12.20,
                "export_credit": 1.12, "usage_kwh": 481.7, "export_kwh": 19.7},
}


def _amber_get(path):
    r = requests.get(AMBER_BASE + path, headers=AMBER_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_current_month_from_api():
    """Fetch the current (unbilled) month's usage data from Amber API."""
    today = date.today()
    month_start = today.replace(day=1)

    records = []
    end = today
    while end >= month_start:
        start = max(end - timedelta(days=7), month_start)
        try:
            data = _amber_get(
                f"/sites/{SITE_ID}/usage"
                f"?startDate={start.isoformat()}&endDate={end.isoformat()}"
            )
            records.extend(data)
        except Exception as e:
            print(f"  API error {start}→{end}: {e}", file=sys.stderr)
        end = start - timedelta(days=1)
        time.sleep(0.05)

    import_cost_c = 0.0
    import_kwh = 0.0
    export_cost_c = 0.0
    export_kwh = 0.0
    demand_kw_intervals = []
    days_seen = set()

    for rec in records:
        ch = rec.get("channelType", "")
        cost = float(rec.get("cost", 0) or 0)
        kwh = float(rec.get("kwh", 0) or 0)
        duration = int(rec.get("duration", 5))
        ti = rec.get("tariffInformation", {})
        dw = ti.get("demandWindow", False) if ti else False
        days_seen.add(rec.get("date", ""))

        if ch == "general":
            import_cost_c += cost
            import_kwh += kwh
            if dw and kwh > 0:
                kw = kwh / (duration / 60.0)
                demand_kw_intervals.append(kw)
        elif ch == "feedIn":
            export_cost_c += cost
            export_kwh += kwh

    days_in_month = len(days_seen)
    month_num = today.month
    is_demand_month = month_num in DEMAND_MONTHS

    usage_cost = import_cost_c / 100.0
    export_credit = abs(export_cost_c / 100.0)
    supply_cost = days_in_month * DAILY_SUPPLY_RATE

    peak_kw = max(demand_kw_intervals) if demand_kw_intervals else 0.0
    # Estimate demand charge from peak kW (derived from Jul 2026 bill: ~$5.15/kW)
    demand_rate_per_kw = 5.15
    demand_cost = peak_kw * demand_rate_per_kw if is_demand_month else 0.0

    subtotal = usage_cost + supply_cost + demand_cost + AMBER_MEMBERSHIP
    gst = subtotal * GST_RATE
    bill_estimate = subtotal + gst - export_credit

    # Pro-rate to full month estimate
    if days_in_month > 0:
        days_total = (date(today.year, today.month + 1, 1) - timedelta(days=1)).day if today.month < 12 else 31
        scale = days_total / days_in_month
    else:
        scale = 1.0

    return {
        "bill_total": round(bill_estimate, 2),
        "bill_total_projected": round(bill_estimate * scale, 2),
        "usage_cost": round(usage_cost, 2),
        "supply_cost": round(supply_cost, 2),
        "demand_cost": round(demand_cost, 2),
        "amber_fee": AMBER_MEMBERSHIP,
        "gst": round(gst, 2),
        "export_credit": round(export_credit, 2),
        "usage_kwh": round(import_kwh, 1),
        "export_kwh": round(export_kwh, 1),
        "peak_demand_kw": round(peak_kw, 2),
        "days_billed": days_in_month,
        "is_estimate": True,
    }


def push_sensor(entity_id, state, attributes):
    """Push a sensor state + attributes to HA."""
    payload = {"state": state, "attributes": attributes}
    url = f"{HA_URL}/api/states/{entity_id}"
    r = requests.post(url, headers=HA_HEADERS, json=payload, timeout=10)
    if not r.ok:
        print(f"  HA push failed for {entity_id}: {r.status_code} {r.text[:200]}", file=sys.stderr)
    return r.ok


def main():
    print("billing_data.py — fetching monthly billing data")

    # Build the monthly dataset: historical + current month from API
    months = {}
    for mo, data in sorted(HISTORICAL_BILLS.items()):
        months[mo] = data

    # Fetch current month from API
    today = date.today()
    current_month = today.strftime("%Y-%m")
    if current_month not in months:
        print(f"  Fetching {current_month} from Amber API...")
        try:
            current = fetch_current_month_from_api()
            months[current_month] = current
            print(f"  {current_month}: ${current['bill_total']:.2f} "
                  f"(projected ${current['bill_total_projected']:.2f}), "
                  f"demand ${current['demand_cost']:.2f}, "
                  f"peak {current['peak_demand_kw']:.2f} kW")
        except Exception as e:
            print(f"  Error fetching current month: {e}", file=sys.stderr)

    # Push to HA: one sensor with all months as attributes
    sorted_months = sorted(months.keys())
    latest = sorted_months[-1] if sorted_months else "unknown"
    latest_total = months.get(latest, {}).get("bill_total", 0)

    # Build attributes with per-month data
    attrs = {
        "friendly_name": "Monthly Billing Data",
        "icon": "mdi:currency-usd",
        "unit_of_measurement": "$",
        "months": json.dumps(sorted_months),
        "updated": today.isoformat(),
    }

    # Add per-month attributes for easy template access
    bill_totals = []
    demand_charges = []
    usage_costs = []
    for mo in sorted_months:
        d = months[mo]
        bill_totals.append(d.get("bill_total", 0))
        demand_charges.append(d.get("demand_cost", 0))
        usage_costs.append(d.get("usage_cost", 0))
        attrs[f"{mo}_bill_total"] = d.get("bill_total", 0)
        attrs[f"{mo}_demand_cost"] = d.get("demand_cost", 0)
        attrs[f"{mo}_usage_cost"] = d.get("usage_cost", 0)
        attrs[f"{mo}_usage_kwh"] = d.get("usage_kwh", 0)
        attrs[f"{mo}_export_credit"] = d.get("export_credit", 0)
        attrs[f"{mo}_supply_cost"] = d.get("supply_cost", 0)
        attrs[f"{mo}_amber_fee"] = d.get("amber_fee", 0)
        attrs[f"{mo}_gst"] = d.get("gst", 0)
        attrs[f"{mo}_export_kwh"] = d.get("export_kwh", 0)
        if d.get("is_estimate"):
            attrs[f"{mo}_is_estimate"] = True
            attrs[f"{mo}_projected"] = d.get("bill_total_projected", 0)
            attrs[f"{mo}_peak_demand_kw"] = d.get("peak_demand_kw", 0)

    attrs["bill_totals"] = json.dumps(bill_totals)
    attrs["demand_charges"] = json.dumps(demand_charges)
    attrs["usage_costs"] = json.dumps(usage_costs)

    ok = push_sensor("sensor.billing_monthly_data", round(latest_total, 2), attrs)
    print(f"  sensor.billing_monthly_data → {'OK' if ok else 'FAILED'} (latest: {latest} ${latest_total:.2f})")

    # Also push individual sensors for the current month for easy card use
    if current_month in months:
        d = months[current_month]
        push_sensor("sensor.billing_current_month_total", d.get("bill_total", 0), {
            "friendly_name": "Current Month Bill",
            "icon": "mdi:receipt-text",
            "unit_of_measurement": "$",
            "projected": d.get("bill_total_projected", 0),
            "days_billed": d.get("days_billed", 0),
        })
        push_sensor("sensor.billing_current_month_demand", d.get("demand_cost", 0), {
            "friendly_name": "Current Month Demand Charge",
            "icon": "mdi:flash-alert",
            "unit_of_measurement": "$",
            "peak_demand_kw": d.get("peak_demand_kw", 0),
        })

    print("Done.")


if __name__ == "__main__":
    main()
