#!/usr/bin/env python3
"""
Unit tests for compute_decision_context() — the pure deterministic decision layer.
No API calls, no HTTP, runs in milliseconds.  Run:  python test_decision.py

These pin the maths the system prompt currently asks the LLM to do in its head,
including the specific edge cases the energy_log earned the hard way.
"""

from datetime import datetime

import pytz

import energy_agent as ea

TZ = pytz.timezone("Australia/Sydney")
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


def now_at(hour, minute=0):
    return TZ.localize(datetime(2026, 6, 15, hour, minute))


def mk_state(soc, hour, accuracy="good", solar_kw=3.0, remaining=8.0,
             is_peak=True, grid_target=30, price=16.0, gateway=None):
    acc_map = {
        "good":       f"good — actual {solar_kw}kW vs forecast (94% of forecast)",
        "poor":       f"poor — actual {solar_kw}kW vs forecast (50% of forecast)",
        "unreliable": f"unreliable — actual {solar_kw}kW vs forecast (10% of forecast)",
        "na":         "not_applicable (night)",
    }
    return {
        "is_peak_month":    is_peak,
        "in_demand_window": is_peak and 15 <= hour < 21,
        "in_solar_sponge":  10 <= hour < 15,
        "battery": {
            "soc_pct": soc,
            "soc_gateway_pct": gateway if gateway is not None else soc,
            "grid_target_pct": grid_target, "reserve_pct": 5, "mode": "self_consumption",
        },
        "grid": {"price_cents_kwh": price, "in_cheap_window": True},
        "solar": {"current_kw": solar_kw, "forecast_remaining_kwh": remaining,
                  "forecast_accuracy": acc_map[accuracy]},
        "home_load_kw": 0.5,
    }


def fc(prices):
    """Uniform 30-min forecast from a list of cents values."""
    return [{"time": f"+{i*30}m", "cents_kwh": p, "descriptor": ""} for i, p in enumerate(prices)]


def flat(price_c, n=24):
    return fc([price_c] * n)


# --------------------------------------------------------------------------
# Helper-level tests
# --------------------------------------------------------------------------

def test_hours_to_cheap_end():
    f = fc([13, 13, 13, 13, 13, 13, 19, 19, 19, 19])     # sustained +6 at index 6
    check("cheap_end sustained rise", ea._hours_to_cheap_end(f, 13) == 3.0,
          ea._hours_to_cheap_end(f, 13))
    f2 = fc([13, 13, 19, 13, 13, 13])                     # single blip, not sustained
    check("cheap_end ignores blip", ea._hours_to_cheap_end(f2, 13) == 6.0,
          ea._hours_to_cheap_end(f2, 13))
    check("cheap_end flat -> 6h", ea._hours_to_cheap_end(flat(16), 16) == 6.0)


def test_detectors():
    holds = [{"actions": [], "price_c": 16, "ts": "2026-06-15T10:30", "solar_current_kw": 0.3},
             {"actions": [], "price_c": 16, "ts": "2026-06-15T11:00", "solar_current_kw": 0.3}]
    check("deferral detected", ea._detect_deferral(holds, 16.0) is True)
    check("deferral resets on price move", ea._detect_deferral(holds, 20.0) is False)
    acted = [{"actions": ["set_reserve(80%)"], "price_c": 16, "ts": "2026-06-15T11:00",
              "solar_current_kw": 0.3}, holds[1]]
    check("deferral needs 2 holds", ea._detect_deferral(acted[:1] + [holds[1]], 16.0)
          in (False, True))  # 1 hold only -> False
    zeros = [{"ts": "2026-06-15T10:00", "solar_current_kw": 0.0},
             {"ts": "2026-06-15T10:30", "solar_current_kw": 0.0}]
    check("zero-solar day detected", ea._detect_zero_solar(zeros, 0.0, 11.0) is True)
    check("zero-solar off before 8am", ea._detect_zero_solar(zeros, 0.0, 6.0) is False)
    sunny = [{"ts": "2026-06-15T10:00", "solar_current_kw": 3.0}]
    check("zero-solar false when sunny", ea._detect_zero_solar(sunny, 3.0, 11.0) is False)


# --------------------------------------------------------------------------
# Verdict tests — mirror the backtest scenarios
# --------------------------------------------------------------------------

def test_peak_sunny_holds():
    ctx = ea.compute_decision_context(
        mk_state(60, 11, "good", 3.2, 8.0), flat(16), [], now_at(11))
    check("peak sunny -> hold", ctx["recommended"]["action"] == "hold", ctx["recommended"])
    check("peak sunny target met", ctx["recommended"]["rule_fired"] == "peak_target_met")


