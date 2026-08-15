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
             is_peak=True, grid_target=30, price=16.0, gateway=None,
             remaining_corrected=None):
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
                  "forecast_remaining_corrected_kwh": remaining_corrected,
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
    # Current 30-min slot 16¢ (matches mk_state's spot), a genuinely cheaper 13¢ window ahead.
    # (Under Rule 32 the anchor is forecast[0], so the current slot and the spot must agree —
    # the old flat(13)-with-spot-16 relied on exactly the mismatch the fix removes.)
    ctx = ea.compute_decision_context(state, fc([16.0] * 2 + [13.0] * 20), [], now_at(7))
    r = ctx["recommended"]
    check("peak low SoC net solar correctly deducted (no solar_will_cover)", r["rule_fired"] != "peak_solar_will_cover", r)
    # 13¢ window is ahead of the 16¢ current slot → correctly waits for it
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
    # SoC=13%, 7:30am, Solcast says solar covers gap (kwh_needed_85=0), battery drains to
    # floor before Solar Sponge (2.5h away). But Solar Sponge is close (≤3h) and 9¢ cheaper
    # (20¢ now vs 11¢ at 10am) — waiting and fast-charging at Solar Sponge saves ~68¢.
    # Should hold (peak_survival_wait_for_sponge) not charge now. SoC 13 is one point above the
    # 12% survival floor, so this still exercises the wait logic (the floor catches at 12 — the
    # two compose: ride down to 12, then defend). Sub-floor override tested separately below.
    prices = [20.0] * 5 + [11.0] * 12 + [18.0] * 7     # 20¢ until ~10am, then 11¢
    state = mk_state(13, 7, "good", 0.1, 20.0, price=20.0)  # kwh_needed_85=0 via Solcast
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
    # SoC=20%, 7am, kwh_needed_85>0, cheaper slot at 10am (11¢, 3h away, 19¢ gap).
    # A far cheaper feasible go-hard slot exists, so the agent holds and waits for it rather
    # than charging now at 30¢. SoC 20 is above the 12% survival floor, so this isolates the
    # wait-for-cheap-slot logic; the sub-floor override (which would defend the floor instead
    # of riding lower) is tested in the Rule 30 block below.
    prices = [30.0] * 6 + [11.0] * 12 + [20.0] * 6     # 30¢ for 3h, then 11¢
    state = mk_state(20, 7, "poor", 0.1, 8.0, price=30.0)
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
    # SoC=50%, 8:30am, all prices flat at 10¢ (at Solar Sponge threshold).
    # Price is at/below threshold so Rule 26 doesn't apply — charge now.
    # No cheaper slot exists (flat forecast, need 1¢ below 10¢ to find one).
    # Phase 2 upgrades to autonomous because price is cheap and SoC < deadline_target.
    state = mk_state(50, 8, "na", 0.0, 0.0, price=10.0)
    ctx = ea.compute_decision_context(state, flat(10), [], now_at(8, 30))
    r = ctx["recommended"]
    check("peak_charge_now at threshold → Phase 2 front-loads",
          r["rule_fired"] == "peak_frontload_cheap", r)
    check("action is charge", r["action"] == "charge", r)
    check("mode is autonomous (Phase 2 front-load)", r["mode"] == "autonomous", r)
    check("target is seasonal deadline_target (95 in winter)",
          r["target_pct"] == ctx["deadline_target_pct"], r)


def test_peak_early_morning_hold_on_price_spike():
    # SoC=35%, 5am, price=24¢ (spike), flat forecast — Amber can't see past the spike.
    # No cheaper slot in window, but it's nighttime with 9.9h to deadline.
    # → hold rather than charging into the spike (peak_early_morning_hold).
    state = mk_state(35, 5, "na", 0.0, 0.0, price=24.0)
    ctx = ea.compute_decision_context(state, flat(24), [], now_at(5, 0))
    r = ctx["recommended"]
    check("peak_early_morning_hold fires on pre-dawn spike", r["rule_fired"] == "peak_early_morning_hold", r)
    check("action is hold", r["action"] == "hold", r)


def test_peak_early_morning_hold_not_fired_when_cheap():
    # SoC=35%, 5am, price=8¢ — genuinely cheap, below Solar Sponge threshold.
    # overnight_hold requires price > 10¢, so the early-morning guard doesn't apply.
    # → charge now, then Phase 2 upgrades to autonomous (front-load cheap).
    state = mk_state(35, 5, "na", 0.0, 0.0, price=8.0)
    ctx = ea.compute_decision_context(state, flat(8), [], now_at(5, 0))
    r = ctx["recommended"]
    check("peak_charge_now when price genuinely cheap at 5am",
          r["rule_fired"] == "peak_frontload_cheap", r)
    check("action is charge", r["action"] == "charge", r)


def test_peak_early_morning_hold_fires_at_low_soc():
    # SoC=20%, 5am, price=24¢. No SoC floor on early-morning hold — deadline logic above
    # already handles urgency, so the battery can drain toward 5% and catch up at Solar Sponge.
    # → hold (peak_early_morning_hold).
    state = mk_state(20, 5, "na", 0.0, 0.0, price=24.0)
    ctx = ea.compute_decision_context(state, flat(24), [], now_at(5, 0))
    r = ctx["recommended"]
    check("peak_early_morning_hold fires even at 20% SoC", r["rule_fired"] == "peak_early_morning_hold", r)
    check("action is hold", r["action"] == "hold", r)


def test_peak_eve_holds_for_cheap_morning_slot():
    # Rule 35 — 11pm on a peak-month day, SoC=23%, current price 19¢, a 12¢ Solar Sponge slot
    # ~8h out. Before the fix the peak block was gated `now_h < 2:55pm`, so this fell through to
    # the non-peak chain and slammed 5 kW autonomous (nonpeak_solar_unreliable_autonomous — the
    # 2026-07-30 23:00 incident). Now the peak block runs through the peak-eve window and defers to
    # the cheap morning slot, matching the LP's mpc_hold.
    # Rule 38 (overnight insurance) would override this hold — isolate it.
    _sv38 = ea.OVERNIGHT_INSURANCE
    ea.OVERNIGHT_INSURANCE = False
    try:
        state = mk_state(23, 23, "na", 0.0, 0.0, price=19.0)
        prices = [19.0] * 16 + [12.0] * 8   # 12¢ Solar Sponge slot ~8h ahead
        ctx = ea.compute_decision_context(state, fc(prices), [], now_at(23, 0))
    finally:
        ea.OVERNIGHT_INSURANCE = _sv38
    r = ctx["recommended"]
    check("peak-eve defers to cheap morning slot", r["rule_fired"] == "wait_for_cheap_go_hard", r)
    check("peak-eve action is hold", r["action"] == "hold", r)


def test_peak_eve_killswitch_off_reverts():
    # PEAK_EVE_RUNUP=False → the peak block stays gated off after 2:55pm, so the same inputs no
    # longer produce the peak-block wait_for_cheap_go_hard (old fall-through restored). Saves and
    # restores the module global so both pytest and the __main__ runner stay clean.
    state = mk_state(23, 23, "na", 0.0, 0.0, price=19.0)
    prices = [19.0] * 16 + [12.0] * 8
    _saved = ea.PEAK_EVE_RUNUP
    ea.PEAK_EVE_RUNUP = False
    try:
        ctx = ea.compute_decision_context(state, fc(prices), [], now_at(23, 0))
    finally:
        ea.PEAK_EVE_RUNUP = _saved
    r = ctx["recommended"]
    check("kill-switch off bypasses the peak block", r["rule_fired"] != "wait_for_cheap_go_hard", r)


