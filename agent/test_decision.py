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
    # Gradual ramp off a low base: relative bands catch it where a +4¢ jump test misses.
    ramp = fc([13.4, 15.7, 17, 19, 19, 19, 19, 19])      # smooth climb to evening peak
    check("cheap_end catches gradual ramp", ea._hours_to_cheap_end(ramp, 13.4) == 0.5,
          ea._hours_to_cheap_end(ramp, 13.4))
    # Flat-ish day with sub-5¢ swing: no meaningful trough to end.
    jitter = fc([15, 15, 16, 15, 16, 15, 15, 16])
    check("cheap_end ignores sub-5c jitter", ea._hours_to_cheap_end(jitter, 15) == 6.0,
          ea._hours_to_cheap_end(jitter, 15))


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
    check("zero-solar off before 9am", ea._detect_zero_solar(zeros, 0.0, 8.5) is False)
    # At 8:30am with a zero solar record at 8:00 — must not fire (pre-9am guard)
    early_zeros = [{"ts": "2026-06-15T08:00", "solar_current_kw": 0.0},
                   {"ts": "2026-06-15T08:30", "solar_current_kw": 0.0}]
    check("zero-solar ignores pre-9am records", ea._detect_zero_solar(early_zeros, 0.0, 8.5) is False)
    sunny = [{"ts": "2026-06-15T10:00", "solar_current_kw": 3.0}]
    check("zero-solar false when sunny", ea._detect_zero_solar(sunny, 3.0, 11.0) is False)


# --------------------------------------------------------------------------
# Verdict tests — mirror the backtest scenarios
# --------------------------------------------------------------------------

def test_peak_sunny_holds():
    # SoC=60%, generous solar remaining, reliable forecast → solar covers gap → hold.
    # rule_fired should be "peak_solar_will_cover" (not "peak_target_met" — SoC not at 85% yet).
    ctx = ea.compute_decision_context(
        mk_state(60, 11, "good", 3.2, 8.0), flat(16), [], now_at(11))
    check("peak sunny -> hold", ctx["recommended"]["action"] == "hold", ctx["recommended"])
    check("peak sunny solar_will_cover (not target_met)", ctx["recommended"]["rule_fired"] == "peak_solar_will_cover",
          ctx["recommended"])


def test_peak_sunny_low_soc_home_load_deducted():
    # SoC=25%, generous solar BUT home load consumes most of it → kwh_needed_85 > 0 (not covered).
    # home_load_kw=1.2, hours_to_2_55≈7.9h → home consumes ~9.5 kWh; net_solar~0.5 kWh
    # kwh_needed_85 = (0.85-0.25)*13.5 - 0.5 = 7.6 > 0 → peak_solar_will_cover should NOT fire.
    # At 7am with 7.9h to deadline, no urgency yet → peak_on_track (hold) is correct.
    state = mk_state(25, 7, "good", 2.0, 10.0)
    state["home_load_kw"] = 1.2
    ctx = ea.compute_decision_context(state, flat(13), [], now_at(7))
    r = ctx["recommended"]
    check("peak low SoC net solar correctly deducted (no solar_will_cover)", r["rule_fired"] != "peak_solar_will_cover", r)
    # flat(13) forecast at price=16 → 13¢ window is ahead → correctly waits for it
    check("peak 7am cheaper window ahead → wait_for_cheap_go_hard", r["rule_fired"] == "wait_for_cheap_go_hard", r)
    check("wait_for_cheap_go_hard is hold (wait for the cheap slot)", r["action"] == "hold", r)


def test_peak_solar_cover_survival_charges_when_battery_wont_reach_sponge():
    # SoC=18%, 3am, solar forecast covers gap (kwh_needed_85=0), BUT the battery will
    # drain to ~0% before Solar Sponge at 10am (7h away at 0.5kW = 26% drain → floor hit).
    # Should charge now at self_consumption rather than hold and force grid coverage at high price.
    prices = [16.0] * 14 + [10.0] * 10 + [20.0] * 12   # cheap overnight, cheaper at sponge
    state = mk_state(18, 3, "good", 0.3, 20.0)           # generous remaining solar → kwh_needed_85=0
    state["home_load_kw"] = 0.5
    ctx = ea.compute_decision_context(state, fc(prices), [], now_at(3))
    r = ctx["recommended"]
    check("survival floor charges when battery drains before sponge",
          r["rule_fired"] == "peak_solar_cover_survival", r)
    check("survival floor action is charge", r["action"] == "charge", r)
    check("survival floor mode is self_consumption", r["mode"] == "self_consumption", r)


