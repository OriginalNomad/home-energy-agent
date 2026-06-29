#!/usr/bin/env python3
"""Build calibration models from energy_log.db and update model_params.json.

Builds two models:
  - solar_correction: per-hour-of-day ratio of actual/Solcast to correct systematic bias
  - charge_rate_kw: observed average charge rate by SoC bucket × mode

Run on the Pi after at least 2 weeks of logged data:
    cd ~/home-energy-agent/agent
    python build_models.py
    git add model_params.json && git commit -m "model_params: rebuild $(date +%Y-%m-%d)"
    git push
"""

import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE      = Path(__file__).parent
DB_PATH   = HERE / "energy_log.db"
PARAMS_PATH = HERE / "model_params.json"

MIN_SAMPLES  = 5
USABLE_KWH   = 13.5
MAX_RATE_CAP = 8.0   # kW — filter sensor glitches
MAX_JUMP_PCT = 20.0  # % SoC per 30 min — filter sensor jumps


def build_solar_correction(conn: sqlite3.Connection) -> dict:
    """Per-hour-of-day ratio: actual_kw / solcast_power_now_kw.

    Only uses observations where Solcast predicts meaningful generation (> 0.3 kW)
    to avoid noise from dawn/dusk transitions and night readings.
    Caps individual ratios at 3.0 to guard against outliers.
    """
    rows = conn.execute("""
        SELECT
            CAST(strftime('%H', ts) AS INT) AS hour,
            solar_actual_kw,
            solcast_power_now_kw
        FROM observations
        WHERE solcast_power_now_kw > 0.3
          AND solar_actual_kw IS NOT NULL
        ORDER BY hour
    """).fetchall()

    by_hour: dict[int, list[float]] = defaultdict(list)
    for hour, actual, forecast in rows:
        if forecast and forecast > 0:
            ratio = actual / forecast
            if 0.0 <= ratio <= 3.0:
                by_hour[int(hour)].append(ratio)

    result = {}
    for hour, ratios in sorted(by_hour.items()):
        n    = len(ratios)
        mean = sum(ratios) / n
        var  = sum((r - mean) ** 2 for r in ratios) / max(n - 1, 1)
        result[str(hour)] = {
            "ratio":       round(mean, 4),
            "uncertainty": round(math.sqrt(var), 4),
            "n":           n,
        }

    return result


def build_charge_rate_model(conn: sqlite3.Connection) -> dict:
    """Observed average charge rate (kW) by SoC decile × mode.

    Joins consecutive observation rows (id+1) where the battery was charging in
    the same mode, computes delta_soc/0.5h converted to kW, and aggregates.
    Filters out sensor jumps (>20 % SoC change per interval) and implausible
    rates (>8 kW).
    """
    rows = conn.execute("""
        SELECT
            (CAST(a.battery_soc_pct / 10 AS INT) * 10)   AS soc_bucket,
            a.battery_mode                                 AS mode,
            (b.battery_soc_pct - a.battery_soc_pct)       AS delta_pct
        FROM observations a
        JOIN observations b ON b.id = a.id + 1
        WHERE b.battery_soc_pct > a.battery_soc_pct
          AND a.battery_mode = b.battery_mode
          AND a.battery_mode IN ('self_consumption', 'autonomous')
          AND (b.battery_soc_pct - a.battery_soc_pct) < ?
    """, (MAX_JUMP_PCT,)).fetchall()

    by_mode_bucket: dict[tuple, list[float]] = defaultdict(list)
    for bucket, mode, delta_pct in rows:
        rate_kw = delta_pct / 100.0 * USABLE_KWH / 0.5   # kWh per 30 min → kW
        if 0 < rate_kw < MAX_RATE_CAP:
            by_mode_bucket[(mode, int(bucket))].append(rate_kw)

    result: dict[str, dict] = {"self_consumption": {}, "autonomous": {}}
    for (mode, bucket), rates in sorted(by_mode_bucket.items()):
        if mode in result:
            n  = len(rates)
            kw = sum(rates) / n
            result[mode][str(bucket)] = {"kw": round(kw, 3), "n": n}

    return result


def _obs_range(conn: sqlite3.Connection) -> tuple[int, str, str]:
    row = conn.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts) FROM observations"
    ).fetchone()
    count, ts_min, ts_max = row or (0, None, None)
    if ts_min and ts_max:
        days = (datetime.fromisoformat(ts_max) - datetime.fromisoformat(ts_min)).days
    else:
        days = 0
    return count, days, ts_min or "", ts_max or ""


def main() -> None:
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found. Run this script on the Pi.")
        return

    conn = sqlite3.connect(DB_PATH)

    count, days, ts_min, ts_max = _obs_range(conn)
    print(f"DB: {count} observations over {days} days  ({ts_min[:10]} → {ts_max[:10]})")

    if days < 7:
        print("WARNING: fewer than 7 days of data — solar corrector may be unreliable.")
    if days < 14:
        print("NOTE: 14+ days recommended for reliable solar correction.")

    # ── Solar correction ──────────────────────────────────────────────
    print("\nSolar correction (actual / Solcast) by hour:")
    solar = build_solar_correction(conn)
    if not solar:
        print("  No solar data found. Check that solar_actual_kw and "
              "solcast_power_now_kw are being logged.")
    for h_str, v in sorted(solar.items(), key=lambda x: int(x[0])):
        flag = ""  if v["n"] >= MIN_SAMPLES else "  ← low-n, will use ratio=1.0 fallback"
        print(f"  {int(h_str):02d}:00  ratio={v['ratio']:.3f}  "
              f"±{v['uncertainty']:.3f}  n={v['n']}{flag}")

    # ── Charge rate model ─────────────────────────────────────────────
    print("\nCharge rate model (kW by SoC bucket × mode):")
    rates = build_charge_rate_model(conn)
    for mode in ("self_consumption", "autonomous"):
        buckets = rates.get(mode, {})
        if not buckets:
            print(f"  {mode}: no data")
            continue
        for bucket, v in sorted(buckets.items(), key=lambda x: int(x[0])):
            flag = ""  if v["n"] >= MIN_SAMPLES else "  ← low-n, will use fallback"
            print(f"  {mode}  SoC={int(bucket):3d}%  {v['kw']:.3f} kW  n={v['n']}{flag}")

    conn.close()

    # ── Write model_params.json ───────────────────────────────────────
    params: dict = {}
    if PARAMS_PATH.exists():
        with PARAMS_PATH.open() as f:
            params = json.load(f)

    params.update({
        "built_at":        datetime.now().strftime("%Y-%m-%d"),
        "obs_days":        days,
        "min_samples":     MIN_SAMPLES,
        "charge_rate_kw":  rates,
        "solar_correction": solar,
    })

    with PARAMS_PATH.open("w") as f:
        json.dump(params, f, indent=2)

    print(f"\nWritten → {PARAMS_PATH}")
    print("Next steps:")
    print("  git add agent/model_params.json")
    print(f"  git commit -m 'model_params: rebuild {datetime.now().strftime(\"%Y-%m-%d\")}'")
    print("  git push")


if __name__ == "__main__":
    main()
