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
    # NOTE: hour is extracted in Python, NOT via SQLite strftime('%H', ts).
    # `ts` is stored offset-aware ("2026-07-22T09:30:04.865371+10:00") and
    # SQLite's date functions normalise to UTC, so strftime returns 23 for
    # local 09:30 and 0 for local 10:00 — every key shifted 10 hours, putting
    # midday ratios under midnight keys. datetime.fromisoformat() keeps the
    # offset, so .hour is the local wall-clock hour and stays DST-correct.
    # (Bug found 2026-07-22 on this script's first successful run.)
    rows = conn.execute("""
        SELECT
            ts,
            solar_actual_kw,
            solcast_power_now_kw
        FROM observations
        WHERE solcast_power_now_kw > 0.3
          AND solar_actual_kw IS NOT NULL
    """).fetchall()

    by_hour: dict[int, list[float]] = defaultdict(list)
    for ts, actual, forecast in rows:
        try:
            hour = datetime.fromisoformat(ts).hour
        except (TypeError, ValueError):
            continue
        if forecast and forecast > 0:
            ratio = actual / forecast
            if 0.0 <= ratio <= 3.0:
                by_hour[int(hour)].append(ratio)

    result = {}
    for hour, ratios in sorted(by_hour.items()):
        n    = len(ratios)
        mean = sum(ratios) / n
        var  = sum((r - mean) ** 2 for r in ratios) / max(n - 1, 1)
        # Key MUST be zero-padded: optimizer._build_solar_series() looks up
        # `key[11:13]` of a normalised "YYYY-MM-DD HH:MM" timestamp, i.e. "09".
        # Writing str(9) -> "9" makes hours 00-09 silently miss the lookup and
        # fall back to raw Solcast — exactly the winter morning hours where the
        # bias is largest. (Bug found 2026-07-22, before this script ever ran.)
        result[f"{hour:02d}"] = {
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
            (b.battery_soc_pct - a.battery_soc_pct)       AS delta_pct,
            a.ts                                           AS ts_a,
            b.ts                                           AS ts_b
        FROM observations a
        JOIN observations b ON b.id = a.id + 1
        WHERE b.battery_soc_pct > a.battery_soc_pct
          AND a.battery_mode = b.battery_mode
          AND a.battery_mode IN ('self_consumption', 'autonomous')
          AND (b.battery_soc_pct - a.battery_soc_pct) < ?
    """, (MAX_JUMP_PCT,)).fetchall()

    by_mode_bucket: dict[tuple, list[float]] = defaultdict(list)
    skipped_gap = 0
    for bucket, mode, delta_pct, ts_a, ts_b in rows:
        # `b.id = a.id + 1` is adjacency in the table, NOT adjacency in time.
        # Agent restarts, cron misses and the 141 orphaned rows deleted in
        # session 12 all leave id-consecutive rows hours or days apart. Dividing
        # those by a hardcoded 0.5h yields garbage rates, so measure the real
        # elapsed time and keep only genuine ~30-min intervals.
        try:
            dt_h = (datetime.fromisoformat(ts_b)
                    - datetime.fromisoformat(ts_a)).total_seconds() / 3600.0
        except (TypeError, ValueError):
            skipped_gap += 1
            continue
        if not (0.4 < dt_h < 0.6):
            skipped_gap += 1
            continue
        rate_kw = delta_pct / 100.0 * USABLE_KWH / dt_h
        if 0 < rate_kw < MAX_RATE_CAP:
            by_mode_bucket[(mode, int(bucket))].append(rate_kw)

    if skipped_gap:
        print(f"  (skipped {skipped_gap} id-adjacent pairs that were not "
              f"~30 min apart)")

    result: dict[str, dict] = {"self_consumption": {}, "autonomous": {}}
    for (mode, bucket), rates in sorted(by_mode_bucket.items()):
        if mode in result:
            rates.sort()
            n = len(rates)

            def _q(frac: float) -> float:
                return rates[min(int(n * frac), n - 1)]

            # "kw" stays the mean so existing readers (energy_agent's
            # _avg_charge_rate_kw, optimizer's _model_avg_rate_kw) are
            # unaffected. Percentiles are recorded so the spread is visible
            # rather than assumed — cheap to keep, and they make it obvious if
            # a bucket ever does develop a tail. As measured on 2026-07-22 the
            # self_consumption distribution is tight (p25 1.35 / median 1.61 /
            # p90 1.89 kW), so the mean is a fair summary today. Nothing reads
            # these yet.
            result[mode][str(bucket)] = {
                "kw":     round(sum(rates) / n, 3),
                "p25":    round(_q(0.25), 3),
                "median": round(_q(0.50), 3),
                "p90":    round(_q(0.90), 3),
                "n":      n,
            }

    return result


def _obs_range(conn: sqlite3.Connection) -> tuple[int, int, str, str]:
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
            print(f"  {mode}  SoC={int(bucket):3d}%  mean={v['kw']:.2f}  "
                  f"p25={v['p25']:.2f}  median={v['median']:.2f}  p90={v['p90']:.2f} kW"
                  f"  n={v['n']}{flag}")

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

    today = datetime.now().strftime("%Y-%m-%d")
    print(f"\nWritten → {PARAMS_PATH}")
    print("Next steps:")
    print("  git add agent/model_params.json")
    print(f"  git commit -m 'model_params: rebuild {today}'")
    print("  git push")


if __name__ == "__main__":
    main()