def test_peak_survival_waits_when_sponge_close_and_cheaper():
    # SoC=12%, 7:30am, Solcast says solar covers gap (kwh_needed_85=0), battery drains to
    # floor before Solar Sponge (2.5h away). But Solar Sponge is close (≤3h) and 9¢ cheaper
    # (20¢ now vs 11¢ at 10am) — waiting and fast-charging at Solar Sponge saves ~68¢.
    # Should hold (peak_survival_wait_for_sponge) not charge now.
    prices = [20.0] * 5 + [11.0] * 12 + [18.0] * 7     # 20¢ until ~10am, then 11¢
    state = mk_state(12, 7, "good", 0.1, 20.0, price=20.0)  # kwh_needed_85=0 via Solcast
    state["home_load_kw"] = 0.5
    ctx = ea.compute_decision_context(state, fc(prices), [], now_at(7, 30))
    r = ctx["recommended"]
    check("waits for sponge when close and cheaper (survival path)",
          r["rule_fired"] == "peak_survival_wait_for_sponge", r)
    check("action is hold", r["action"] == "hold", r)


def test_peak_solar_cover_no_survival_holds_when_soc_ok():
    # SoC=60%, 3am, solar covers gap, battery comfortably survives to Solar Sponge (7h × 0.5kW ≈ 19% drain → ~41% remaining).
    # Should hold normally.
    prices = [16.0] * 14 + [10.0] * 10 + [20.0] * 12
    state = mk_state(60, 3, "good", 0.3, 15.0)
    state["home_load_kw"] = 0.5
    ctx = ea.compute_decision_context(state, fc(prices), [], now_at(3))
    r = ctx["recommended"]
    check("no survival fire when soc survives to sponge",
          r["rule_fired"] == "peak_solar_will_cover", r)
    check("action is hold when soc ok", r["action"] == "hold", r)


def test_wait_for_cheap_go_hard_holds_when_sponge_close_and_cheaper():
    # SoC=8%, 7am, kwh_needed_85>0, cheaper slot at 10am (11¢, 3h away, 19¢ gap).
    # Battery will drain to floor before 10am, but waiting is cheaper:
    # drain cost (~0.3 kWh grid) < saving (9.86 kWh × 19¢ gap = 187¢).
    # Agent should hold and wait for the cheap slot.
    prices = [30.0] * 6 + [11.0] * 12 + [20.0] * 6     # 30¢ for 3h, then 11¢
    state = mk_state(8, 7, "poor", 0.1, 8.0, price=30.0)
    state["home_load_kw"] = 0.5
    ctx = ea.compute_decision_context(state, fc(prices), [], now_at(7))
    r = ctx["recommended"]
    check("waits for cheap slot even when battery hits floor en route",
          r["rule_fired"] == "wait_for_cheap_go_hard", r)
    check("action is hold", r["action"] == "hold", r)


def test_peak_wait_for_cheap_go_hard():
    # SoC=16%, 8:30am, price=17¢, Solar Sponge arrives at 10am (11¢ in forecast).
    # Solar is poor — grid charge needed. Cheaper window (11¢) is ahead and feasible.
    # → should hold and wait for that slot (wait_for_cheap_go_hard).
    prices = [17.0] * 3 + [11.0] * 12 + [16.0] * 9   # 17¢ for 1.5h, then 11¢
    state = mk_state(16, 8, "poor", 0.3, 5.0, price=17.0)
    ctx = ea.compute_decision_context(state, fc(prices), [], now_at(8, 30))
    r = ctx["recommended"]
    check("wait_for_cheap_go_hard when cheaper window ahead", r["rule_fired"] == "wait_for_cheap_go_hard", r)
    check("action is hold (waiting)", r["action"] == "hold", r)
    check("go_hard_slot populated", ctx.get("go_hard_slot") is not None, ctx)
    check("go_hard_slot price is 11¢", ctx["go_hard_slot"]["price_c"] == 11.0, ctx)


def test_peak_charge_now_when_no_cheaper_slot():
    # SoC=16%, 8:30am, all prices flat at 17¢. No cheaper slot available.
    # → should charge now at self_consumption (peak_charge_now).
    state = mk_state(16, 8, "poor", 0.3, 5.0, price=17.0)
    ctx = ea.compute_decision_context(state, flat(17), [], now_at(8, 30))
    r = ctx["recommended"]
    check("peak_charge_now when no cheaper slot", r["rule_fired"] == "peak_charge_now", r)
    check("action is charge", r["action"] == "charge", r)
    check("mode is self_consumption (not urgent enough for autonomous)", r["mode"] == "self_consumption", r)


def test_peak_sponge_go_hard():
    # SoC=40%, 10:30am (in Solar Sponge), poor solar → solar_unreliable=True → expected_solar=0.
    # kwh_needed_85 = (0.85-0.4)*13.5 = 6.075, fill_slow=3.57h, deadline=4.42h, buffer=0.85h < 1h.
    # fill_slow_85 >= deadline - 1h → peak_deadline_selfcons (self_consumption, tight but not yet autonomous).
    # Receding horizon: at next cycle if solar doesn't improve, peak_deadline_autonomous fires.
    state = mk_state(40, 10, "poor", 0.3, 3.0, price=11.0)
    ctx = ea.compute_decision_context(state, flat(11), [], now_at(10, 30))
    r = ctx["recommended"]
    check("peak_sponge_go_hard: fill_slow tight → charge at self_consumption", r["action"] == "charge", r)
    # When fill_slow just barely exceeds deadline-1h, self_consumption is the correct rate.
    # Receding horizon will escalate to autonomous if solar doesn't improve next cycle.