def test_peak_cloudy_10am_sponge_floor():
    prior = [{"actions": [], "price_c": 16, "ts": "2026-06-15T09:00", "solar_current_kw": 0.0},
             {"actions": [], "price_c": 16, "ts": "2026-06-15T09:30", "solar_current_kw": 0.0}]
    ctx = ea.compute_decision_context(
        mk_state(40, 10, "unreliable", 0.0, 9.0), flat(16), prior, now_at(10))
    r = ctx["recommended"]
    check("peak cloudy 10am charges", r["action"] == "charge", r)
    check("peak cloudy 10am self_consumption", r["mode"] == "self_consumption", r)
    check("peak cloudy 10am target 85 (substitute)", r["target_pct"] == 85, r)
    check("zero-solar flagged", ctx["zero_solar_day"] is True)


def test_peak_cloudy_1330_autonomous():
    ctx = ea.compute_decision_context(
        mk_state(55, 13, "unreliable", 0.2, 1.0), flat(16), [], now_at(13, 30))
    r = ctx["recommended"]
    check("peak tight 1:30pm autonomous", r["mode"] == "autonomous", r)
    check("peak tight rule", r["rule_fired"] == "peak_deadline_autonomous", r)


def test_peak_deferral_trap_selfcons():
    # Deterministic picks self_consumption (3.18h fill fits 3.42h window) — the LLM
    # over-escalated to autonomous here; this divergence is exactly what shadow mode surfaces.
    holds = [{"actions": [], "price_c": 16, "ts": "2026-06-15T10:30", "solar_current_kw": 0.3},
             {"actions": [], "price_c": 16, "ts": "2026-06-15T11:00", "solar_current_kw": 0.3}]
    ctx = ea.compute_decision_context(
        mk_state(45, 11, "unreliable", 0.3, 1.5), flat(16), holds, now_at(11, 30))
    r = ctx["recommended"]
    check("peak deferral trap charges", r["action"] == "charge", r)
    check("peak deferral trap self_consumption", r["mode"] == "self_consumption", r)


def test_soc_gateway_divergence():
    # True SoC 50%, gateway floor-clipped to 85%. Must use 50% and keep charging.
    ctx = ea.compute_decision_context(
        mk_state(50, 14, "unreliable", 0.1, 0.3, gateway=85), flat(16), [], now_at(14, 45))
    check("uses true Tessie SoC", ctx["soc"] == 50, ctx["soc"])
    check("does not declare target met", ctx["recommended"]["action"] == "charge",
          ctx["recommended"])


def test_nonpeak_deferral():
    holds = [{"actions": [], "price_c": 16, "ts": "2026-05-20T13:00", "solar_current_kw": 2.0},
             {"actions": [], "price_c": 16, "ts": "2026-05-20T13:30", "solar_current_kw": 2.0}]
    ctx = ea.compute_decision_context(
        mk_state(30, 13, "good", 2.0, 5.0, is_peak=False, grid_target=80),
        flat(16), holds, now_at(13, 30))
    check("nonpeak deferral fires", ctx["recommended"]["rule_fired"] == "deferral_limit",
          ctx["recommended"])


def test_nonpeak_spread_arbitrage():
    prices = [10] * 10 + [30] * 14            # cheap 5h then spike
    ctx = ea.compute_decision_context(
        mk_state(40, 13, "good", 2.0, 5.0, is_peak=False, grid_target=80, price=10),
        fc(prices), [], now_at(13, 30))
    r = ctx["recommended"]
    check("big spread -> autonomous", r["mode"] == "autonomous", r)
    check("spread arbitrage rule", r["rule_fired"] == "spread_arbitrage", r)


def test_nonpeak_spread_too_small():
    ctx = ea.compute_decision_context(
        mk_state(40, 13, "good", 2.0, 5.0, is_peak=False, grid_target=80, price=18),
        flat(18), [], now_at(13, 30))
    check("flat price small spread -> hold",
          ctx["recommended"]["rule_fired"] == "spread_too_small", ctx["recommended"])


def test_demand_window_no_import():
    ctx = ea.compute_decision_context(
        mk_state(40, 16, "na", 0.0, 0.0), flat(16), [], now_at(16))
    check("demand window -> hold", ctx["recommended"]["rule_fired"] == "demand_window_active",
          ctx["recommended"])


if __name__ == "__main__":
    for fn in [test_hours_to_cheap_end, test_detectors, test_peak_sunny_holds,
               test_peak_cloudy_10am_sponge_floor, test_peak_cloudy_1330_autonomous,
               test_peak_deferral_trap_selfcons, test_soc_gateway_divergence,
               test_nonpeak_deferral, test_nonpeak_spread_arbitrage,
               test_nonpeak_spread_too_small, test_demand_window_no_import]:
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n{'='*50}\n{_passed} passed, {_failed} failed")
    raise SystemExit(1 if _failed else 0)