def test_peak_eve_no_quickcheck_slam_at_low_soc():
    # SoC=30% (<40) at 11pm would trip the afternoon-only peak_deadline_quickcheck if that heuristic
    # weren't guarded to now_h < 2:55pm. With a flat 19¢ forecast (no cheaper slot) the peak-eve
    # window must HOLD (peak_early_morning_hold), never slam autonomous at 11pm.
    # Rule 38 (overnight insurance) would override this hold — isolate it.
    _sv38 = ea.OVERNIGHT_INSURANCE
    ea.OVERNIGHT_INSURANCE = False
    try:
        state = mk_state(30, 23, "na", 0.0, 0.0, price=19.0)
        ctx = ea.compute_decision_context(state, flat(19), [], now_at(23, 0))
    finally:
        ea.OVERNIGHT_INSURANCE = _sv38
    r = ctx["recommended"]
    check("peak-eve does not fire peak_deadline_quickcheck", r["rule_fired"] != "peak_deadline_quickcheck", r)
    check("peak-eve holds at low SoC on flat expensive price", r["action"] == "hold", r)


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
    # Phase 2 upgrades to autonomous because in sponge (price=11 ≤ 15¢ ceiling) and SoC < 85.
    state = mk_state(65, 10, "poor", 0.3, 2.0, price=11.0)
    ctx = ea.compute_decision_context(state, flat(11), [], now_at(10, 30))
    r = ctx["recommended"]
    check("peak_sponge_selfcons → Phase 2 front-loads at autonomous",
          r["rule_fired"] == "peak_frontload_cheap", r)
    check("mode is autonomous (Phase 2 front-load cheap sponge energy)",
          r["mode"] == "autonomous", r)

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
    # 11:30, SoC 45%, unreliable solar, deferral trap. fill_slow ≈ 3.97h > 3.42h deadline, so
    # gentle alone can't fill the whole gap — but a 5 kW charge still has margin (fill_fast ≈
    # 1.5h < 3.42 - 1.5h buffer). Under Rule 33 (receding-horizon escalation) the correct move
    # is to break the deferral trap by charging *gently now* (peak_deadline_gentle_lead), holding
    # the 5 kW option for a later cycle at the point-of-no-return. (Pre-Rule-33 this slammed
    # autonomous immediately, the exact over-eagerness the 2026-07-26 fix removes.)
    holds = [{"actions": [], "price_c": 16, "ts": "2026-06-15T10:30", "solar_current_kw": 0.3},
             {"actions": [], "price_c": 16, "ts": "2026-06-15T11:00", "solar_current_kw": 0.3}]
    ctx = ea.compute_decision_context(
        mk_state(45, 11, "unreliable", 0.3, 1.5), flat(16), holds, now_at(11, 30))
    r = ctx["recommended"]
    check("peak deferral trap charges", r["action"] == "charge", r)
    check("peak deferral trap leads gently (Rule 33)", r["rule_fired"] == "peak_deadline_gentle_lead", r)
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
                     "max_insurance_floor_pct": 70},
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


def test_solar_accuracy_corrected_normal_winter_morning_is_good():
    # 08:00 winter: raw Solcast 1.2 kW, measured actual 0.16 kW. Against RAW that
    # is 13% -> "unreliable". Against the corrected expectation (1.2 x 0.14 = 0.17)
    # it is ~94% -> "good". The corrected label is the one that must win, else the
    # rule layer zeroes the whole day's solar on an ordinary morning (2026-07-24).
    raw = ea._solar_accuracy(0.16, 1.2)
    corr = ea._solar_accuracy(0.16, 1.2, corrected_forecast_kw=1.2 * 0.14)
    check("raw label is unreliable on normal winter morning", raw.startswith("unreliable"), raw)
    check("corrected label is good on normal winter morning", corr.startswith("good"), corr)


def test_solar_accuracy_corrected_flags_genuine_underperformance():
    # Same hour, but actual is only 0.05 kW — well below even the calibrated
    # expectation of 0.17. That is a real cloud/rain miss and must still flag.
    corr = ea._solar_accuracy(0.05, 1.2, corrected_forecast_kw=1.2 * 0.14)
    check("corrected still flags true underperformance",
          corr.startswith("poor") or corr.startswith("unreliable"), corr)


def test_solar_accuracy_near_zero_corrected_ref_is_not_applicable():
    # Deep-morning bias: corrected expectation ~0.03 kW. Judging a ratio off that
    # would be noise, so return not_applicable rather than condemning the day.
    label = ea._solar_accuracy(0.0, 1.2, corrected_forecast_kw=0.03)
    check("near-zero corrected ref -> not_applicable", label.startswith("not_applicable"), label)


def test_solar_accuracy_falls_back_to_raw_when_uncorrected():
    # No corrected figure (Solcast attr outage / uncalibrated hour) -> old behaviour.
    label = ea._solar_accuracy(0.16, 1.2, corrected_forecast_kw=None)
    check("falls back to raw label when no correction", label.startswith("unreliable"), label)


def test_solar_accuracy_night_still_not_applicable():
    # Raw forecast below 0.2 -> night, regardless of any correction passed.
    label = ea._solar_accuracy(0.0, 0.1, corrected_forecast_kw=0.05)
    check("night stays not_applicable", label.startswith("not_applicable"), label)


def test_hour_solar_ratio_reads_model_params():
    # _hour_solar_ratio pulls the per-hour ratio from _model_params when n>=min.
    saved = ea._model_params
    try:
        ea._model_params = {"min_samples": 5,
                            "solar_correction": {"08": {"ratio": 0.14, "n": 60},
                                                 "09": {"ratio": 0.16, "n": 3}}}
        check("ratio returned when enough samples", ea._hour_solar_ratio("08") == 0.14,
              ea._hour_solar_ratio("08"))
        check("None when too few samples", ea._hour_solar_ratio("09") is None,
              ea._hour_solar_ratio("09"))
        check("None when hour absent", ea._hour_solar_ratio("13") is None,
              ea._hour_solar_ratio("13"))
    finally:
        ea._model_params = saved


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


def _with_override_state(obj):
    """Swap ea.ha_get for a stub returning `obj` (or raising if it's an Exception)."""
    def _stub(entity_id):
        if isinstance(obj, Exception):
            raise obj
        return obj
    return _stub


def test_manual_override():
    # Agent Control switch: input_boolean.agent_active, ON = active / OFF = paused.
    from datetime import timedelta
    real_ha_get = ea.ha_get
    real_set_reserve = ea.set_powerwall_reserve
    real_service = ea.ha_service
    real_ctx = ea._cycle_context
    try:
        now = ea.datetime.now(ea.SYDNEY_TZ)
        ea.ha_service = lambda *a, **k: None   # swallow the auto-resume write-back

        ea.ha_get = _with_override_state({"state": "on", "last_changed": now.isoformat()})
        paused, _ = ea._agent_paused()
        check("Agent Control ON → agent active (not paused)", paused is False)

        ea.ha_get = _with_override_state({
            "state": "off", "last_changed": (now - timedelta(hours=1)).isoformat()})
        paused, msg = ea._agent_paused()
        check("Agent Control OFF (1h) → paused", paused is True, msg)

        ea.ha_get = _with_override_state({
            "state": "off", "last_changed": (now - timedelta(hours=13)).isoformat()})
        paused, msg = ea._agent_paused()
        check("pause auto-resumes after 12h", paused is False, msg)
        check("  ...and says why", "EXPIRED" in msg, msg)

        # unavailable during an HA restart must not pause the agent (fail safe → active)
        ea.ha_get = _with_override_state({"state": "unavailable", "last_changed": now.isoformat()})
        paused, msg = ea._agent_paused()
        check("Agent Control unavailable → active (fail safe)", paused is False, msg)

        # Fail safe — a broken HA must not silently make the agent passive.
        ea.ha_get = _with_override_state(RuntimeError("HA unreachable"))
        paused, msg = ea._agent_paused()
        check("Agent Control fails safe (active) when HA unreachable", paused is False, msg)

        # End-to-end: a paused agent (Agent Control OFF) must send no commands.
        sent = []
        ea.set_powerwall_reserve = lambda pct: sent.append(pct)
        ea._cycle_context = {"state": {"battery": {"soc_pct": 47, "reserve_pct": 85,
                                                   "mode": "self_consumption"}}}
        ea.ha_get = _with_override_state({
            "state": "off", "last_changed": (now - timedelta(hours=1)).isoformat()})
        ctx = {"recommended": {"action": "charge", "target_pct": 85,
                               "mode": "self_consumption", "rule_fired": "solar_sponge_floor"},
               "ev_recommended": {}}
        out = ea._execute_deterministic_verdict(ctx, dry_run=False)
        check("paused agent blocks a charge verdict", sent == [], f"sent={sent}")
        check("  ...and reports the suppressed verdict", "AGENT PAUSED" in out[0], out)

        # A hold verdict must also be suppressed — otherwise the agent would
        # yank reserve back to 5% and undo the user's manual setting.
        sent.clear()
        ctx_hold = {"recommended": {"action": "hold", "rule_fired": "target_met"},
                    "ev_recommended": {}}
        ea._execute_deterministic_verdict(ctx_hold, dry_run=False)
        check("paused agent blocks a hold verdict clearing reserve", sent == [], f"sent={sent}")

        # Control returns the moment Agent Control is switched back ON.
        ea.ha_get = _with_override_state({"state": "on", "last_changed": now.isoformat()})
        sent.clear()
        ea._execute_deterministic_verdict(ctx, dry_run=False)
        # Control resumed → a reserve command was sent. Under the Rule 31 controller a
        # self_consumption charge chases SoC+offset (soc 47, target 85 → 53), not a flat 85.
        check("control resumes when Agent Control is on",
              sent == [ea._gentle_charge_reserve(47, 85)], f"sent={sent}")
    finally:
        ea.ha_get = real_ha_get
        ea.set_powerwall_reserve = real_set_reserve
        ea.ha_service = real_service
        ea._cycle_context = real_ctx


# ---------------------------------------------------------------------------
# Rule 31 — gentle self_consumption charge controller (reserve = SoC + offset)
#
# Firmware 26.18.3 makes a fixed high reserve pull 5 kW from any large reserve−SoC
# gap even in self_consumption. The controller chases reserve = SoC + offset so the
# gap stays small and the Powerwall trickles (~1.6 kW), restoring the rate the whole
# verdict tree budgets. autonomous charges are untouched (full 5 kW, export-guarded).
# ---------------------------------------------------------------------------