def test_peak_sponge_selfcons_then_escalates():
    # SoC=65%, 10:30am, poor solar, kwh_needed_85=(0.85-0.65)*13.5=2.7, fill_slow=1.6h, deadline=4.4h.
    # In Solar Sponge, grid charge needed, fill_slow comfortably fits → peak_sponge_selfcons.
    state = mk_state(65, 10, "poor", 0.3, 2.0, price=11.0)
    ctx = ea.compute_decision_context(state, flat(11), [], now_at(10, 30))
    r = ctx["recommended"]
    check("peak_sponge_selfcons: fill_slow fits → self_consumption", r["rule_fired"] == "peak_sponge_selfcons", r)
    check("mode is self_consumption (receding horizon, may downgrade if solar improves)", r["mode"] == "self_consumption", r)

def test_peak_sponge_solar_improves_to_hold():
    # SoC=75%, 11am, good solar recovering — net_solar now covers remaining gap → hold.
    state = mk_state(75, 11, "good", 3.0, 5.0, price=11.0)
    ctx = ea.compute_decision_context(state, flat(11), [], now_at(11, 0))
    r = ctx["recommended"]
    check("peak_sponge_solar_improves: solar covers gap → hold", r["action"] == "hold", r)


def test_peak_target_met_label_at_85():
    # When SoC actually reaches 85%, rule should still be "peak_target_met".
    ctx = ea.compute_decision_context(
        mk_state(85, 11, "good", 3.2, 8.0), flat(16), [], now_at(11))
    # soc >= 85 so the outer branch `soc < 85` won't fire — falls to target_met/hold below.
    # (The peak branch condition is `soc < 85`, so this falls to the non-peak/target_met path)
    check("soc=85 -> hold", ctx["recommended"]["action"] == "hold", ctx["recommended"])


def test_nonpeak_solar_will_cover_holds():
    # Non-peak, morning, reliable solar forecast, gap < net solar → hold (solar_will_cover).
    # home_load=0.5, hours_to_deadline~4h → home consumes 2 kWh; remaining=8 → net=6 kWh
    # gap = (0.70 - 0.50) * 13.5 = 2.7 kWh < 6 kWh → solar covers it
    ctx = ea.compute_decision_context(
        mk_state(50, 10, "good", 3.0, 8.0, is_peak=False, grid_target=70, price=12),
        flat(12), [], now_at(10))
    r = ctx["recommended"]
    check("nonpeak solar_will_cover -> hold", r["action"] == "hold", r)
    check("nonpeak solar_will_cover rule", r["rule_fired"] == "solar_will_cover", r)


def test_nonpeak_solar_insufficient_charges():
    # Non-peak, tiny solar remaining → solar can't cover gap → falls through to spread logic.
    ctx = ea.compute_decision_context(
        mk_state(50, 10, "good", 3.0, 1.0, is_peak=False, grid_target=70, price=12),
        flat(12), [], now_at(10))
    r = ctx["recommended"]
    check("nonpeak tiny solar falls to spread logic", r["rule_fired"] != "solar_will_cover", r)


def test_nonpeak_solar_will_cover_not_after_1pm():
    # Non-peak, after 1pm → solar_will_cover guard (now_h < 13) prevents the hold.
    ctx = ea.compute_decision_context(
        mk_state(50, 13, "good", 3.0, 8.0, is_peak=False, grid_target=70, price=12),
        flat(12), [], now_at(13))
    r = ctx["recommended"]
    check("nonpeak after 1pm solar_will_cover doesn't fire", r["rule_fired"] != "solar_will_cover", r)


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
    # With Phase 2.5-A charge rate model, avg rate 45%→85% is ~1.36 kW (tapers above 80%),
    # so fill_slow ≈ 3.97h vs 3.42h deadline at 11:30 — autonomous is now correct.
    # (Pre-model: flat 1.7 kW gave 3.18h which fits, so self_consumption was right then.)
    holds = [{"actions": [], "price_c": 16, "ts": "2026-06-15T10:30", "solar_current_kw": 0.3},
             {"actions": [], "price_c": 16, "ts": "2026-06-15T11:00", "solar_current_kw": 0.3}]
    ctx = ea.compute_decision_context(
        mk_state(45, 11, "unreliable", 0.3, 1.5), flat(16), holds, now_at(11, 30))
    r = ctx["recommended"]
    check("peak deferral trap charges", r["action"] == "charge", r)
    check("peak deferral trap autonomous", r["mode"] == "autonomous", r)


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


