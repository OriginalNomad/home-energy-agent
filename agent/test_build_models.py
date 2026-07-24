#!/usr/bin/env python3
"""
Unit tests for _aggregate_charge_rates() — the pure asymmetric charge-rate
window. No HA/DB access, runs in milliseconds.  Run:  python test_build_models.py

These pin the asymmetry decided on 2026-07-24: the headline charge rate must
FALL within ~a day (safe: budget more charge time) but RISE only on sustained
evidence (a spurious fast day must not make the deadline maths optimistic).
"""

from datetime import datetime, timedelta

import build_models as bm

_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


NOW = datetime(2026, 7, 24, 9, 0, 0)


def samples(mode, bucket, per_day, min_samples=None):
    """Build a (mode, bucket) -> [(ts, rate)] dict.

    per_day maps `days-ago` (0 = today, 1 = yesterday, ...) to the constant rate
    on that day. Emits enough samples/day to clear POWER_MIN_SAMPLES.
    """
    n_each = (min_samples or bm.POWER_MIN_SAMPLES)
    out = []
    for days_ago, rate in per_day.items():
        base = NOW - timedelta(days=days_ago, hours=1)
        for i in range(n_each):
            out.append((base + timedelta(seconds=30 * i), rate))
    return {(mode, bucket): out}


def test_rise_is_slow_new_regime_minority():
    # 8 old days at 1.67, 2 recent days at 5.0 (the 2026-07-24 state). The long
    # median is still 1.67 (old days dominate) so the headline must stay 1.67 —
    # today's data must NOT flip self_consumption to 5.0.
    per_day = {d: (5.0 if d <= 1 else 1.67) for d in range(10)}
    buckets = samples("self_consumption", 40, per_day)
    res = bm._aggregate_charge_rates(buckets, NOW)
    v = res["self_consumption"]["40"]
    check("rise slow: headline stays pessimistic while new regime is minority",
          abs(v["kw"] - 1.67) < 1e-6, v)
    check("rise slow: long median is the old regime", abs(v["kw_long"] - 1.67) < 1e-6, v)
    check("rise slow: short median already sees the new regime",
          abs(v["kw_short"] - 5.0) < 1e-6, v)


def test_rise_completes_once_long_window_is_majority():
    # 6 of 10 days new-regime: the long median itself is now 5.0, so the model
    # rises. min(5.0, 5.0) = 5.0 — no upside is delayed vs a plain median.
    per_day = {d: (5.0 if d <= 5 else 1.67) for d in range(10)}
    buckets = samples("self_consumption", 40, per_day)
    v = bm._aggregate_charge_rates(buckets, NOW)["self_consumption"]["40"]
    check("rise completes when long window is majority new-regime",
          abs(v["kw"] - 5.0) < 1e-6, v)


def test_fall_is_fast_within_short_window():
    # Was 5.0 for 8 old days, dropped to 1.67 for the last 2. The long median is
    # still 5.0, but the model must already follow the drop down (safe direction).
    per_day = {d: (1.67 if d <= 1 else 5.0) for d in range(10)}
    buckets = samples("self_consumption", 40, per_day)
    v = bm._aggregate_charge_rates(buckets, NOW)["self_consumption"]["40"]
    check("fall fast: headline follows the drop within the short window",
          abs(v["kw"] - 1.67) < 1e-6, v)
    check("fall fast: long median still lags at the old fast rate",
          abs(v["kw_long"] - 5.0) < 1e-6, v)


def test_stable_regime_headline_equals_median():
    per_day = {d: 4.98 for d in range(10)}
    buckets = samples("autonomous", 30, per_day)
    v = bm._aggregate_charge_rates(buckets, NOW)["autonomous"]["30"]
    check("stable regime: kw == long median", abs(v["kw"] - v["kw_long"]) < 1e-6, v)
    check("stable regime: kw ~= 4.98", abs(v["kw"] - 4.98) < 1e-6, v)


def test_short_window_needs_support_to_pull_down():
    # A genuine fast history, but only a trickle of (low) samples in the last 2
    # days — below min_samples. The short window must NOT move the headline; a
    # quiet couple of days can't spuriously drop the model.
    per_day = {d: 5.0 for d in range(2, 10)}          # 8 old days, fast
    buckets = samples("self_consumption", 40, per_day)
    # add just 3 slow samples inside the short window (< POWER_MIN_SAMPLES)
    key = ("self_consumption", 40)
    base = NOW - timedelta(hours=2)
    buckets[key] += [(base + timedelta(seconds=30 * i), 1.0) for i in range(3)]
    v = bm._aggregate_charge_rates(buckets, NOW)["self_consumption"]["40"]
    check("short window ignored when it lacks support (kw_short None)",
          v["kw_short"] is None, v)
    check("headline stays at long median without short support",
          abs(v["kw"] - v["kw_long"]) < 1e-6, v)


def test_bucket_below_min_samples_is_dropped():
    per_day = {0: 5.0}
    buckets = samples("self_consumption", 90, per_day, min_samples=3)  # only 3 total
    res = bm._aggregate_charge_rates(buckets, NOW)
    check("under-supported bucket is omitted entirely",
          "90" not in res.get("self_consumption", {}), res)


if __name__ == "__main__":
    for fn in [test_rise_is_slow_new_regime_minority,
               test_rise_completes_once_long_window_is_majority,
               test_fall_is_fast_within_short_window,
               test_stable_regime_headline_equals_median,
               test_short_window_needs_support_to_pull_down,
               test_bucket_below_min_samples_is_dropped]:
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n{'='*50}\n{_passed} passed, {_failed} failed")
    raise SystemExit(1 if _failed else 0)