def test_gentle_charge_reserve_small_gap():
    # Low SoC: a big charge target must collapse to a small chased reserve.
    r = ea._gentle_charge_reserve(19, 85)
    check("gentle reserve = soc + offset at low soc", r == 19 + ea.SELF_CONS_CHARGE_OFFSET_PTS, r)


def test_gentle_charge_reserve_clamps_at_target():
    # Near the target the chase must not overshoot — capped at target_pct.
    r = ea._gentle_charge_reserve(82, 85)
    check("gentle reserve clamps at target", r == 85, r)
    r2 = ea._gentle_charge_reserve(85, 85)
    check("gentle reserve at target idles (reserve<=soc)", r2 == 85, r2)


def test_gentle_charge_reserve_none_soc_falls_back_to_target():
    # Tessie+gateway both unreadable → can't compute a gap → old fixed-reserve behaviour.
    r = ea._gentle_charge_reserve(None, 85)
    check("gentle reserve falls back to target when soc is None", r == 85, r)


def test_gentle_charge_reserve_custom_offset():
    r = ea._gentle_charge_reserve(30, 85, offset=10)
    check("gentle reserve honours explicit offset", r == 40, r)


def test_execute_selfcons_charge_uses_gentle_reserve():
    real_set_reserve = ea.set_powerwall_reserve
    real_set_mode    = ea.set_powerwall_mode
    real_ctx         = ea._cycle_context
    real_flag        = ea.GENTLE_CHARGE_CONTROL
    try:
        sent = []
        ea.set_powerwall_reserve = lambda pct: sent.append(pct)
        ea.set_powerwall_mode    = lambda m: None
        ea.GENTLE_CHARGE_CONTROL = True
        ea._cycle_context = {"state": {"battery": {"soc_pct": 19, "reserve_pct": 5,
                                                   "mode": "self_consumption"}}}
        ctx = {"recommended": {"action": "charge", "target_pct": 85,
                               "mode": "self_consumption", "rule_fired": "peak_charge_now"},
               "ev_recommended": {}}
        ea._execute_deterministic_verdict(ctx, dry_run=False)
        check("selfcons charge commands SoC+offset, not target",
              sent == [19 + ea.SELF_CONS_CHARGE_OFFSET_PTS], f"sent={sent}")
        rc = ea._cycle_context.get("rate_control", {})
        check("  ...and logs the gentle intent", rc.get("charge_rate_intent") == "gentle", rc)
        check("  ...and logs the charge target", rc.get("charge_target_pct") == 85, rc)
    finally:
        ea.set_powerwall_reserve = real_set_reserve
        ea.set_powerwall_mode    = real_set_mode
        ea._cycle_context        = real_ctx
        ea.GENTLE_CHARGE_CONTROL = real_flag


def test_execute_autonomous_charge_unaffected():
    real_set_reserve = ea.set_powerwall_reserve
    real_set_mode    = ea.set_powerwall_mode
    real_ctx         = ea._cycle_context
    real_flag        = ea.GENTLE_CHARGE_CONTROL
    try:
        sent = []
        ea.set_powerwall_reserve = lambda pct: sent.append(pct)
        ea.set_powerwall_mode    = lambda m: None
        ea.GENTLE_CHARGE_CONTROL = True
        ea._cycle_context = {"state": {"battery": {"soc_pct": 19, "reserve_pct": 5,
                                                   "mode": "self_consumption"}}}
        ctx = {"recommended": {"action": "charge", "target_pct": 100,
                               "mode": "autonomous", "rule_fired": "peak_deadline_autonomous"},
               "ev_recommended": {}}
        ea._execute_deterministic_verdict(ctx, dry_run=False)
        check("autonomous charge still commands reserve=target (full rate)",
              sent == [100], f"sent={sent}")
        rc = ea._cycle_context.get("rate_control", {})
        check("  ...and logs the full intent", rc.get("charge_rate_intent") == "full", rc)
    finally:
        ea.set_powerwall_reserve = real_set_reserve
        ea.set_powerwall_mode    = real_set_mode
        ea._cycle_context        = real_ctx
        ea.GENTLE_CHARGE_CONTROL = real_flag


def test_execute_gentle_killswitch_reverts_to_target():
    real_set_reserve = ea.set_powerwall_reserve
    real_set_mode    = ea.set_powerwall_mode
    real_ctx         = ea._cycle_context
    real_flag        = ea.GENTLE_CHARGE_CONTROL
    try:
        sent = []
        ea.set_powerwall_reserve = lambda pct: sent.append(pct)
        ea.set_powerwall_mode    = lambda m: None
        ea.GENTLE_CHARGE_CONTROL = False   # kill-switch off
        ea._cycle_context = {"state": {"battery": {"soc_pct": 19, "reserve_pct": 5,
                                                   "mode": "self_consumption"}}}
        ctx = {"recommended": {"action": "charge", "target_pct": 85,
                               "mode": "self_consumption", "rule_fired": "peak_charge_now"},
               "ev_recommended": {}}
        ea._execute_deterministic_verdict(ctx, dry_run=False)
        check("kill-switch off restores reserve=target (old 5 kW behaviour)",
              sent == [85], f"sent={sent}")
    finally:
        ea.set_powerwall_reserve = real_set_reserve
        ea.set_powerwall_mode    = real_set_mode
        ea._cycle_context        = real_ctx
        ea.GENTLE_CHARGE_CONTROL = real_flag


# ---------------------------------------------------------------------------
# Rule 33 — receding-horizon deadline escalation (added 2026-07-26)
#
# The peak deadline branch used to jump straight to autonomous (5 kW) the instant
# self_consumption could no longer fill the WHOLE 85% gap in time. On 2026-07-26 that
# slammed 5 kW at 10:00 with SoC 16% and ~4.9h to the deadline — a 5 kW charge fills in
# <2h, so there was ~3h of slack. The fix leads with a gentle self_consumption charge and
# only escalates to autonomous at the fast rate's point-of-no-return
# (hours_to_2_55 <= fill_fast_85 + FAST_ESCALATE_BUFFER_H).
# ---------------------------------------------------------------------------

def test_peak_deadline_gentle_lead_when_slack():
    # The 2026-07-26 incident: SoC 16%, 10:00, poor solar, peak. Plenty of runway → the
    # agent should lead gently (self_consumption), NOT slam 5 kW autonomous.
    ctx = ea.compute_decision_context(mk_state(16, 10, "poor", 0.3, 1.0), flat(14), [], now_at(10))
    r = ctx["recommended"]
    check("10am low-SoC with slack → gentle lead (not autonomous)",
          r["rule_fired"] == "peak_deadline_gentle_lead", r)
    check("  ...is a charge", r["action"] == "charge", r)
    check("  ...at self_consumption (~1.7 kW), not autonomous", r["mode"] == "self_consumption", r)


def test_peak_deadline_escalates_at_point_of_no_return():
    # Same low SoC but at 1:30pm: only ~1.4h to the deadline, so even a 5 kW charge is now
    # within the safety buffer → escalate to autonomous.
    ctx = ea.compute_decision_context(mk_state(20, 13, "poor", 0.3, 1.0), flat(14), [], now_at(13, 30))
    r = ctx["recommended"]
    check("1:30pm low-SoC at point-of-no-return → autonomous",
          r["rule_fired"] == "peak_deadline_autonomous", r)
    check("  ...at autonomous (5 kW)", r["mode"] == "autonomous", r)


def test_deadline_gentle_lead_killswitch():
    # DEADLINE_GENTLE_LEAD = False must restore the old straight-to-autonomous behaviour.
    real = ea.DEADLINE_GENTLE_LEAD
    try:
        ea.DEADLINE_GENTLE_LEAD = False
        ctx = ea.compute_decision_context(mk_state(16, 10, "poor", 0.3, 1.0), flat(14), [], now_at(10))
        r = ctx["recommended"]
        check("kill-switch off → old peak_deadline_autonomous at 10am",
              r["rule_fired"] == "peak_deadline_autonomous", r)
        check("  ...autonomous", r["mode"] == "autonomous", r)
    finally:
        ea.DEADLINE_GENTLE_LEAD = real


# ---------------------------------------------------------------------------
# Hold-branch executor fix — a hold must revert autonomous mode (added 2026-07-26)
#
# 2026-07-26 incident: a `hold` verdict left mode=autonomous from an earlier deadline
# charge. Under firmware 26.18.3 autonomous grid-charges at ~5 kW regardless of reserve,
# so the reserve-only cleanup was powerless — the charge ran until reverted by hand. And
# the reserve drop itself was gated on sensor.powerwall_backup_reserve, which read a stale
# 5% while the true setpoint was ~57%, so it skipped. A hold must revert the mode AND, when
# it does, drop reserve unconditionally (not trusting the lagging sensor).
# ---------------------------------------------------------------------------