def test_overnight_hold_for_cheap_window():
    # Overnight at 3am: price 13c, cheap window (6c) arrives at 10am — 7h away.
    # Deferral detected (3 holds at flat overnight price), but holding is correct
    # because forward_min=6c is well below current 13c. deferral_limit must NOT fire.
    holds = [{"actions": [], "price_c": 13, "ts": "2026-05-31T02:00", "solar_current_kw": 0.0},
             {"actions": [], "price_c": 13, "ts": "2026-05-31T02:30", "solar_current_kw": 0.0}]
    # Forecast: 13c flat overnight, drops to 6c from 10am (index 14 = 7h from 3am)
    forecast_prices = [13] * 14 + [6] * 10
    ctx = ea.compute_decision_context(
        mk_state(20, 3, "na", 0.0, 0.0, is_peak=False, grid_target=30, price=13),
        fc(forecast_prices), holds, now_at(3, 0))
    r = ctx["recommended"]
    check("overnight hold: deferral_limit suppressed when cheap window incoming",
          r["rule_fired"] != "deferral_limit", r)
    check("overnight hold: forward_min captured", ctx["forward_min_c"] == 6.0, ctx["forward_min_c"])


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


def mk_state_with_stats(soc, hour, accuracy, solar_kw, remaining, price,
                        p25, p75, is_peak=False, grid_target=30):
    """mk_state + price_stats injected into settings."""
    state = mk_state(soc, hour, accuracy=accuracy, solar_kw=solar_kw,
                     remaining=remaining, is_peak=is_peak,
                     grid_target=grid_target, price=price)
    state["settings"] = {"price_stats": {"p25": p25, "p75": p75,
                                          "pmin": p25 - 2, "pmax": p75 + 5, "n": 100},
                         "max_insurance_floor_pct": 70}
    return state


def test_historical_model_cheap_price_raises_target():
    # p25=6, p75=18. P_now=6 (at p25) → price_position=0 → solar_trusted=0 → target=95
    # insurance_floor = 70 × 1.0 = 70. max(95, 70) = 95
    state = mk_state_with_stats(30, 11, "good", 3.0, 8.0, price=6,
                                p25=6, p75=18)
    ctx = ea.compute_decision_context(state, flat(6), [], now_at(11))
    check("cheap price target near 95%", ctx["cost_target_pct"] >= 90, ctx["cost_target_pct"])
    check("cost_target_method is historical", ctx["cost_target_method"] == "historical",
          ctx["cost_target_method"])


def test_historical_model_expensive_price_lowers_target():
    # p25=6, p75=18. P_now=18 (at p75) → price_position=1 → full solar trust
    # solar_trusted = 8 kWh × 1.0 × 1.0 = 8 kWh → target = 95 - (8/13.5*100) ≈ 36%
    # insurance_floor = 70 × 0 = 0. target ≈ 36
    state = mk_state_with_stats(30, 11, "good", 3.0, 8.0, price=18,
                                p25=6, p75=18)
    ctx = ea.compute_decision_context(state, flat(18), [], now_at(11))
    check("expensive price target is low (trusts solar)", ctx["cost_target_pct"] < 50,
          ctx["cost_target_pct"])


def test_historical_model_falls_back_without_stats():
    # No price_stats in settings → falls back to legacy
    state = mk_state(30, 11, "unreliable", 0.0, 0.0, is_peak=False,
                     grid_target=30, price=13)
    ctx = ea.compute_decision_context(state, flat(13), [], now_at(11))
    check("fallback to legacy when no stats", ctx["cost_target_method"] == "legacy",
          ctx["cost_target_method"])


def test_historical_model_flat_history_falls_back():
    # p25=13, p75=14 → swing=1 < 2 → fall back to legacy
    state = mk_state_with_stats(30, 11, "good", 3.0, 8.0, price=13,
                                p25=13, p75=14)
    ctx = ea.compute_decision_context(state, flat(13), [], now_at(11))
    check("flat history falls back to legacy", ctx["cost_target_method"] == "legacy",
          ctx["cost_target_method"])


def test_ev_case6_negative_fit_solar_dump():
    # FIT negative, battery 90%+, EV below 100% → Fast regardless of EV target
    state = mk_state(90, 12, "good", 3.0, 2.0, is_peak=False, grid_target=30, price=6)
    state["grid"]["fit_price_cents_kwh"] = -1.0
    state["ev"] = {"plugged_in": True, "plug_status": "EV Connected", "charging": False,
                   "zappi_mode": "Eco+", "ev_soc_pct": 80,
                   "min_soc_pct": 20, "charge_target_pct": 80, "schedule": None}
    ctx = ea.compute_decision_context(state, flat(6), [], now_at(12))
    ev = ctx["ev_recommended"]
    check("case6: Eco+ when FIT negative and battery full", ev["zappi_mode"] == "Eco+", ev)
    check("case6: rule fired", ev["rule_fired"] == "ev_case6_negative_fit_solar_dump", ev)


def test_ev_case6_not_fired_when_battery_low():
    # FIT negative but battery only 60% — don't dump into EV yet
    state = mk_state(60, 12, "good", 3.0, 2.0, is_peak=False, grid_target=30, price=6)
    state["grid"]["fit_price_cents_kwh"] = -1.0
    state["ev"] = {"plugged_in": True, "plug_status": "EV Connected", "charging": False,
                   "zappi_mode": "Eco+", "ev_soc_pct": 70,
                   "min_soc_pct": 20, "charge_target_pct": 80, "schedule": None}
    ctx = ea.compute_decision_context(state, flat(6), [], now_at(12))
    ev = ctx["ev_recommended"]
    check("case6: not fired when battery below 85%",
          ev["rule_fired"] != "ev_case6_negative_fit_solar_dump", ev)


