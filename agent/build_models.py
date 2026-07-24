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
from datetime import datetime, timedelta
from pathlib import Path

HERE      = Path(__file__).parent
DB_PATH   = HERE / "energy_log.db"
PARAMS_PATH = HERE / "model_params.json"

MIN_SAMPLES  = 5
USABLE_KWH   = 13.5
MAX_RATE_CAP = 8.0   # kW — filter sensor glitches
MAX_JUMP_PCT = 20.0  # % SoC per 30 min — filter sensor jumps

# Power-based charge rate model (preferred — see build_charge_rate_model_from_power)
POWER_DAYS        = 10    # long window: HA recorder keeps ~10 days of 30 s battery_power
POWER_SHORT_DAYS  = 2     # short window: the "fall fast" reactor (see _aggregate_charge_rates)
POWER_MIN_SAMPLES = 20    # emit a bucket only with real support behind it
MODE_SETTLE_S     = 180   # ignore samples within 3 min of a mode change (poll lag)


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


def build_charge_rate_model_from_power(days: int = POWER_DAYS,
                                       min_samples: int = POWER_MIN_SAMPLES) -> dict:
    """Measure *instantaneous* grid charge rate (kW) by SoC decile × mode.

    Reads `sensor.tesla_powerwall_2_battery_power` from HA's recorder at its
    native ~30 s resolution, rather than differencing SoC across 30-minute agent
    cycles.

    Why this replaces the SoC-delta method (2026-07-22): that method measured
    *SoC gained per cycle*, which conflates the charge rate with how long
    charging actually ran, and it gated on `sensor.powerwall_backup_reserve` —
    a Tessie-polled sensor with ~2 minutes of lag (observed logging 5% while the
    true value was 80%). Instantaneous power has neither problem, and gives
    hundreds of samples per bucket instead of tens.

    Filters:
      - battery_power < -0.3 kW          → charging
      - site_power    >= 0.5 kW          → grid-sourced (not solar self-charge)
      - >= MODE_SETTLE_S since the last mode change → the mode sensor is derived
        from a 2-minute Tessie poll, so samples straight after a transition can
        carry the wrong mode label. This is what made ~5 kW autonomous charging
        show up as "self_consumption" outliers in earlier analyses.

    Stores percentiles as well as the mean. `kw` is the **median**, which is
    robust to the transition contamination above and to one-off regime changes;
    `p10`/`p25` are there so deadline-critical projections can choose a
    conservative quantile (being optimistic near the 2:55pm deadline risks a
    demand charge; being pessimistic costs cents).
    """
    try:
        import energy_agent as ea  # HA creds + URL, already configured on the Pi
    except Exception as exc:
        print(f"  Could not import energy_agent for HA access ({exc}) — "
              f"skipping power-based model.")
        return {}

    import bisect
    import requests
    from datetime import timedelta

    tz  = ea.SYDNEY_TZ
    now = datetime.now(tz)
    ents = {
        "batt": "sensor.tesla_powerwall_2_battery_power",
        "grid": "sensor.tesla_powerwall_2_site_power",
        "soc":  "sensor.tessie_powerwall_charge",
        "mode": "sensor.powerwall_mode",
    }
    series: dict[str, list] = {k: [] for k in ents}
    for d in range(days, 0, -1):
        start = (now - timedelta(days=d)).isoformat()
        end   = (now - timedelta(days=d - 1)).isoformat()
        for key, ent in ents.items():
            try:
                r = requests.get(f"{ea.HA_URL}/api/history/period/{start}",
                                 headers=ea.HA_HEADERS,
                                 params={"filter_entity_id": ent, "end_time": end},
                                 timeout=90).json()
            except Exception:
                continue
            for x in (r[0] if r else []):
                if x["state"] in ("unknown", "unavailable"):
                    continue
                try:
                    ts = datetime.fromisoformat(x["last_changed"]).astimezone(tz)
                except (TypeError, ValueError):
                    continue
                series[key].append((ts, x["state"]))
    for key in series:
        series[key].sort(key=lambda p: p[0])

    if not series["batt"]:
        print("  No battery_power history returned — is the recorder keeping it?")
        return {}

    index = {k: [p[0] for p in v] for k, v in series.items()}

    def at(key, t):
        i = bisect.bisect_right(index[key], t) - 1
        return series[key][i][1] if i >= 0 else None

    def mode_settled(t) -> bool:
        i = bisect.bisect_right(index["mode"], t) - 1
        if i < 0:
            return False
        return (t - series["mode"][i][0]).total_seconds() >= MODE_SETTLE_S

    # Keep the timestamp with each sample so _aggregate_charge_rates() can split
    # the long window into a short "fall fast" sub-window (see that function).
    buckets: dict[tuple, list[tuple]] = defaultdict(list)
    for ts, raw in series["batt"]:
        try:
            batt = float(raw)
            grid = float(at("grid", ts) or 0.0)
            soc  = float(at("soc", ts) or -1.0)
        except (TypeError, ValueError):
            continue
        mode = at("mode", ts)
        if batt > -0.3 or grid < 0.5 or soc < 0 or mode is None:
            continue
        if not mode_settled(ts):
            continue
        buckets[(mode, int(soc // 10) * 10)].append((ts, -batt))

    return _aggregate_charge_rates(buckets, now, days=days, min_samples=min_samples)


def _aggregate_charge_rates(buckets: dict, now: datetime,
                            days: int = POWER_DAYS,
                            short_days: int = POWER_SHORT_DAYS,
                            min_samples: int = POWER_MIN_SAMPLES) -> dict:
    """Per-(mode, SoC-bucket) charge-rate summary with an *asymmetric* headline.

    `buckets` maps (mode, soc_bucket) -> list of (timestamp, rate_kw) samples over
    the full `days` window. Pure — no HA/DB access — so it is unit-tested directly
    against synthetic regime-change data (test_build_models.py).

    The headline `kw` (what energy_agent._avg_charge_rate_kw and optimizer read)
    is the **minimum of the long-window median and the short-window median**:

        kw = min( median(all samples), median(last short_days) )

    Why min, and why asymmetric (2026-07-24): the charge rate stepped from
    ~1.67 kW to ~5.0 kW on 2026-07-22 (a firmware regime change). A symmetric
    10-day median is slow in *both* directions, but the risk is not symmetric —
    believing 5 kW when it is really 1.67 means budgeting too little charge time
    and risking a ~$30 demand charge, while believing 1.67 when it is really 5
    only costs a few cents of charging earlier/dearer than needed. So the model
    should fall to a slower rate within ~a day (safe) but rise to a faster rate
    only on sustained evidence:

      - When the rate *rises*, the short median jumps first while the long median
        lags (old days still outvote), so min() = the lagging long median →
        inertia. It rises only once the long window itself is majority new-regime
        (~same timing as a plain 10-day median — no upside is delayed).
      - When the rate *falls*, the short median drops first, so min() = the short
        median → the model follows down within ~short_days.

    A low quantile does NOT substitute for this: within the new regime p25≈p50, so
    quantiles hedge within-regime spread, not a regime change. Percentiles below
    are still emitted (over the full window) for deadline-conservative projections.
    """
    def _median(sorted_vals):
        n = len(sorted_vals)
        return sorted_vals[min(int(n * 0.5), n - 1)]

    short_cutoff = now - timedelta(days=short_days)
    result: dict[str, dict] = {}
    for (mode, bucket), samples in sorted(buckets.items()):
        if len(samples) < min_samples:
            continue
        rates = sorted(v for _ts, v in samples)
        n = len(rates)
        q = lambda f: round(rates[min(int(n * f), n - 1)], 3)
        long_median = _median(rates)

        short_rates = sorted(v for ts, v in samples if ts >= short_cutoff)
        # Only let the short window pull the headline down if it has real support;
        # otherwise a quiet couple of days can't spuriously move the model.
        if len(short_rates) >= min_samples:
            short_median = _median(short_rates)
            kw = round(min(long_median, short_median), 3)
        else:
            short_median = None
            kw = round(long_median, 3)

        result.setdefault(mode, {})[str(bucket)] = {
            "kw":           kw,                      # asymmetric — what readers use
            "kw_long":      round(long_median, 3),   # plain full-window median
            "kw_short":     round(short_median, 3) if short_median is not None else None,
            "n_short":      len(short_rates),
            "p10":          q(0.10),
            "p25":          q(0.25),
            "median":       q(0.50),
            "mean":         round(sum(rates) / n, 3),
            "max":          round(rates[-1], 3),
            "n":            n,
            "source":       f"power_{days}d_asym{short_days}d",
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
    # Legacy SoC-delta model first, so it can back-fill any bucket the
    # power-based model has too little data for.
    legacy = build_charge_rate_model(conn)

    print(f"\nCharge rate — instantaneous power, last {POWER_DAYS} days:")
    power = build_charge_rate_model_from_power()

    rates: dict[str, dict] = {}
    for mode in ("self_consumption", "autonomous"):
        merged = dict(legacy.get(mode, {}))
        for bucket, v in (power.get(mode) or {}).items():
            merged[bucket] = v                      # power data wins where it exists
        if merged:
            rates[mode] = merged

    if not power:
        print("  (no power data — falling back to the legacy SoC-delta model)")
    for mode in ("self_consumption", "autonomous"):
        buckets = rates.get(mode, {})
        if not buckets:
            print(f"  {mode}: no data")
            continue
        for bucket, v in sorted(buckets.items(), key=lambda x: int(x[0])):
            src = v.get("source", "soc_delta_legacy")
            if src.startswith("power"):
                asym = ""
                if v.get("kw_short") is not None and v.get("kw_long") is not None:
                    held = " (held-down)" if v["kw"] < v["kw_long"] - 1e-9 else ""
                    asym = (f"  long={v['kw_long']:.2f} short={v['kw_short']:.2f}"
                            f"(n={v.get('n_short', 0)}){held}")
                print(f"  {mode}  SoC={int(bucket):3d}%  kw={v['kw']:.2f}"
                      f"  p10={v['p10']:.2f}  p25={v['p25']:.2f}  max={v['max']:.2f} kW"
                      f"  n={v['n']:<5} [{src}]{asym}")
            else:
                flag = "" if v["n"] >= MIN_SAMPLES else "  ← low-n, agent will use fallback"
                print(f"  {mode}  SoC={int(bucket):3d}%  kw={v['kw']:.2f} kW"
                      f"  n={v['n']:<5} [{src}]{flag}")

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