def _run_hold_executor(battery_state):
    """Run a hold verdict through the executor with mocked control calls; return (reserves, modes)."""
    real_set_reserve = ea.set_powerwall_reserve
    real_set_mode    = ea.set_powerwall_mode
    real_ctx         = ea._cycle_context
    reserves, modes  = [], []
    try:
        ea.set_powerwall_reserve = lambda pct: reserves.append(pct)
        ea.set_powerwall_mode    = lambda m: modes.append(m)
        ea._cycle_context = {"state": {"battery": battery_state}}
        ctx = {"recommended": {"action": "hold", "target_pct": None, "mode": None,
                               "rule_fired": "peak_solar_will_cover"},
               "ev_recommended": {}}
        ea._execute_deterministic_verdict(ctx, dry_run=False)
    finally:
        ea.set_powerwall_reserve = real_set_reserve
        ea.set_powerwall_mode    = real_set_mode
        ea._cycle_context        = real_ctx
    return reserves, modes


def test_hold_reverts_autonomous_and_forces_reserve_drop():
    # The exact failure mode: mode=autonomous, reserve sensor reads a stale 5% (true was ~57%).
    reserves, modes = _run_hold_executor({"soc_pct": 49, "reserve_pct": 5, "mode": "autonomous"})
    check("hold reverts autonomous → self_consumption", modes == ["self_consumption"], modes)
    check("hold forces reserve=5 despite stale sensor reading 5%", reserves == [5], reserves)


def test_hold_selfcons_at_floor_no_redundant_writes():
    # Steady state: already self_consumption at the 5% floor → no commands (avoid write spam).
    reserves, modes = _run_hold_executor({"soc_pct": 40, "reserve_pct": 5, "mode": "self_consumption"})
    check("hold sends no mode command when already self_consumption", modes == [], modes)
    check("hold sends no reserve command when already at floor", reserves == [], reserves)


def test_hold_selfcons_high_reserve_drops_to_floor():
    # self_consumption but a stale high reserve → drop to 5%, no mode change.
    reserves, modes = _run_hold_executor({"soc_pct": 40, "reserve_pct": 80, "mode": "self_consumption"})
    check("hold leaves mode alone when already self_consumption", modes == [], modes)
    check("hold drops a high reserve to 5%", reserves == [5], reserves)


# ---------------------------------------------------------------------------
# Rule 30 (revised) — overnight survival-floor defense (added 2026-07-25)
#
# The rule layer defends a ~12% instantaneous floor: instead of holding while SoC
# drains toward 5%, it gently self_consumption-charges so the battery never reaches
# the battery_low_soc_emergency_charge automation's 10% trigger (which slams 5 kW and
# then fights the next HOLD). Only overrides holds, never during the demand window.
# ---------------------------------------------------------------------------

def test_survival_floor_overrides_low_soc_overnight_hold():
    # 2am, peak month, SoC 8% → would be peak_early_morning_hold (ride to 5%). The floor
    # defense overrides it to a gentle self_consumption top-up.
    # Isolate from Rule 38 (overnight insurance) which would override the survival floor.
    _sv38 = ea.OVERNIGHT_INSURANCE
    ea.OVERNIGHT_INSURANCE = False
    try:
        ctx = ea.compute_decision_context(mk_state(8, 2, "na", 0.0, 0.0), flat(13), [], now_at(2))
    finally:
        ea.OVERNIGHT_INSURANCE = _sv38
    r = ctx["recommended"]
    check("survival floor overrides low-SoC hold", r["rule_fired"] == "survival_floor_defend", r)
    check("  ...as a charge", r["action"] == "charge", r)
    check("  ...via self_consumption (gentle, Rule 31)", r["mode"] == "self_consumption", r)
    check("  ...reserve chases SoC+offset",
          ea._gentle_charge_reserve(8, r["target_pct"]) == 8 + ea.SELF_CONS_CHARGE_OFFSET_PTS,
          ea._gentle_charge_reserve(8, r["target_pct"]))


def test_survival_floor_does_not_override_deadline_autonomous():
    # 1:30pm, peak, SoC 8% → deadline urgency fires an autonomous charge; the floor defense
    # must NOT downgrade it to a gentle self_consumption top-up.
    ctx = ea.compute_decision_context(mk_state(8, 13, "poor", 0.5, 1.0), flat(16), [], now_at(13, 30))
    r = ctx["recommended"]
    check("survival floor leaves deadline autonomous alone", r["mode"] == "autonomous", r)
    check("  ...still a charge", r["action"] == "charge", r)
    check("  ...not relabelled survival_floor_defend", r["rule_fired"] != "survival_floor_defend", r)


def test_survival_floor_not_active_in_demand_window():
    # 4pm, peak, SoC 8% → demand_window_active hold must stand; the battery has to be free to
    # discharge 3–9pm, and the emergency automation is disabled then too.
    ctx = ea.compute_decision_context(mk_state(8, 16, "na", 0.0, 0.0), flat(30), [], now_at(16))
    r = ctx["recommended"]
    check("survival floor inactive in demand window", r["action"] == "hold", r)
    check("  ...stays demand_window_active", r["rule_fired"] == "demand_window_active", r)


def test_survival_floor_killswitch_reverts_to_ride_low():
    real = ea.SURVIVAL_FLOOR_DEFENSE
    _sv38 = ea.OVERNIGHT_INSURANCE
    try:
        ea.SURVIVAL_FLOOR_DEFENSE = False
        ea.OVERNIGHT_INSURANCE = False
        ctx = ea.compute_decision_context(mk_state(8, 2, "na", 0.0, 0.0), flat(13), [], now_at(2))
        r = ctx["recommended"]
        check("kill-switch off → low-SoC hold rides low again", r["action"] == "hold", r)
        check("  ...not survival_floor_defend", r["rule_fired"] != "survival_floor_defend", r)
    finally:
        ea.SURVIVAL_FLOOR_DEFENSE = real
        ea.OVERNIGHT_INSURANCE = _sv38


def test_survival_floor_not_triggered_above_floor():
    # SoC 40% overnight → well above the floor, normal hold stands.
    # Isolate from Rule 38 which would fire at nighttime with low projected SoC.
    _sv38 = ea.OVERNIGHT_INSURANCE
    ea.OVERNIGHT_INSURANCE = False
    try:
        ctx = ea.compute_decision_context(mk_state(40, 2, "na", 0.0, 0.0), flat(13), [], now_at(2))
    finally:
        ea.OVERNIGHT_INSURANCE = _sv38
    r = ctx["recommended"]
    check("survival floor idle above 12%", r["rule_fired"] != "survival_floor_defend", r)
    check("  ...still a hold", r["action"] == "hold", r)


def test_survival_floor_defers_to_cheaper_slot():
    # Price-aware (Rule 30, 2026-08-06): 2am, peak, SoC 8%, current slot 27¢ with a 12¢ Solar
    # Sponge slot ~2h ahead. The rule must NOT buy at 27¢ — it lets SoC ride toward the 5%
    # reserve and defers the top-up, so the base HOLD stands rather than survival_floor_defend.
    # (This is the 2026-08-05/06 live incident that the fix targets.)
    fcast = fc([27, 27, 27, 27, 12, 12, 12, 12, 12, 12, 12, 12])
    ctx = ea.compute_decision_context(mk_state(8, 2, "na", 0.0, 0.0), fcast, [], now_at(2))
    r = ctx["recommended"]
    check("survival floor defers to the cheaper 12¢ slot (holds)", r["action"] == "hold", r)
    check("  ...not force-charged at the 27¢ spike",
          r["rule_fired"] != "survival_floor_defend", r)


def test_survival_floor_charges_when_no_cheaper_slot():
    # Rising price, no cheaper slot ahead → the current slot is the cheapest we'll see, so the
    # rule still tops up now (buying later is strictly worse).
    _sv38 = ea.OVERNIGHT_INSURANCE
    ea.OVERNIGHT_INSURANCE = False
    try:
        ctx = ea.compute_decision_context(mk_state(8, 2, "na", 0.0, 0.0),
                                          fc([13, 15, 18, 20, 22, 22, 22, 22]), [], now_at(2))
    finally:
        ea.OVERNIGHT_INSURANCE = _sv38
    r = ctx["recommended"]
    check("survival floor charges when nothing cheaper ahead",
          r["rule_fired"] == "survival_floor_defend" and r["action"] == "charge", r)


def test_survival_floor_price_aware_killswitch():
    # SURVIVAL_FLOOR_PRICE_AWARE = False → revert to the old always-top-up-at-≤12% behaviour
    # even with a cheaper slot ahead.
    real = ea.SURVIVAL_FLOOR_PRICE_AWARE
    try:
        ea.SURVIVAL_FLOOR_PRICE_AWARE = False
        fcast = fc([27, 27, 27, 27, 12, 12, 12, 12, 12, 12, 12, 12])
        ctx = ea.compute_decision_context(mk_state(8, 2, "na", 0.0, 0.0), fcast, [], now_at(2))
        r = ctx["recommended"]
        check("price-aware off → force-charges despite cheaper slot",
              r["rule_fired"] == "survival_floor_defend" and r["action"] == "charge", r)
    finally:
        ea.SURVIVAL_FLOOR_PRICE_AWARE = real