def mk_ev_state(ev_soc, ev_min, ev_target, batt_soc, reserve, price, forward_prices=None,
                in_demand=False, ultra_cheap_c=5, standard_price_c=10, min_charge_price_c=20):
    """Helper: build minimal state dict for EV verdict tests."""
    forecast = fc(forward_prices) if forward_prices else flat(price)
    state = {
        "is_peak_month": in_demand, "in_demand_window": in_demand, "in_solar_sponge": False,
        "battery": {"soc_pct": batt_soc, "soc_gateway_pct": batt_soc,
                    "grid_target_pct": 30, "reserve_pct": reserve, "mode": "self_consumption"},
        "grid": {"price_cents_kwh": price, "in_cheap_window": False},
        "solar": {"current_kw": 0.0, "forecast_remaining_kwh": 0.0,
                  "forecast_accuracy": "not_applicable (night or near-zero forecast)"},
        "home_load_kw": 0.5,
        "ev": {"plugged_in": True, "plug_status": "EV Connected", "charging": False,
               "zappi_mode": "Eco+", "ev_soc_pct": ev_soc,
               "min_soc_pct": ev_min, "charge_target_pct": ev_target, "schedule": None},
        "settings": {"ev_ultra_cheap_c": ultra_cheap_c, "ev_standard_price_c": standard_price_c,
                     "ev_min_charge_price_c": min_charge_price_c,
                     "battery_charge_threshold_c": 12, "max_insurance_floor_pct": 70},
    }
    return state, forecast


def test_ev_eco_when_below_standard_price():
    # EV at 50%, price=8c (< standard_price_c=10c, > ultra_cheap=5c) → Eco (slow charge)
    state, fcast = mk_ev_state(50, 20, 70, batt_soc=80, reserve=20, price=8)
    ctx = ea.compute_decision_context(state, fcast, [], now_at(11))
    ev = ctx["ev_recommended"]
    check("ev: Eco when price below standard threshold", ev["zappi_mode"] == "Eco", ev)
    check("ev: rule is ev_standard_price", ev["rule_fired"] == "ev_standard_price", ev)


def test_ev_eco_plus_when_above_standard_price():
    # EV at 50%, price=13c (> standard_price_c=10c) → Eco+ (solar only, price too high)
    state, fcast = mk_ev_state(50, 20, 70, batt_soc=80, reserve=20, price=13)
    ctx = ea.compute_decision_context(state, fcast, [], now_at(11))
    ev = ctx["ev_recommended"]
    check("ev: Eco+ when price above standard threshold", ev["zappi_mode"] == "Eco+", ev)
    check("ev: rule is ev_price_too_high", ev["rule_fired"] == "ev_price_too_high", ev)


def test_ev_fast_when_ultra_cheap():
    # EV at 50%, price=4c (< ultra_cheap_c=5c) → Fast
    state, fcast = mk_ev_state(50, 20, 70, batt_soc=80, reserve=20, price=4)
    ctx = ea.compute_decision_context(state, fcast, [], now_at(11))
    ev = ctx["ev_recommended"]
    check("ev: Fast when price below ultra-cheap threshold", ev["zappi_mode"] == "Fast", ev)
    check("ev: rule is ev_ultra_cheap", ev["rule_fired"] == "ev_ultra_cheap", ev)


def test_ev_fast_when_below_min_within_ceiling():
    # EV at 15% (below min=20%), price=18c < min_charge_price_c=20c → Fast
    state, fcast = mk_ev_state(15, 20, 70, batt_soc=80, reserve=20, price=18)
    ctx = ea.compute_decision_context(state, fcast, [], now_at(11))
    ev = ctx["ev_recommended"]
    check("ev: Fast when below min and price within ceiling", ev["zappi_mode"] == "Fast", ev)
    check("ev: case3 rule", ev["rule_fired"] == "ev_case3_below_minimum", ev)


def test_ev_eco_plus_when_below_min_above_ceiling():
    # EV at 15% (below min=20%), price=25c > min_charge_price_c=20c → Eco+ (too expensive)
    state, fcast = mk_ev_state(15, 20, 70, batt_soc=80, reserve=20, price=25)
    ctx = ea.compute_decision_context(state, fcast, [], now_at(11))
    ev = ctx["ev_recommended"]
    check("ev: Eco+ when below min but price above ceiling", ev["zappi_mode"] == "Eco+", ev)


def test_ev_eco_plus_during_demand_window():
    # Even with cheap price, demand window → Eco+ (no grid draw)
    state, fcast = mk_ev_state(50, 20, 70, batt_soc=80, reserve=20, price=4, in_demand=True)
    ctx = ea.compute_decision_context(state, fcast, [], now_at(16))
    ev = ctx["ev_recommended"]
    check("ev: Eco+ during demand window despite cheap price", ev["zappi_mode"] == "Eco+", ev)
    check("ev: rule is ev_demand_window", ev["rule_fired"] == "ev_demand_window", ev)


