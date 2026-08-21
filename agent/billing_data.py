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
SITE_ID       = os.environ.get("AMBER_SITE_ID", "")

TZ = pytz.timezone("Australia/Sydney")

# Peak demand months (Ausgrid EA116): Nov-Mar (summer) + Jun-Aug (winter)
DEMAND_MONTHS = {1, 2, 3, 6, 7, 8, 11, 12}

# Fixed charges (ex-GST, from recent bills)
DAILY_SUPPLY_RATE = 1.0994       # $/day (Jul 2026 rate)
AMBER_MEMBERSHIP  = 23.16        # $/month (Jul 2026)
GST_RATE          = 0.10

# Historical bill data from PDFs + bill_comparison_36months.csv.
# Format: {month: {bill_total, usage_cost, supply_cost, demand_cost, amber_fee,
#                   gst, export_credit, usage_kwh, export_kwh}}
# Covers Jan 2023 – Jul 2026. Demand tariff started ~Jul 2024.
# Months marked [R] include a $75 NSW Energy Bill Relief rebate in bill_total
# not reflected in export_credit (solar export only).
# export_kwh sourced from PDF bills for all months.
HISTORICAL_BILLS = {
    "2023-01": {"bill_total": -77.35,  "usage_cost": 61.12, "supply_cost": 31.74,
                "demand_cost": 0,      "amber_fee": 13.90, "gst": 10.67,
                "export_credit": 182.29, "usage_kwh": 379.0, "export_kwh": 462.0},
    "2023-02": {"bill_total": -98.54,  "usage_cost": 49.04, "supply_cost": 28.67,
                "demand_cost": 0,      "amber_fee": 12.55, "gst": 9.04,
                "export_credit": 170.87, "usage_kwh": 284.0, "export_kwh": 442.0},
    "2023-03": {"bill_total": -106.55, "usage_cost": 67.69, "supply_cost": 31.74,
                "demand_cost": 0,      "amber_fee": 13.90, "gst": 11.34,
                "export_credit": 216.23, "usage_kwh": 397.5, "export_kwh": 395.0},
    "2023-04": {"bill_total": -3.91,   "usage_cost": 76.83, "supply_cost": 30.72,
                "demand_cost": 0,      "amber_fee": 13.45, "gst": 12.09,
                "export_credit": 137.00, "usage_kwh": 456.0, "export_kwh": 287.0},
    "2023-05": {"bill_total": -69.42,  "usage_cost": 109.82, "supply_cost": 31.74,
                "demand_cost": 0,      "amber_fee": 13.90, "gst": 15.56,
                "export_credit": 240.44, "usage_kwh": 543.5, "export_kwh": 272.0},
    "2023-06": {"bill_total": 13.12,   "usage_cost": 73.54, "supply_cost": 30.72,
                "demand_cost": 0,      "amber_fee": 13.45, "gst": 11.75,
                "export_credit": 116.34, "usage_kwh": 468.5, "export_kwh": 218.0},
    "2023-07": {"bill_total": 11.43,   "usage_cost": 61.92, "supply_cost": 27.71,
                "demand_cost": 0,      "amber_fee": 17.60, "gst": 10.72,
                "export_credit": 102.52, "usage_kwh": 447.2, "export_kwh": 244.0},
    "2023-08": {"bill_total": -27.16,  "usage_cost": 60.66, "supply_cost": 27.71,
                "demand_cost": 0,      "amber_fee": 17.60, "gst": 10.58,
                "export_credit": 139.71, "usage_kwh": 408.3, "export_kwh": 266.0},
    "2023-09": {"bill_total": -71.34,  "usage_cost": 22.54, "supply_cost": 26.81,
                "demand_cost": 0,      "amber_fee": 17.04, "gst": 6.64,
                "export_credit": 140.37, "usage_kwh": 184.3, "export_kwh": 317.0},
    "2023-10": {"bill_total": -55.17,  "usage_cost": 20.40, "supply_cost": 27.71,
                "demand_cost": 0,      "amber_fee": 17.60, "gst": 6.56,
                "export_credit": 127.44, "usage_kwh": 178.2, "export_kwh": 386.0},
    "2023-11": {"bill_total": -77.29,  "usage_cost": 27.30, "supply_cost": 26.81,
                "demand_cost": 0,      "amber_fee": 17.04, "gst": 7.10,
                "export_credit": 136.54, "usage_kwh": 165.7, "export_kwh": 321.0},
    "2023-12": {"bill_total": -64.05,  "usage_cost": 36.30, "supply_cost": 27.71,
                "demand_cost": 0,      "amber_fee": 17.60, "gst": 8.13,
                "export_credit": 153.79, "usage_kwh": 227.1, "export_kwh": 401.0},
    "2024-01": {"bill_total": -76.32,  "usage_cost": 40.66, "supply_cost": 27.71,
                "demand_cost": 0,      "amber_fee": 17.60, "gst": 8.58,
                "export_credit": 170.87, "usage_kwh": 237.8, "export_kwh": 432.0},
    "2024-02": {"bill_total": -93.87,  "usage_cost": 45.90, "supply_cost": 25.91,
                "demand_cost": 0,      "amber_fee": 16.47, "gst": 8.85,
                "export_credit": 191.00, "usage_kwh": 270.3, "export_kwh": 346.0},
    "2024-03": {"bill_total": -19.72,  "usage_cost": 37.56, "supply_cost": 27.71,
                "demand_cost": 0,      "amber_fee": 17.60, "gst": 8.27,
                "export_credit": 110.86, "usage_kwh": 278.1, "export_kwh": 300.0},
    "2024-04": {"bill_total": -20.32,  "usage_cost": 38.09, "supply_cost": 26.81,
                "demand_cost": 0,      "amber_fee": 17.04, "gst": 8.18,
                "export_credit": 110.44, "usage_kwh": 275.8, "export_kwh": 253.0},
    "2024-05": {"bill_total": -221.60, "usage_cost": 90.20, "supply_cost": 27.71,
                "demand_cost": 0,      "amber_fee": 17.60, "gst": 13.57,
                "export_credit": 370.68, "usage_kwh": 483.9, "export_kwh": 217.0},
    "2024-06": {"bill_total": 35.07,   "usage_cost": 90.72, "supply_cost": 26.81,
                "demand_cost": 0,      "amber_fee": 17.04, "gst": 13.45,
                "export_credit": 112.95, "usage_kwh": 461.8, "export_kwh": 204.0},
    "2024-07": {"bill_total": 54.71,   "usage_cost": 50.97, "supply_cost": 28.72,
                "demand_cost": 20.13,  "amber_fee": 17.60, "gst": 11.75,
                "export_credit": 74.46, "usage_kwh": 374.1, "export_kwh": 180.0},
    "2024-08": {"bill_total": -115.94, "usage_cost": 59.07, "supply_cost": 28.72,  # [R]
                "demand_cost": 19.47,  "amber_fee": 20.38, "gst": 12.79,
                "export_credit": 181.37, "usage_kwh": 357.5, "export_kwh": 123.0},
    "2024-09": {"bill_total": 40.74,   "usage_cost": 15.54, "supply_cost": 27.79,
                "demand_cost": 0,      "amber_fee": 19.73, "gst": 6.31,
                "export_credit": 28.63, "usage_kwh": 139.2, "export_kwh": 94.0},
    "2024-10": {"bill_total": -55.53,  "usage_cost": 10.50, "supply_cost": 28.72,  # [R]
                "demand_cost": 0,      "amber_fee": 20.38, "gst": 5.98,
                "export_credit": 46.11, "usage_kwh": 77.1,  "export_kwh": 214.0},
    "2024-11": {"bill_total": -133.86, "usage_cost": 16.75, "supply_cost": 27.79,
                "demand_cost": 15.24,  "amber_fee": 19.73, "gst": 7.95,
                "export_credit": 221.32, "usage_kwh": 97.3, "export_kwh": 326.0},
    "2024-12": {"bill_total": -109.90, "usage_cost": 11.46, "supply_cost": 28.72,
                "demand_cost": 5.06,   "amber_fee": 20.38, "gst": 6.59,
                "export_credit": 182.11, "usage_kwh": 57.3, "export_kwh": 341.0},
    "2025-01": {"bill_total": 9.41,    "usage_cost": 18.04, "supply_cost": 28.72,  # [R]
                "demand_cost": 57.26,  "amber_fee": 20.38, "gst": 12.45,
                "export_credit": 52.44, "usage_kwh": 135.9, "export_kwh": 184.0},
    "2025-02": {"bill_total": 15.80,   "usage_cost": 11.06, "supply_cost": 25.94,
                "demand_cost": 11.34,  "amber_fee": 18.41, "gst": 6.67,
                "export_credit": 57.62, "usage_kwh": 82.0,  "export_kwh": 225.0},
    "2025-03": {"bill_total": 44.24,   "usage_cost": 21.52, "supply_cost": 28.72,
                "demand_cost": 23.49,  "amber_fee": 20.38, "gst": 9.43,
                "export_credit": 59.30, "usage_kwh": 150.7, "export_kwh": 120.0},
    "2025-04": {"bill_total": -63.72,  "usage_cost": 25.14, "supply_cost": 27.79,  # [R]
                "demand_cost": 0,      "amber_fee": 19.73, "gst": 7.27,
                "export_credit": 68.65, "usage_kwh": 195.1, "export_kwh": 153.0},
    "2025-05": {"bill_total": -27.15,  "usage_cost": 40.01, "supply_cost": 28.73,
                "demand_cost": 0,      "amber_fee": 20.38, "gst": 9.30,
                "export_credit": 125.57, "usage_kwh": 333.8, "export_kwh": 161.2},
    "2025-06": {"bill_total": -98.17,  "usage_cost": 49.64, "supply_cost": 27.79,
                "demand_cost": 24.79,  "amber_fee": 19.73, "gst": 12.50,
                "export_credit": 232.62, "usage_kwh": 335.8, "export_kwh": 143.1},
    "2025-07": {"bill_total": 111.50,  "usage_cost": 58.48, "supply_cost": 29.74,
                "demand_cost": 32.27,  "amber_fee": 20.38, "gst": 14.09,
                "export_credit": 43.46, "usage_kwh": 452.3, "export_kwh": 174.1},
    "2025-08": {"bill_total": 146.35,  "usage_cost": 64.69, "supply_cost": 29.74,
                "demand_cost": 51.72,  "amber_fee": 23.16, "gst": 16.93,
                "export_credit": 39.89, "usage_kwh": 522.6, "export_kwh": 152.3},
    "2025-09": {"bill_total": -42.74,  "usage_cost": 15.94, "supply_cost": 28.77,  # [R]
                "demand_cost": 0,      "amber_fee": 22.42, "gst": 6.71,
                "export_credit": 41.58, "usage_kwh": 119.2, "export_kwh": 190.0},
    "2025-10": {"bill_total": -59.23,  "usage_cost": 16.04, "supply_cost": 29.74,  # [R]
                "demand_cost": 0,      "amber_fee": 23.16, "gst": 6.89,
                "export_credit": 60.06, "usage_kwh": 132.1, "export_kwh": 228.9},
    "2025-11": {"bill_total": -33.86,  "usage_cost": 7.99,  "supply_cost": 28.77,
                "demand_cost": 1.06,   "amber_fee": 22.42, "gst": 6.02,
                "export_credit": 100.12, "usage_kwh": 82.4, "export_kwh": 244.0},
    "2025-12": {"bill_total": 143.60,  "usage_cost": 37.22, "supply_cost": 29.74,
                "demand_cost": 88.14,  "amber_fee": 23.16, "gst": 17.83,
                "export_credit": 52.49, "usage_kwh": 489.2, "export_kwh": 148.1},
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

    # Push to HA: one sensor with completed months only (exclude current month estimate)
    sorted_months = [m for m in sorted(months.keys()) if not months[m].get("is_estimate")]
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