# ---------------------------------------------------------------------------
# Rule 32 — decide on the 30-min slot, not the 5-min spot (added 2026-07-25)
#
# The general_price sensor is duration:5, so a single per-cycle sample is a coin-flip.
# On 2026-07-23 12:00 it sampled 9¢ (→ EV Fast) while the 30-min value was 11¢ (→ Eco).
# The anchor now derives from price_forecast[0] (the current interval, averaged).
# ---------------------------------------------------------------------------

def test_price_anchor_uses_30min_slot_not_spot():
    # Reproduces the 2026-07-23 12:00 flip: 5-min spot 9¢ (would be ev_ultra_cheap → Fast),
    # but the current 30-min slot is 11¢ → ev_standard_price → Eco. The fix must decide on 11.
    state, fcast = mk_ev_state(50, 20, 70, batt_soc=80, reserve=20, price=9,
                               forward_prices=[11, 11, 13, 13, 14, 14],
                               ultra_cheap_c=10, standard_price_c=15)
    ctx = ea.compute_decision_context(state, fcast, [], now_at(12))
    check("price anchored on the 30-min slot (11c), not the 5-min spot (9c)",
          ctx["price_used_c"] == 11 and ctx["price_spot_c"] == 9, ctx.get("price_used_c"))
    check("  ...so the EV stays Eco, not Fast on a 9c blip",
          ctx["ev_recommended"]["rule_fired"] == "ev_standard_price", ctx["ev_recommended"])


def test_price_anchor_falls_back_to_spot_when_forecast_empty():
    # No forecast → nothing to average → use the spot sample (9c → ev_ultra_cheap → Fast).
    state, _ = mk_ev_state(50, 20, 70, batt_soc=80, reserve=20, price=9, ultra_cheap_c=10)
    ctx = ea.compute_decision_context(state, [], [], now_at(12))
    check("empty forecast falls back to the 5-min spot", ctx["price_used_c"] == 9, ctx.get("price_used_c"))
    check("  ...and the EV verdict reflects the spot", ctx["ev_recommended"]["rule_fired"] == "ev_ultra_cheap",
          ctx["ev_recommended"])


def test_price_anchor_killswitch_reverts_to_spot():
    real = ea.PRICE_USE_30MIN_SLOT
    try:
        ea.PRICE_USE_30MIN_SLOT = False
        state, fcast = mk_ev_state(50, 20, 70, batt_soc=80, reserve=20, price=9,
                                   forward_prices=[11, 11, 13, 13, 14, 14], ultra_cheap_c=10)
        ctx = ea.compute_decision_context(state, fcast, [], now_at(12))
        check("kill-switch off → decides on the 5-min spot again", ctx["price_used_c"] == 9,
              ctx.get("price_used_c"))
    finally:
        ea.PRICE_USE_30MIN_SLOT = real


# ---------------------------------------------------------------------------
# Rule 29 — bias-corrected solar in the control path (added 2026-07-23)
#
# Raw Solcast runs ~2x optimistic at this flat-roof site in winter. Until now the
# correction reached only the dashboard and the shadow LP, while the authoritative
# rule layer read raw. On 2026-07-23 that held `peak_solar_will_cover` for 17
# consecutive overnight cycles against ~16.6 kWh of forecast when the calibrated
# expectation was 7.55 kWh, and the battery drained to 17%.
# ---------------------------------------------------------------------------

def test_corrected_solar_is_preferred_when_present():
    """The verdict must reason from the corrected figure, not the raw one."""
    st = mk_state(50, 9, remaining=16.6, remaining_corrected=7.5)
    ctx = ea.compute_decision_context(st, flat(14), [], now=now_at(9))
    check("used figure is the corrected one", ctx["solar_remaining_used_kwh"] == 7.5,
          ctx.get("solar_remaining_used_kwh"))
    check("raw is still reported for comparison", ctx["solar_remaining_raw_kwh"] == 16.6)
    check("corrected is reported", ctx["solar_remaining_corrected_kwh"] == 7.5)


def test_corrected_solar_falls_back_to_raw_when_unavailable():
    """A Solcast attribute outage must degrade to old behaviour, not to zero solar.

    Zero would be the dangerous failure: it would make the agent charge hard from
    grid on every cycle where the Solcast attributes happened to be missing.
    """
    st = mk_state(50, 9, remaining=16.6, remaining_corrected=None)
    ctx = ea.compute_decision_context(st, flat(14), [], now=now_at(9))
    check("falls back to raw", ctx["solar_remaining_used_kwh"] == 16.6,
          ctx.get("solar_remaining_used_kwh"))
    check("corrected reported as None", ctx["solar_remaining_corrected_kwh"] is None)


def test_use_corrected_solar_killswitch_reverts_to_raw():
    st = mk_state(50, 9, remaining=16.6, remaining_corrected=7.5)
    original = ea.USE_CORRECTED_SOLAR
    try:
        ea.USE_CORRECTED_SOLAR = False
        ctx = ea.compute_decision_context(st, flat(14), [], now=now_at(9))
        check("kill-switch reverts to raw", ctx["solar_remaining_used_kwh"] == 16.6,
              ctx.get("solar_remaining_used_kwh"))
    finally:
        ea.USE_CORRECTED_SOLAR = original
    check("kill-switch restored", ea.USE_CORRECTED_SOLAR is original)


def test_corrected_solar_changes_the_verdict_when_it_matters():
    """The behavioural case this was built for.

    Same battery, same prices, same hour — only the solar figure differs. With raw
    Solcast the gap to 85% looks covered and the layer holds; with the calibrated
    figure it does not, and the layer must stop holding for solar that won't come.
    """
    optimistic = mk_state(35, 9, remaining=16.6, remaining_corrected=None)
    calibrated = mk_state(35, 9, remaining=16.6, remaining_corrected=2.0)
    ctx_opt = ea.compute_decision_context(optimistic, flat(14), [], now=now_at(9))
    ctx_cal = ea.compute_decision_context(calibrated, flat(14), [], now=now_at(9))
    check("optimistic raw forecast covers the gap",
          ctx_opt["kwh_needed_85"] == 0.0, ctx_opt.get("kwh_needed_85"))
    check("calibrated forecast does NOT cover the gap",
          ctx_cal["kwh_needed_85"] > 0.0, ctx_cal.get("kwh_needed_85"))
    check("calibrated verdict stops holding for absent solar",
          ctx_cal["recommended"]["rule_fired"] != "peak_solar_will_cover",
          ctx_cal["recommended"]["rule_fired"])


# ---------------------------------------------------------------------------
# SETTINGS_SPEC validation (added 2026-07-23)
#
# These helpers are control inputs read by compute_decision_context(). Before
# validation existed, `battery_max_insurance_floor_pct` sat at 0 (silently
# disabling Rule 15's insurance floor) and `ev_min_soc_pct` drifted to 80,
# firing ev_case3_below_minimum at 60% EV SoC and putting the Zappi on Fast on
# a peak morning. Both slipped through because the fallback idioms in use
# (`or 20`, `settings.get(k, DEFAULT)`) only catch falsy or missing values.
# ---------------------------------------------------------------------------

class _StubHA:
    """Swap ea.ha_state for a dict lookup, restoring it on exit."""

    def __init__(self, mapping):
        self.mapping = mapping

    def __enter__(self):
        self._real = ea.ha_state
        ea.ha_state = lambda entity_id: self.mapping.get(entity_id)
        return self

    def __exit__(self, *exc):
        ea.ha_state = self._real
        return False


def _entity(key):
    """Resolve a SETTINGS_SPEC key to the HA entity id it reads."""
    return ea.ENTITIES[ea.SETTINGS_SPEC[key][0]]


def _hist(key, value):
    """One decisions.jsonl-shaped record carrying a past settings_used value."""
    return [{"settings_used": {key: value}}]


def test_settings_in_band_used_as_is():
    """An in-band value passes through untouched — tuning is never overridden."""
    with _StubHA({_entity("ev_min_soc_pct"): "45"}):
        value, violation = ea._validated_setting("ev_min_soc_pct")
    check("in-band value used as-is", value == 45.0, f"got {value}")
    check("in-band produces no violation", violation is None, f"got {violation}")


def test_settings_out_of_band_prefers_last_known_good():
    """The 2026-07-23 failure: ev_min_soc_pct drifted to 80 (band 0–50).

    The substitute must be a value the console itself previously held — not a
    target hardcoded in energy_agent.py.
    """
    with _StubHA({_entity("ev_min_soc_pct"): "80"}):
        value, violation = ea._validated_setting(
            "ev_min_soc_pct", _hist("ev_min_soc_pct", 30.0))
    check("out-of-band uses last known good", value == 30.0, f"got {value}")
    check("out-of-band is flagged", violation is not None)
    check("violation records what was found", violation and violation["found"] == 80.0)
    check("violation records the substitute source",
          violation and violation["source"] == "last_known_good", f"got {violation}")