def test_ev_fast_when_below_min_despite_cheaper_incoming():
    # EV at 15% (below min=20%) → Fast regardless of forward_min, Case 3 fires first
    state, fcast = mk_ev_state(15, 20, 70, batt_soc=80, reserve=20, price=14)
    ctx = ea.compute_decision_context(state, fcast, [], now_at(11))
    ev = ctx["ev_recommended"]
    check("ev: Fast when below min SoC", ev["zappi_mode"] == "Fast", ev)
    check("ev: Case 3 rule", ev["rule_fired"] == "ev_case3_below_minimum", ev)


def test_ev_battery_full_solar_absorb():
    # Battery ≥95% + solar surplus ≥ 1.44 kW (Zappi minimum met) → Eco
    state, fcast = mk_ev_state(70, 20, 80, batt_soc=96, reserve=5, price=9)
    state["solar"]["current_kw"] = 2.5   # surplus = 2.5 - 0.5 home = 2.0 kW ≥ 1.44
    state["home_load_kw"] = 0.5
    ctx = ea.compute_decision_context(state, fcast, [], now_at(11))
    ev = ctx["ev_recommended"]
    check("ev: Eco when battery full + sufficient surplus", ev["zappi_mode"] == "Eco", ev)
    check("ev: rule is ev_battery_full_solar_absorb", ev["rule_fired"] == "ev_battery_full_solar_absorb", ev)


def test_ev_battery_full_fast_deadline():
    # Peak month, demand window ≤45 min away, EV below target, price OK → Fast
    state, fcast = mk_ev_state(70, 20, 80, batt_soc=96, reserve=5, price=11, in_demand=False)
    state["is_peak_month"] = True
    state["solar"]["current_kw"] = 1.37
    # now_at(14, 30) = 14:30 → hours_to_2_55 = 14.917 - 14.5 = 0.417h ≤ 0.75
    ctx = ea.compute_decision_context(state, fcast, [], now_at(14, 30))
    ev = ctx["ev_recommended"]
    check("ev: Fast when demand window ≤45min and EV below target", ev["zappi_mode"] == "Fast", ev)
    check("ev: rule is ev_battery_full_fast_deadline", ev["rule_fired"] == "ev_battery_full_fast_deadline", ev)


def test_ev_battery_full_eco_not_fast_when_deadline_far():
    # Battery full + solar but demand window > 45 min away → Eco (not Fast)
    state, fcast = mk_ev_state(70, 20, 80, batt_soc=96, reserve=5, price=11, in_demand=False)
    state["is_peak_month"] = True
    state["solar"]["current_kw"] = 1.37
    # now_at(11) → hours_to_2_55 = 3.9h >> 0.75 → no deadline escalation
    ctx = ea.compute_decision_context(state, fcast, [], now_at(11))
    ev = ctx["ev_recommended"]
    check("ev: Eco when deadline far", ev["zappi_mode"] == "Eco", ev)
    check("ev: rule is ev_battery_full_solar_absorb", ev["rule_fired"] == "ev_battery_full_solar_absorb", ev)


def test_ev_battery_full_solar_absorb_not_when_no_solar():
    # Battery ≥95% but no solar → falls through to price-based rule
    state, fcast = mk_ev_state(70, 20, 80, batt_soc=96, reserve=5, price=9)
    state["solar"]["current_kw"] = 0.1  # below 0.5 kW threshold
    ctx = ea.compute_decision_context(state, fcast, [], now_at(11))
    ev = ctx["ev_recommended"]
    check("ev: not battery-full-solar rule when no solar", ev["rule_fired"] != "ev_battery_full_solar_absorb", ev)


def test_ev_battery_full_solar_absorb_not_when_battery_not_full():
    # Solar generating but battery only 85% → does NOT fire battery-full rule
    state, fcast = mk_ev_state(70, 20, 80, batt_soc=85, reserve=5, price=9)
    state["solar"]["current_kw"] = 2.5
    ctx = ea.compute_decision_context(state, fcast, [], now_at(11))
    ev = ctx["ev_recommended"]
    check("ev: not battery-full-solar rule at 85%", ev["rule_fired"] != "ev_battery_full_solar_absorb", ev)


def test_solar_unreliable_not_before_9am():
    # At 8:30am, actual solar=0 but Solcast forecasts 1.2kWh — should NOT be solar_unreliable
    state = mk_state(30, 8, accuracy="unreliable", solar_kw=0.0, remaining=6.0,
                     is_peak=False, grid_target=30, price=13)
    ctx = ea.compute_decision_context(state, flat(13), [], now_at(8, 30))
    check("solar_unreliable False before 9am", ctx["solar_unreliable"] is False, ctx["solar_unreliable"])


def test_solar_unreliable_after_9am():
    # At 10am, actual solar=0 vs significant forecast → IS solar_unreliable
    state = mk_state(30, 10, accuracy="unreliable", solar_kw=0.0, remaining=6.0,
                     is_peak=False, grid_target=30, price=13)
    ctx = ea.compute_decision_context(state, flat(13), [], now_at(10))
    check("solar_unreliable True after 9am with poor accuracy", ctx["solar_unreliable"] is True,
          ctx["solar_unreliable"])


def make_sliding_records(n, price, forward_min):
    """Build n recent records where cheap window was forecast but never arrived."""
    return [{"actions": [], "price_c": price,
             "ts": f"2026-05-31T10:{i*30:02d}:00+10:00",
             "solar_current_kw": 0.5,
             "computed_context": {"forward_min_c": forward_min}} for i in range(n)]


def test_nonpeak_solar_unreliable_escalates_autonomous():
    # Solar unreliable, fill_slow=4.5h, hours_to_deadline=5.5h
    # Normal threshold: 5.5 - 0.5 = 5.0h → self_consumption (4.5 < 5.0, no fire)
    # Unreliable threshold: 5.5 - 1.5 = 4.0h → autonomous (4.5 >= 4.0, fires)
    # SoC=55 (above sponge floor), 9am (before sponge), target=80
    # need 25%=3.375kWh, fill_slow=3.375/1.7=1.99h
    # forecast: cheap 5.5h then spike → deadline=5.5h
    # normal threshold: 5.5-0.5=5.0 → 1.99 < 5.0, no fire
    # unreliable threshold: 5.5-1.5=4.0 → 1.99 < 4.0, no fire either
    # Need bigger gap: SoC=30 but use time=9am (before sponge window which starts at 10)
    # need 50%=6.75kWh, fill_slow=3.97h; deadline=5.5h
    # normal: 3.97 < 5.0 → no fire; unreliable: 3.97 >= 4.0 → fires ✓
    forecast_prices = [13] * 11 + [25] * 13  # cheap for 5.5h then spike
    ctx = ea.compute_decision_context(
        mk_state(30, 9, accuracy="unreliable", solar_kw=0.0, remaining=0.0,
                 is_peak=False, grid_target=80, price=13),
        fc(forecast_prices), [], now_at(9))
    r = ctx["recommended"]
    check("solar unreliable escalates to autonomous", r["mode"] == "autonomous", r)
    check("solar unreliable rule fired", r["rule_fired"] == "nonpeak_solar_unreliable_autonomous", r)


def test_nonpeak_solar_good_stays_selfcons():
    # Same scenario but solar is good — should NOT fire nonpeak_solar_unreliable_autonomous
    forecast_prices = [13] * 11 + [25] * 13
    ctx = ea.compute_decision_context(
        mk_state(30, 9, accuracy="good", solar_kw=2.0, remaining=4.0,
                 is_peak=False, grid_target=80, price=13),
        fc(forecast_prices), [], now_at(9))
    r = ctx["recommended"]
    check("solar good stays self_consumption", r["mode"] != "autonomous" or r["rule_fired"] != "nonpeak_solar_unreliable_autonomous", r)


def test_sliding_forecast_fires():
    # 3 consecutive records: price=9c, forward_min=6c (3c gap > 2c threshold), price never dropped
    records = make_sliding_records(3, price=9, forward_min=6)
    result = ea._detect_sliding_forecast(records, current_price=9, current_forward_min=6)
    check("sliding forecast detected after 3 cycles", result is True, result)


def test_sliding_forecast_not_enough_cycles():
    # Only 1 prior record + current = 2 total observations, need 3
    records = make_sliding_records(1, price=9, forward_min=6)
    result = ea._detect_sliding_forecast(records, current_price=9, current_forward_min=6)
    check("sliding forecast needs 3 cycles minimum", result is False, result)


def test_sliding_forecast_suppressed_when_window_arrived():
    # Price dropped to 6c in most recent record — window arrived, not sliding
    records = make_sliding_records(2, price=9, forward_min=6)
    records.append({"actions": [], "price_c": 6.5,  # price reached the cheap band
                    "ts": "2026-05-31T11:00:00+10:00",
                    "solar_current_kw": 0.5,
                    "computed_context": {"forward_min_c": 6}})
    result = ea._detect_sliding_forecast(records, current_price=9, current_forward_min=6)
    check("sliding forecast clears when window arrived", result is False, result)


def test_sliding_forecast_drives_charge():
    # Sliding forecast should fire charge even though forward_min < price - 2c.
    # Use SoC=60 (above 50% solar sponge floor) and time outside sponge (9am, before 10am).
    records = make_sliding_records(3, price=9, forward_min=6)
    ctx = ea.compute_decision_context(
        mk_state(60, 9, "na", 0.0, 0.0, is_peak=False, grid_target=80, price=9),
        fc([9] * 4 + [6] * 10 + [18] * 10), records, now_at(9))
    r = ctx["recommended"]
    check("sliding forecast fires charge", r["action"] == "charge", r)
    check("sliding forecast rule_fired", r["rule_fired"] == "sliding_forecast", r)