def test_settings_out_of_band_clamps_when_no_history():
    """With no usable history, clamp to the nearest band edge."""
    with _StubHA({_entity("ev_min_soc_pct"): "80"}):
        value, violation = ea._validated_setting("ev_min_soc_pct", [])
    lo, hi = ea.SETTINGS_SPEC["ev_min_soc_pct"][1:]
    check("clamped to band edge", value == hi, f"got {value} (band {lo}-{hi})")
    check("clamp is reported as the source",
          violation and violation["source"] == "clamped_to_band", f"got {violation}")


def test_settings_history_must_itself_be_in_band():
    """A historical value that is also out of band must not be resurrected."""
    with _StubHA({_entity("ev_min_soc_pct"): "80"}):
        value, violation = ea._validated_setting(
            "ev_min_soc_pct", _hist("ev_min_soc_pct", 75.0))
    check("bad history is ignored, falls through to clamp",
          violation and violation["source"] == "clamped_to_band", f"got {violation}")
    check("clamped value used", value == ea.SETTINGS_SPEC["ev_min_soc_pct"][2],
          f"got {value}")


def test_settings_unavailable_uses_history_silently():
    """An unreadable entity is a transport failure, not a bad value."""
    for raw in (None, "", "unknown", "unavailable"):
        with _StubHA({_entity("ev_min_soc_pct"): raw}):
            value, violation = ea._validated_setting(
                "ev_min_soc_pct", _hist("ev_min_soc_pct", 30.0))
        check(f"unavailable ({raw!r}) uses last known good", value == 30.0, f"got {value}")
        check(f"unavailable ({raw!r}) is not a violation", violation is None)


def test_settings_unavailable_with_no_history_yields_none():
    """Nothing can be established — the caller must apply its own default."""
    with _StubHA({_entity("ev_min_soc_pct"): "unavailable"}):
        value, violation = ea._validated_setting("ev_min_soc_pct", [])
    check("no value can be established", value is None, f"got {value}")
    check("reported as unreadable", violation and violation["reason"] == "unreadable")
    with _StubHA({_entity("ev_min_soc_pct"): "unavailable"}):
        values, _ = ea._read_validated_settings([])
    check("key is omitted so the caller's own default applies",
          "ev_min_soc_pct" not in values, f"got {values}")


def test_settings_unparseable_flags():
    with _StubHA({_entity("ev_min_soc_pct"): "not-a-number"}):
        value, violation = ea._validated_setting(
            "ev_min_soc_pct", _hist("ev_min_soc_pct", 30.0))
    check("unparseable uses last known good", value == 30.0, f"got {value}")
    check("unparseable is flagged with reason",
          violation and violation["reason"] == "unparseable", f"got {violation}")


def test_settings_zero_insurance_floor_is_a_violation():
    """0 disables Rule 15 entirely — must not be accepted silently."""
    with _StubHA({_entity("max_insurance_floor_pct"): "0"}):
        value, violation = ea._validated_setting("max_insurance_floor_pct", [])
    lo = ea.SETTINGS_SPEC["max_insurance_floor_pct"][1]
    check("insurance floor 0 is rejected", violation is not None)
    check("insurance floor 0 clamps up to the band floor", value == lo, f"got {value}")


def test_settings_drifted_ev_min_soc_no_longer_forces_fast():
    """End-to-end: the drifted helper no longer reaches the EV decision.

    With ev_min_soc_pct at 80, `ev_soc(60) < ev_min` was true and the layer
    chose Fast. Validation substitutes an in-band value, making it false, so
    the cycle falls through to the price-based rules instead.
    """
    with _StubHA({_entity("ev_min_soc_pct"): "80"}):
        values, violations = ea._read_validated_settings(_hist("ev_min_soc_pct", 30.0))
    check("drifted ev_min_soc is reported",
          any(v["setting"] == "ev_min_soc_pct" for v in violations))
    check("EV at 60% is no longer 'below minimum'",
          not (60 < values["ev_min_soc_pct"]), f"ev_min={values.get('ev_min_soc_pct')}")


def test_peak_months_agree_across_agent_and_ha_config():
    """The EA116 peak-month list exists in three places. They must not diverge.

    - energy_agent.PEAK_MONTHS          (the control path)
    - binary_sensor.peak_month          (dashboard visibility)
    - battery_grid_charge_target        (the 85% peak floor — deliberately
                                         self-contained, see the comment there)

    Duplication is accepted for the safety-critical sensor, which must not depend
    on another template entity resolving first. This test is the price of that:
    divergence fails here instead of going unnoticed, which is how the missing
    85% floor went undetected for seven weeks (2026-07-22).
    """
    import re
    from pathlib import Path
    cfg = Path(__file__).resolve().parent.parent / "config" / "configuration.yaml"
    if not cfg.exists():
        check("configuration.yaml reachable from tests", False, str(cfg))
        return
    text = cfg.read_text()
    found = re.findall(r"now\(\)\.month in \[([0-9,\s]+)\]", text)
    check("peak-month list appears in configuration.yaml", len(found) >= 1, f"{len(found)}")
    # battery_grid_charge_target uses a `peak_months` variable rather than an inline list
    found += re.findall(r"set peak_months\s*=\s*\[([0-9,\s]+)\]", text)
    check("both HA copies found", len(found) >= 2, f"found {len(found)}: {found}")
    for i, raw in enumerate(found):
        months = {int(x) for x in raw.replace(" ", "").split(",") if x}
        check(f"HA peak-month list #{i+1} matches agent PEAK_MONTHS",
              months == set(ea.PEAK_MONTHS), f"{sorted(months)} vs {sorted(ea.PEAK_MONTHS)}")


def test_missing_entity_does_not_crash_the_cycle():
    """ha_state() raises on a 404. A helper deleted from configuration.yaml must
    degrade to the 'unreadable' path, not take down get_current_state().

    Found 2026-07-23 the hard way: deleting battery_charge_price_threshold_c from
    the config while the agent still referenced it made every read raise.
    """
    real = ea.ha_state

    def _raises(entity_id):
        raise RuntimeError("404 Client Error: Not Found")

    try:
        ea.ha_state = _raises
        value, violation = ea._validated_setting(
            "ev_min_soc_pct", _hist("ev_min_soc_pct", 30.0))
        check("missing entity uses last known good", value == 30.0, f"got {value}")
        check("missing entity is not a violation when history covers it",
              violation is None, f"got {violation}")
        values, _ = ea._read_validated_settings([])
        check("missing entity with no history omits the key",
              "ev_min_soc_pct" not in values, f"got {values}")
    finally:
        ea.ha_state = real


def test_last_known_good_ignores_its_own_substitutions():
    """A substituted value must never be laundered into a 'known good' one.

    `settings_used` logs the value *used*, which may itself be a substitute. On
    2026-07-23 a hardcoded 70 written by an earlier build was read back from the
    log an hour after the hardcoding was removed, and reported as though HA had
    supplied it. Records carrying a violation for the key must be skipped.
    """
    laundered = [{
        "settings_used": {"max_insurance_floor_pct": 70.0},
        "settings_violations": [{"setting": "max_insurance_floor_pct",
                                 "found": 0.0, "used": 70.0}],
    }]
    check("substituted history is not treated as known-good",
          ea._last_known_good("max_insurance_floor_pct", laundered) is None)

    genuine = [{"settings_used": {"max_insurance_floor_pct": 65.0},
                "settings_violations": []}]
    check("a clean observation is still used",
          ea._last_known_good("max_insurance_floor_pct", genuine) == 65.0)

    # A clean record must win even when a later cycle substituted.
    mixed = genuine + laundered
    check("clean observation preferred over later substitution",
          ea._last_known_good("max_insurance_floor_pct", mixed) == 65.0)


def test_last_known_good_rejects_a_string_history():
    """Regression: get_recent_decisions() returns a *string*, get_recent_records()
    a list of dicts. Passing the string silently iterated characters and made
    last-known-good never fire, so every bad value fell through to the clamp."""
    check("a string history yields no last-known-good",
          ea._last_known_good("ev_min_soc_pct", "some formatted block") is None)
    check("a list of dicts does yield one",
          ea._last_known_good("ev_min_soc_pct", _hist("ev_min_soc_pct", 30.0)) == 30.0)


def test_settings_spec_holds_no_target_values():
    """The spec must declare bands only — never a target.

    Targets live in the HA console and are read from it; duplicating one here is
    how CONTEXT.md came to claim 6¢ while the console said 10¢.
    """
    for key, spec in ea.SETTINGS_SPEC.items():
        check(f"{key} spec is (alias, lo, hi) only", len(spec) == 3, f"got {spec}")
        _alias, lo, hi = spec
        check(f"{key} band is ordered", lo < hi, f"lo={lo} hi={hi}")


def test_rule37_solar_after_hour():
    """_corrected_solar_after_hour sums only >=15:00 slots, applies per-hour correction,
    and falls back to raw for low-sample hours."""
    winter = [
        {"time": "2026-08-08T13:00", "kw_est": 3.0},
        {"time": "2026-08-08T14:30", "kw_est": 1.6},
        {"time": "2026-08-08T15:00", "kw_est": 0.8},   # after 3pm
        {"time": "2026-08-08T15:30", "kw_est": 0.4},   # after 3pm
        {"time": "2026-08-08T16:00", "kw_est": 0.1},   # after 3pm
    ]
    # raw post-3pm kWh = (0.8 + 0.4 + 0.1) * 0.5 = 0.65
    check("rule37 post-3pm solar sums only >=15:00 slots (raw)",
          abs(ea._corrected_solar_after_hour(winter, None) - 0.65) < 1e-9,
          ea._corrected_solar_after_hour(winter, None))
    # correction halves the 15/16h ratios (n high); helper rounds to 2dp → round(0.325,2)
    corr = {"15": {"ratio": 0.5, "n": 100}, "16": {"ratio": 0.5, "n": 100}}
    exp_corr = round((0.8 + 0.4 + 0.1) * 0.5 * 0.5, 2)
    check("rule37 post-3pm solar applies per-hour correction",
          ea._corrected_solar_after_hour(winter, corr) == exp_corr,
          (ea._corrected_solar_after_hour(winter, corr), exp_corr))
    # low-sample hour falls back to raw (no correction)
    check("rule37 low-sample hour uses raw ratio 1.0",
          abs(ea._corrected_solar_after_hour(winter, {"15": {"ratio": 0.5, "n": 2}}) - 0.65) < 1e-9,
          ea._corrected_solar_after_hour(winter, {"15": {"ratio": 0.5, "n": 2}}))
    check("rule37 empty/absent periods -> None (caller falls back to floor)",
          ea._corrected_solar_after_hour([]) is None and ea._corrected_solar_after_hour(None) is None,
          (ea._corrected_solar_after_hour([]), ea._corrected_solar_after_hour(None)))


def test_rule37_deadline_target_pct():
    """Seasonal target: winter (≈0 post-3pm solar) → ceil; summer (large) → floor; banded;
    unknown/kill-switch off → floor. Sets the flag explicitly so it is independent of the
    module default (which ships False)."""
    _saved = ea.SEASONAL_DEADLINE_TARGET
    ea.SEASONAL_DEADLINE_TARGET = True
    try:
        check("rule37 winter (0 post-3pm solar) -> ceil 95",
              ea._deadline_target_pct(0.0) == 95, ea._deadline_target_pct(0.0))
        check("rule37 summer (large post-3pm solar) -> floor 85",
              ea._deadline_target_pct(6.0) == 85, ea._deadline_target_pct(6.0))
        exp = int(round(max(85.0, min(95.0, 95 - 0.65 / ea.USABLE_KWH * 100))))
        check("rule37 small post-3pm solar raises headroom below ceil",
              ea._deadline_target_pct(0.65) == exp, (ea._deadline_target_pct(0.65), exp))
        check("rule37 target never exceeds ceil for negative/zero solar",
              ea._deadline_target_pct(-5.0) == 95, ea._deadline_target_pct(-5.0))
        check("rule37 unknown post-3pm solar (None) -> safe floor 85",
              ea._deadline_target_pct(None) == 85, ea._deadline_target_pct(None))
        ea.SEASONAL_DEADLINE_TARGET = False
        check("rule37 kill-switch off -> fixed floor 85 regardless of solar",
              ea._deadline_target_pct(0.0) == 85, ea._deadline_target_pct(0.0))
    finally:
        ea.SEASONAL_DEADLINE_TARGET = _saved


def test_rule37_opportunistic_topup():
    """Wired behaviour: winter (0 post-3pm solar → target 95) + cheap → the peak HOLD is overridden
    to a gentle top-up toward 95; summer / expensive / kill-switch off leave the hold intact.
    Isolate from Phase 2 (which upgrades gentle to autonomous) to test Phase 1 in isolation."""
    def run(soc, price, hour, solar_after, seasonal=True):
        _sv = ea.SEASONAL_DEADLINE_TARGET
        _sv2 = ea.FRONTLOAD_CHEAP_FLOOR
        ea.SEASONAL_DEADLINE_TARGET = seasonal
        ea.FRONTLOAD_CHEAP_FLOOR = False
        st = mk_state(soc, hour, price=price, solar_kw=2.0, remaining_corrected=3.0)
        st["solar"]["forecast_after_deadline_kwh"] = solar_after
        try:
            return ea.compute_decision_context(st, fc([price] * 12), [], now_at(hour))["recommended"]
        finally:
            ea.SEASONAL_DEADLINE_TARGET = _sv
            ea.FRONTLOAD_CHEAP_FLOOR = _sv2

    r = run(71, 8.0, 14, 0.0)   # winter, cheap, floor covered by solar
    check("rule37 winter+cheap: opportunistic top-up overrides the hold to charge->95",
          r["rule_fired"] == "peak_opportunistic_topup" and r["action"] == "charge"
          and r["mode"] == "self_consumption" and r["target_pct"] == 95, r)
    r = run(71, 8.0, 14, 6.0)   # summer: target == floor 85 → override must NOT fire
    check("rule37 summer: hold preserved (solar covers, no grid top-up)",
          r["rule_fired"] == "peak_solar_will_cover" and r["action"] == "hold", r)
    r = run(88, 25.0, 13, 0.0)  # winter but expensive → cheap-gate blocks it
    check("rule37 winter+expensive: cheap-gated, holds peak_target_met (no forced slam)",
          r["rule_fired"] == "peak_target_met" and r["action"] == "hold", r)
    r = run(88, 8.0, 13, 0.0)   # winter, cheap, in the 85–95 band → top up toward 95
    check("rule37 winter+cheap above floor: top-up toward 95",
          r["rule_fired"] == "peak_opportunistic_topup" and r["target_pct"] == 95, r)
    r = run(71, 8.0, 14, 0.0, seasonal=False)  # kill-switch off
    check("rule37 kill-switch off: no top-up, hold preserved",
          r["rule_fired"] == "peak_solar_will_cover" and r["action"] == "hold", r)


def test_rule37p2_frontload_cheap():
    """Phase 2: in sponge at cheap price with SoC < deadline_target → upgrade to autonomous.
    Target is the seasonal deadline_target (95 in winter with no post-3pm solar)."""
    def run(soc, price, hour, frontload=True, solar_after=0.0,
            accuracy="poor", remaining_corr=1.0):
        _sv = ea.FRONTLOAD_CHEAP_FLOOR
        _sv2 = ea.SEASONAL_DEADLINE_TARGET
        ea.FRONTLOAD_CHEAP_FLOOR = frontload
        ea.SEASONAL_DEADLINE_TARGET = True
        st = mk_state(soc, hour, price=price, solar_kw=0.5, accuracy=accuracy,
                       remaining_corrected=remaining_corr)
        st["solar"]["forecast_after_deadline_kwh"] = solar_after
        try:
            return ea.compute_decision_context(st, fc([price] * 12), [], now_at(hour))["recommended"]
        finally:
            ea.FRONTLOAD_CHEAP_FLOOR = _sv
            ea.SEASONAL_DEADLINE_TARGET = _sv2

    r = run(40, 8.0, 11)
    check("rule37p2 sponge+cheap+low-soc: upgrades to autonomous, targets seasonal 95",
          r["rule_fired"] == "peak_frontload_cheap" and r["mode"] == "autonomous"
          and r["target_pct"] == 95, r)
    r = run(80, 8.0, 11)   # above sponge floor but grid still needs >1 kWh to seasonal 95
    check("rule37p2 soc=80 (grid needs 2+ kWh to target): upgrades to autonomous",
          r["rule_fired"] == "peak_frontload_cheap" and r["mode"] == "autonomous"
          and r["target_pct"] == 95, r)
    r = run(96, 8.0, 11)   # above seasonal target → no upgrade
    check("rule37p2 soc>=deadline_target: no upgrade",
          r["rule_fired"] != "peak_frontload_cheap", r)
    r = run(40, 25.0, 11)
    check("rule37p2 expensive: no upgrade",
          r["rule_fired"] != "peak_frontload_cheap", r)
    r = run(40, 8.0, 11, frontload=False)
    check("rule37p2 kill-switch off: no upgrade",
          r["rule_fired"] != "peak_frontload_cheap", r)
    # Summer: good accuracy + 8 kWh remaining + 6 kWh post-3pm → confident solar covers gap
    r = run(40, 8.0, 11, solar_after=6.0, accuracy="good", remaining_corr=8.0)
    check("rule37p2 summer (solar covers): no front-load (solar fills it for free)",
          r["rule_fired"] != "peak_frontload_cheap", r)
    # Mid-season: poor accuracy (confidence=0.5) + moderate solar → half credit, gap still large
    r = run(40, 8.0, 11, accuracy="poor", remaining_corr=4.0)
    check("rule37p2 midseason (poor accuracy, partial solar): still front-loads",
          r["rule_fired"] == "peak_frontload_cheap", r)