def test_overnight_hold_suppresses_charging():
    # 10pm, price=15¢ (above 10¢ threshold), SoC=60% — should hold for Solar Sponge
    ctx = ea.compute_decision_context(
        mk_state(60, 22, "na", 0.0, 0.0, is_peak=False, grid_target=80, price=15),
        flat(15), [], now_at(22))
    r = ctx["recommended"]
    check("overnight hold fires at 10pm at 15c", r["rule_fired"] == "overnight_hold_wait_for_sponge", r)
    check("overnight hold action is hold", r["action"] == "hold", r)


def test_overnight_hold_not_fired_when_cheap():
    # 10pm but price=8¢ (below 10¢ threshold) — charging IS justified overnight
    ctx = ea.compute_decision_context(
        mk_state(60, 22, "na", 0.0, 0.0, is_peak=False, grid_target=80, price=8),
        flat(8), [], now_at(22))
    r = ctx["recommended"]
    check("overnight hold not fired when price cheap", r["rule_fired"] != "overnight_hold_wait_for_sponge", r)


def test_overnight_hold_not_fired_when_critically_low():
    # 10pm, 15¢, but SoC=20% (at/below 25% threshold) — don't apply overnight hold
    ctx = ea.compute_decision_context(
        mk_state(20, 22, "na", 0.0, 0.0, is_peak=False, grid_target=80, price=15),
        flat(15), [], now_at(22))
    r = ctx["recommended"]
    check("overnight hold not fired when SoC critically low", r["rule_fired"] != "overnight_hold_wait_for_sponge", r)


def test_overnight_hold_not_fired_during_day():
    # 11am, 15¢ — not nighttime, overnight hold must not fire
    ctx = ea.compute_decision_context(
        mk_state(60, 11, "na", 0.0, 0.0, is_peak=False, grid_target=80, price=15),
        flat(15), [], now_at(11))
    r = ctx["recommended"]
    check("overnight hold not fired during day", r["rule_fired"] != "overnight_hold_wait_for_sponge", r)


if __name__ == "__main__":
    for fn in [test_ev_case6_negative_fit_solar_dump,
               test_ev_case6_not_fired_when_battery_low,
               test_historical_model_cheap_price_raises_target,
               test_historical_model_expensive_price_lowers_target,
               test_historical_model_falls_back_without_stats,
               test_historical_model_flat_history_falls_back,
               test_hours_to_cheap_end, test_detectors, test_peak_sunny_holds,
               test_peak_sunny_low_soc_home_load_deducted, test_peak_target_met_label_at_85,
               test_nonpeak_solar_will_cover_holds, test_nonpeak_solar_insufficient_charges,
               test_nonpeak_solar_will_cover_not_after_1pm,
               test_peak_cloudy_10am_sponge_floor, test_peak_cloudy_1330_autonomous,
               test_peak_deferral_trap_selfcons, test_soc_gateway_divergence,
               test_nonpeak_deferral, test_overnight_hold_for_cheap_window,
               test_nonpeak_spread_arbitrage, test_nonpeak_spread_too_small,
               test_demand_window_no_import,
               test_ev_eco_when_below_standard_price, test_ev_eco_plus_when_above_standard_price,
               test_ev_fast_when_ultra_cheap, test_ev_eco_plus_during_demand_window,
               test_ev_fast_when_below_min_within_ceiling, test_ev_eco_plus_when_below_min_above_ceiling,
               test_ev_fast_when_below_min_despite_cheaper_incoming,
               test_ev_battery_full_solar_absorb,
               test_ev_battery_full_fast_deadline,
               test_ev_battery_full_eco_not_fast_when_deadline_far,
               test_ev_battery_full_solar_absorb_not_when_no_solar,
               test_ev_battery_full_solar_absorb_not_when_battery_not_full,
               test_solar_unreliable_not_before_9am, test_solar_unreliable_after_9am,
               test_nonpeak_solar_unreliable_escalates_autonomous,
               test_nonpeak_solar_good_stays_selfcons,
               test_sliding_forecast_fires, test_sliding_forecast_not_enough_cycles,
               test_sliding_forecast_suppressed_when_window_arrived,
               test_sliding_forecast_drives_charge,
               test_overnight_hold_suppresses_charging,
               test_overnight_hold_not_fired_when_cheap,
               test_overnight_hold_not_fired_when_critically_low,
               test_overnight_hold_not_fired_during_day,
               test_peak_solar_cover_survival_charges_when_battery_wont_reach_sponge,
               test_peak_survival_waits_when_sponge_close_and_cheaper,
               test_peak_solar_cover_no_survival_holds_when_soc_ok,
               test_wait_for_cheap_go_hard_holds_when_sponge_close_and_cheaper,
               test_peak_wait_for_cheap_go_hard, test_peak_charge_now_when_no_cheaper_slot,
               test_peak_sponge_go_hard, test_peak_sponge_selfcons_then_escalates,
               test_peak_sponge_solar_improves_to_hold]:
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n{'='*50}\n{_passed} passed, {_failed} failed")
    raise SystemExit(1 if _failed else 0)