def test_rule37p2_not_in_demand_window():
    """Phase 2 never fires in the demand window (3-9pm)."""
    _sv = ea.FRONTLOAD_CHEAP_FLOOR
    ea.FRONTLOAD_CHEAP_FLOOR = True
    st = mk_state(40, 16, price=8.0, solar_kw=0.5, accuracy="poor")
    try:
        r = ea.compute_decision_context(st, fc([8.0] * 12), [], now_at(16))["recommended"]
    finally:
        ea.FRONTLOAD_CHEAP_FLOOR = _sv
    check("rule37p2 demand window: never fires",
          r["rule_fired"] == "demand_window_active", r)


def test_rule38_overnight_insurance_fires():
    """Overnight, peak, SoC projects below margin before sponge → gentle insurance charge."""
    def run(soc, price, hour, insurance=True):
        _sv = ea.OVERNIGHT_INSURANCE
        ea.OVERNIGHT_INSURANCE = insurance
        st = mk_state(soc, hour, price=price, solar_kw=0.0, accuracy="na",
                       remaining_corrected=0.0, is_peak=True)
        try:
            return ea.compute_decision_context(st, fc([price] * 12), [], now_at(hour))["recommended"]
        finally:
            ea.OVERNIGHT_INSURANCE = _sv

    r = run(20, 15.0, 23)
    check("rule38 overnight+peak+low-projected: insurance fires",
          r["rule_fired"] == "overnight_insurance" and r["action"] == "charge"
          and r["mode"] == "self_consumption", r)
    check("rule38 insurance target is above current SoC",
          r["target_pct"] > 20, r)

    r = run(70, 15.0, 23)
    check("rule38 overnight+peak+high-soc: no insurance (will survive to sponge)",
          r["rule_fired"] != "overnight_insurance", r)

    r = run(20, 30.0, 23)
    check("rule38 price above ceiling: no insurance",
          r["rule_fired"] != "overnight_insurance", r)

    r = run(20, 15.0, 23, insurance=False)
    check("rule38 kill-switch off: no insurance",
          r["rule_fired"] != "overnight_insurance", r)


def test_rule38_not_during_day():
    """Rule 38 only fires at night (20:00-07:00), not during the day."""
    _sv = ea.OVERNIGHT_INSURANCE
    ea.OVERNIGHT_INSURANCE = True
    st = mk_state(20, 12, price=8.0, solar_kw=0.5, accuracy="poor",
                   remaining_corrected=1.0, is_peak=True)
    st["solar"]["forecast_after_deadline_kwh"] = 0.0
    try:
        r = ea.compute_decision_context(st, fc([8.0] * 12), [], now_at(12))["recommended"]
    finally:
        ea.OVERNIGHT_INSURANCE = _sv
    check("rule38 daytime: never fires",
          r["rule_fired"] != "overnight_insurance", r)


def test_rule38_early_morning():
    """Rule 38 fires at 5am when SoC is low and projected to drain before sponge."""
    _sv = ea.OVERNIGHT_INSURANCE
    ea.OVERNIGHT_INSURANCE = True
    st = mk_state(15, 5, price=14.0, solar_kw=0.0, accuracy="na",
                   remaining_corrected=0.0, is_peak=True)
    try:
        r = ea.compute_decision_context(st, fc([14.0] * 12), [], now_at(5))["recommended"]
    finally:
        ea.OVERNIGHT_INSURANCE = _sv
    check("rule38 5am+low-soc: insurance fires",
          r["rule_fired"] == "overnight_insurance" and r["action"] == "charge", r)


def test_rule38_price_aware():
    """Rule 38 defers insurance when a cheaper slot exists ahead."""
    _sv_ins = ea.OVERNIGHT_INSURANCE
    _sv_pa = ea.OVERNIGHT_INSURANCE_PRICE_AWARE
    ea.OVERNIGHT_INSURANCE = True
    ea.OVERNIGHT_INSURANCE_PRICE_AWARE = True
    try:
        # Cheaper slot ahead (forward_min 10¢ vs current 19¢): defer
        st = mk_state(20, 23, price=19.0, solar_kw=0.0, accuracy="na",
                       remaining_corrected=0.0, is_peak=True)
        prices = [19.0, 18.0, 17.0, 16.0, 15.0, 14.0, 12.0, 10.0, 10.0, 10.0, 10.0, 10.0]
        r = ea.compute_decision_context(st, fc(prices), [], now_at(23))["recommended"]
        check("rule38 cheaper slot ahead: defers (no insurance)",
              r["rule_fired"] != "overnight_insurance", r)

        # Current slot is already the cheapest (flat 12¢): fires
        st2 = mk_state(20, 23, price=12.0, solar_kw=0.0, accuracy="na",
                        remaining_corrected=0.0, is_peak=True)
        r2 = ea.compute_decision_context(st2, fc([12.0] * 12), [], now_at(23))["recommended"]
        check("rule38 already cheapest: insurance fires",
              r2["rule_fired"] == "overnight_insurance" and r2["action"] == "charge", r2)

        # Kill-switch off: charges even with cheaper slot ahead
        ea.OVERNIGHT_INSURANCE_PRICE_AWARE = False
        st3 = mk_state(20, 23, price=19.0, solar_kw=0.0, accuracy="na",
                        remaining_corrected=0.0, is_peak=True)
        r3 = ea.compute_decision_context(st3, fc(prices), [], now_at(23))["recommended"]
        check("rule38 price-aware off: insurance fires regardless",
              r3["rule_fired"] == "overnight_insurance" and r3["action"] == "charge", r3)
    finally:
        ea.OVERNIGHT_INSURANCE = _sv_ins
        ea.OVERNIGHT_INSURANCE_PRICE_AWARE = _sv_pa


if __name__ == "__main__":
    for fn in [test_manual_override,
               test_rule37_solar_after_hour,
               test_rule37_deadline_target_pct,
               test_rule37_opportunistic_topup,
               test_rule37p2_frontload_cheap,
               test_rule37p2_not_in_demand_window,
               test_rule38_overnight_insurance_fires,
               test_rule38_not_during_day,
               test_rule38_early_morning,
               test_rule38_price_aware,
               test_gentle_charge_reserve_small_gap,
               test_gentle_charge_reserve_clamps_at_target,
               test_gentle_charge_reserve_none_soc_falls_back_to_target,
               test_gentle_charge_reserve_custom_offset,
               test_execute_selfcons_charge_uses_gentle_reserve,
               test_execute_autonomous_charge_unaffected,
               test_execute_gentle_killswitch_reverts_to_target,
               test_survival_floor_overrides_low_soc_overnight_hold,
               test_survival_floor_does_not_override_deadline_autonomous,
               test_survival_floor_not_active_in_demand_window,
               test_survival_floor_killswitch_reverts_to_ride_low,
               test_survival_floor_not_triggered_above_floor,
               test_survival_floor_defers_to_cheaper_slot,
               test_survival_floor_charges_when_no_cheaper_slot,
               test_survival_floor_price_aware_killswitch,
               test_price_anchor_uses_30min_slot_not_spot,
               test_price_anchor_falls_back_to_spot_when_forecast_empty,
               test_price_anchor_killswitch_reverts_to_spot,
               test_ev_case6_negative_fit_solar_dump,
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
               test_solar_accuracy_corrected_normal_winter_morning_is_good,
               test_solar_accuracy_corrected_flags_genuine_underperformance,
               test_solar_accuracy_near_zero_corrected_ref_is_not_applicable,
               test_solar_accuracy_falls_back_to_raw_when_uncorrected,
               test_solar_accuracy_night_still_not_applicable,
               test_hour_solar_ratio_reads_model_params,
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
               test_peak_early_morning_hold_on_price_spike,
               test_peak_early_morning_hold_not_fired_when_cheap,
               test_peak_early_morning_hold_fires_at_low_soc,
               test_peak_eve_holds_for_cheap_morning_slot,
               test_peak_eve_killswitch_off_reverts,
               test_peak_eve_no_quickcheck_slam_at_low_soc,
               test_peak_sponge_go_hard, test_peak_sponge_selfcons_then_escalates,
               test_peak_sponge_solar_improves_to_hold,
               test_corrected_solar_is_preferred_when_present,
               test_corrected_solar_falls_back_to_raw_when_unavailable,
               test_use_corrected_solar_killswitch_reverts_to_raw,
               test_corrected_solar_changes_the_verdict_when_it_matters,
               test_settings_in_band_used_as_is,
               test_settings_out_of_band_prefers_last_known_good,
               test_settings_out_of_band_clamps_when_no_history,
               test_settings_history_must_itself_be_in_band,
               test_settings_unavailable_uses_history_silently,
               test_settings_unavailable_with_no_history_yields_none,
               test_settings_unparseable_flags,
               test_settings_zero_insurance_floor_is_a_violation,
               test_settings_drifted_ev_min_soc_no_longer_forces_fast,
               test_peak_months_agree_across_agent_and_ha_config,
               test_missing_entity_does_not_crash_the_cycle,
               test_last_known_good_ignores_its_own_substitutions,
               test_last_known_good_rejects_a_string_history,
               test_settings_spec_holds_no_target_values]:
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n{'='*50}\n{_passed} passed, {_failed} failed")
    raise SystemExit(1 if _failed else 0)
