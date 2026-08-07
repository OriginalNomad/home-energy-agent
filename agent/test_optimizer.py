"""Unit tests for the LP optimiser (agent/optimizer.py).

Pure, no API calls — run with: python3 agent/test_optimizer.py
Each scenario asserts the verdict the optimiser SHOULD reach, given prices/solar/SoC.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from optimizer import optimize_battery, OptParams

TZ = ZoneInfo("Australia/Sydney")


def _prices(seq, start="2026-06-01 10:00"):
    """Build a 30-min price forecast from a list of ¢ values."""
    base = datetime.fromisoformat(start)
    out = []
    for i, c in enumerate(seq):
        t = base.replace(minute=0 if (base.minute + 30 * i) % 60 == 0 else 30,
                         hour=base.hour + (base.minute + 30 * i) // 60)
        out.append({"time": t.strftime("%Y-%m-%d %H:%M"), "cents_kwh": float(c),
                    "descriptor": ""})
    return out


def _flat_solar(pf, kw=0.0):
    return [{"time": f["time"], "kw_est": kw} for f in pf]


passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}  {detail}")


now = datetime(2026, 6, 1, 10, 0, tzinfo=TZ)


# 1. Cheap now, expensive later, half-empty battery, no solar → should grid-charge.
pf = _prices([6, 6, 6, 7, 18, 19, 20, 21, 20, 19, 18, 17])
state = {"soc_pct": 40, "is_peak_month": False, "home_load_kw": 0.5}
r = optimize_battery(state, pf, _flat_solar(pf), now)
check("cheap-now-expensive-later charges", r["verdict"]["action"] == "charge", r["verdict"])
check("  ...sources from grid", (r.get("grid_charge_now_kw") or 0) > 0.3, r.get("grid_charge_now_kw"))

# 2. Flat prices, battery already full → hold (no arbitrage, no need).
pf = _prices([15, 15, 15, 15, 15, 15, 15, 15])
state = {"soc_pct": 95, "is_peak_month": False, "home_load_kw": 0.5}
r = optimize_battery(state, pf, _flat_solar(pf), now)
check("flat prices + full battery holds", r["verdict"]["action"] == "hold", r["verdict"])

# 3. Negative price → charge hard (autonomous).
pf = _prices([-5, -4, 2, 8, 12, 15, 18, 20])
state = {"soc_pct": 30, "is_peak_month": False, "home_load_kw": 0.5}
r = optimize_battery(state, pf, _flat_solar(pf), now)
check("negative price charges", r["verdict"]["action"] == "charge", r["verdict"])
check("  ...goes autonomous (fast)", r["verdict"]["mode"] == "autonomous", r["verdict"])

# 4. Peak month, low SoC late-morning, demand window in horizon, no solar →
#    must pre-charge from cheap grid now (penalty on 3-9pm import forces it).
now_pk = datetime(2026, 6, 1, 11, 0, tzinfo=TZ)
pf = _prices([8, 8, 9, 10, 11, 12, 13, 25, 28, 30, 28, 26], start="2026-06-01 11:00")
#            11:00 ........................ 15:00 demand window 25¢+ ........
state = {"soc_pct": 25, "is_peak_month": True, "home_load_kw": 1.0}
r = optimize_battery(state, pf, _flat_solar(pf), now_pk)
check("peak-month pre-charges before demand window",
      r["verdict"]["action"] == "charge", r["verdict"])
traj = r.get("soc_trajectory_pct") or []
# the plan should lift SoC meaningfully before the 15:00 slot (index 8)
check("  ...lifts SoC before 3pm",
      len(traj) > 8 and traj[7] > 25 + 10, traj)

# 5. Sunny day, prices flat, low SoC → solar fills battery, no grid intent → hold.
pf = _prices([14, 14, 14, 14, 14, 14, 14, 14])
state = {"soc_pct": 45, "is_peak_month": False, "home_load_kw": 0.4}
solar = [{"time": f["time"], "kw_est": 4.0} for f in pf]   # strong solar surplus
r = optimize_battery(state, pf, solar, now)
check("sunny + flat price holds (solar-only fill)",
      r["verdict"]["action"] == "hold", r["verdict"])

# 6. Risk knob: conservative planning charges at least as much as neutral.
pf = _prices([9, 9, 10, 11, 16, 17, 18, 19])
state = {"soc_pct": 50, "is_peak_month": False, "home_load_kw": 0.6}
r_neutral = optimize_battery(state, pf, _flat_solar(pf, 1.0), now, OptParams(risk=0.0))
r_conserv = optimize_battery(state, pf, _flat_solar(pf, 1.0), now, OptParams(risk=0.6))
check("risk knob never reduces protective charging",
      (r_conserv.get("grid_charge_now_kw") or 0) >= (r_neutral.get("grid_charge_now_kw") or 0) - 1e-6,
      (r_neutral.get("grid_charge_now_kw"), r_conserv.get("grid_charge_now_kw")))



# 7. Horizon extension: cloudy peak morning, zero solar.
#    Short horizon (no demand window) → LP doesn't see penalty → may hold.
#    Extended horizon including demand window slots → LP pre-charges.
def _prices_ext(base_seq, extra_h_prices, start="2026-06-03 09:00"):
    """base_seq: 30-min prices; extra_h_prices: {hour: price} synthetic extension."""
    base = datetime.fromisoformat(start)
    out = []
    for i, c in enumerate(base_seq):
        mins = 30 * i
        t = base.replace(hour=base.hour + (base.minute + mins) // 60,
                         minute=(base.minute + mins) % 60)
        out.append({"time": t.strftime("%Y-%m-%d %H:%M"), "cents_kwh": float(c),
                    "descriptor": ""})
    # Append synthetic evening slots at specified hours
    last_t = datetime.fromisoformat(out[-1]["time"])
    slot = last_t.replace(minute=(last_t.minute + 30) % 60,
                          hour=last_t.hour + (last_t.minute + 30) // 60)
    for h, price in sorted(extra_h_prices.items()):
        while slot.hour < h:
            slot = slot.replace(hour=slot.hour + 1) if slot.minute == 0 else \
                   slot.replace(minute=0, hour=slot.hour + 1)
        for _ in range(2):  # two 30-min slots per hour
            out.append({"time": slot.strftime("%Y-%m-%d %H:%M"),
                        "cents_kwh": float(price), "descriptor": "synthetic_historical"})
            slot = slot.replace(minute=30 if slot.minute == 0 else 0,
                                hour=slot.hour if slot.minute == 0 else slot.hour + 1)
    return out

now_am = datetime(2026, 6, 3, 9, 0, tzinfo=TZ)
state_cloudy_peak = {"soc_pct": 30, "is_peak_month": True, "home_load_kw": 1.0}
zero_solar = _flat_solar([], 0.0)

# Short ~6h horizon ending at 15:00 — demand window starts right at the edge
short_pf = _prices([9, 9, 10, 10, 11, 12, 13, 14, 15, 16, 17, 18],
                   start="2026-06-03 09:00")
# time 09:00-14:30 — demand window (15:00+) NOT in horizon
short_solar = [{"time": f["time"], "kw_est": 0.0} for f in short_pf]
r_short = optimize_battery(state_cloudy_peak, short_pf, short_solar, now_am)

# Extended horizon: same base + synthetic 15:00–21:00 slots with p75-style price
ext_pf = list(short_pf) + [
    {"time": f"2026-06-03 {h:02d}:{m:02d}", "cents_kwh": 25.0, "descriptor": "synthetic_historical"}
    for h in range(15, 21) for m in [0, 30]
]
ext_solar = [{"time": f["time"], "kw_est": 0.0} for f in ext_pf]
r_ext = optimize_battery(state_cloudy_peak, ext_pf, ext_solar, now_am)

check("cloudy peak short horizon — LP may hold (no demand window visible)",
      r_short["verdict"]["action"] in ("hold", "charge"), r_short["verdict"])
check("cloudy peak extended horizon — LP pre-charges (demand window visible)",
      r_ext["verdict"]["action"] == "charge", r_ext["verdict"])
traj_ext = r_ext.get("soc_trajectory_pct") or []
check("  ...extended horizon lifts SoC meaningfully",
      len(traj_ext) > 12 and traj_ext[11] > 30 + 10, traj_ext[:14])

# 13. Regression — SoC must never silently default.
# From 2026-06-01 to 2026-07-22 optimize_battery() read
#   state.get("soc_pct", state.get("soc", 50.0))
# while energy_agent.py passed SoC nested under state["battery"]["soc_pct"].
# The LP therefore ran on a constant 50% for ~2000 shadow cycles. These tests
# pin the contract: a flat dict works, a nested one raises rather than guessing.
pf_reg = _prices([6, 6, 6, 7, 18, 19, 20, 21])

nested_state = {"battery": {"soc_pct": 8}, "is_peak_month": True, "home_load_kw": 0.5}
try:
    optimize_battery(nested_state, pf_reg, _flat_solar(pf_reg), now)
    check("nested SoC raises instead of defaulting to 50%", False,
          "no exception — the 2026-06/07 blind-LP bug has regressed")
except ValueError as exc:
    check("nested SoC raises instead of defaulting to 50%", "soc_pct" in str(exc))

try:
    optimize_battery({"is_peak_month": True}, pf_reg, _flat_solar(pf_reg), now)
    check("absent SoC raises instead of defaulting to 50%", False, "no exception")
except ValueError:
    check("absent SoC raises instead of defaulting to 50%", True)

# The LP must actually respond to SoC — a near-empty peak-month battery and a
# near-full one cannot produce the same verdict.
low  = optimize_battery({"soc_pct": 8,  "is_peak_month": True, "home_load_kw": 0.5},
                        pf_reg, _flat_solar(pf_reg), now)
high = optimize_battery({"soc_pct": 95, "is_peak_month": True, "home_load_kw": 0.5},
                        pf_reg, _flat_solar(pf_reg), now)
check("  ...LP distinguishes 8% from 95% SoC",
      low["verdict"]["action"] != high["verdict"]["action"]
      or (low.get("grid_charge_now_kw") or 0) > (high.get("grid_charge_now_kw") or 0),
      f"low={low['verdict']} high={high['verdict']}")
check("  ...and reports the real SoC, not 50%",
      low.get("soc_now_pct") == 8 and high.get("soc_now_pct") == 95,
      f"low={low.get('soc_now_pct')} high={high.get('soc_now_pct')}")

# ── Family-A instrumentation: the LP's per-slot INPUT series are logged (2026-08-01) ──
# The 2026-07-31 replay was blocked because the solar forecast the LP consumed was never
# recorded. These pin that the inputs are now attached and that solar_unreliable zeroes the
# EFFECTIVE series while the RAW Solcast is preserved for offline quantile replay.
pf_in = _prices([10, 10, 10, 12, 14, 15])
solar_in = [{"time": f["time"], "kw_est": 3.0} for f in pf_in]   # 3 kW raw Solcast each slot
st_ok = {"soc_pct": 50, "is_peak_month": False, "home_load_kw": 0.5}
r_ok = optimize_battery(st_ok, pf_in, solar_in, now)
_inp = r_ok.get("inputs") or {}
check("LP result carries an inputs dict", isinstance(r_ok.get("inputs"), dict), r_ok.get("inputs"))
check("  inputs carry per-slot solar_raw_kw aligned to the horizon",
      len(_inp.get("solar_raw_kw", [])) == r_ok["horizon_slots"], _inp.get("solar_raw_kw"))
check("  inputs carry per-slot price_c + scalar load_kw",
      len(_inp.get("price_c", [])) == r_ok["horizon_slots"] and "load_kw" in _inp, _inp)

st_bad = {"soc_pct": 50, "is_peak_month": False, "home_load_kw": 0.5, "solar_unreliable": True}
r_bad = optimize_battery(st_bad, pf_in, solar_in, now)
_inb = r_bad.get("inputs") or {}
check("solar_unreliable zeroes the EFFECTIVE solar the LP solved on",
      bool(_inb.get("solar_eff_kw")) and all(s == 0.0 for s in _inb["solar_eff_kw"]),
      _inb.get("solar_eff_kw"))
check("  ...but RAW Solcast is preserved for offline quantile replay",
      any(s > 0.0 for s in _inb.get("solar_raw_kw", [])), _inb.get("solar_raw_kw"))

# ── Plan-execution margin (2026-08-01): a conservative charge-rate derate flips a genuinely
#    tight peak deferral hold → charge — the exact Family-A lever the 2026-07-31 replay found.
#    Solar is zeroed (unreliable), so forecast-risk is inert and only the rate matters. ──
pf_tight = _prices([15, 15, 15, 15, 6, 6, 21, 21, 21, 21, 21, 21], start="2026-06-01 12:00")
solar_z = [{"time": f["time"], "kw_est": 0.0} for f in pf_tight]
st_tight = {"soc_pct": 50, "is_peak_month": True, "home_load_kw": 3.5, "solar_unreliable": True}
r_base = optimize_battery(st_tight, pf_tight, solar_z, now, params=OptParams(exec_charge_derate=1.0))
r_der = optimize_battery(st_tight, pf_tight, solar_z, now, params=OptParams(exec_charge_derate=0.3))
check("plan-execution: point-rate LP defers (holds) the tight peak fill",
      r_base["verdict"]["action"] == "hold", r_base["verdict"])
check("  ...conservative charge-rate derate flips it to charge now",
      r_der["verdict"]["action"] == "charge", r_der["verdict"])
check("  ...derate=1.0 is a no-op vs default (shadow unaffected until swept)",
      optimize_battery(st_tight, pf_tight, solar_z, now)["verdict"]["action"]
      == r_base["verdict"]["action"], "default should equal exec_charge_derate=1.0")
check("  ...the derated assumed rate is logged, below the point rate",
      r_der["inputs"]["p_charge_max_kw"] < r_base["inputs"]["p_charge_max_kw"],
      (r_der["inputs"]["p_charge_max_kw"], r_base["inputs"]["p_charge_max_kw"]))

# ── Conservative solar quantile (2026-08-08): solar_quantile_k plans solar against
#    ratio − k·uncertainty per hour, so the badly-forecast morning hours (uncertainty ≈
#    ratio) are trimmed hard and the reliable midday hours only gently. The lever for the
#    solar-TRUSTED Family-A holds — distinct from exec_charge_derate, which covers the
#    solar-ZEROED deferrals above. Default k=0.0 → identical to trusting the mean ratio. ──
from optimizer import _build_solar_series

# Direct, deterministic check of the per-hour asymmetry (no solver dependence).
_corr_q = {
    "09": {"ratio": 0.16, "uncertainty": 0.18, "n": 100},   # morning: uncertainty > ratio
    "13": {"ratio": 0.72, "uncertainty": 0.26, "n": 100},   # midday: well-forecast for its size
}
_pf_q2 = [{"time": "2026-06-01 09:00", "cents_kwh": 10.0},
          {"time": "2026-06-01 13:00", "cents_kwh": 10.0}]
_solar_q2 = [{"time": f["time"], "kw_est": 3.0} for f in _pf_q2]
_mean_s = _build_solar_series(_pf_q2, _solar_q2, 2, 0.0, solar_correction=_corr_q, solar_quantile_k=0.0)
_cons_s = _build_solar_series(_pf_q2, _solar_q2, 2, 0.0, solar_correction=_corr_q, solar_quantile_k=1.0)
check("quantile k=0 reproduces the mean-ratio correction",
      abs(_mean_s[0] - 3.0 * 0.16) < 1e-9 and abs(_mean_s[1] - 3.0 * 0.72) < 1e-9, _mean_s)
check("quantile k=1 trims the uncertain MORNING hour to zero (ratio−σ ≤ 0)",
      _cons_s[0] == 0.0, _cons_s)
check("  ...and trims the reliable MIDDAY hour only partially",
      0 < _cons_s[1] < _mean_s[1] and abs(_cons_s[1] - 3.0 * (0.72 - 0.26)) < 1e-9,
      (_cons_s[1], _mean_s[1]))
check("  ...quantile never makes solar negative",
      all(s >= 0.0 for s in _cons_s), _cons_s)

# End-to-end: a solar-TRUSTED hold flips to a protective charge under a conservative quantile.
# Cheap now → expensive later; strong raw solar that the mean ratio trusts to fill the battery,
# but ratio−σ ≈ 0 removes that credit so the LP must pre-charge in the cheap slots.
pf_qe = _prices([8, 8, 9, 10, 25, 25, 25, 25], start="2026-06-01 09:00")
mp_qe = {"solar_correction": {h: {"ratio": 0.6, "uncertainty": 0.6, "n": 100}
                             for h in ("09", "10", "11", "12")}}
solar_qe = [{"time": f["time"], "kw_est": 4.0} for f in pf_qe]
st_qe = {"soc_pct": 40, "is_peak_month": False, "home_load_kw": 0.6}
r_mean_e = optimize_battery(st_qe, pf_qe, solar_qe, now,
                            params=OptParams(solar_quantile_k=0.0), model_params=mp_qe)
r_cons_e = optimize_battery(st_qe, pf_qe, solar_qe, now,
                            params=OptParams(solar_quantile_k=1.0), model_params=mp_qe)
check("solar-trusted: mean-ratio LP holds (expects solar to fill)",
      r_mean_e["verdict"]["action"] == "hold", r_mean_e["verdict"])
check("  ...conservative solar quantile flips it to charge",
      r_cons_e["verdict"]["action"] == "charge", r_cons_e["verdict"])
check("  ...k=0.0 is a no-op vs default (shadow unaffected until swept)",
      optimize_battery(st_qe, pf_qe, solar_qe, now, model_params=mp_qe)["verdict"]["action"]
      == r_mean_e["verdict"]["action"], "default should equal solar_quantile_k=0.0")
check("  ...the conservative effective solar is logged below the mean",
      sum(r_cons_e["inputs"]["solar_eff_kw"]) < sum(r_mean_e["inputs"]["solar_eff_kw"]),
      (sum(r_cons_e["inputs"]["solar_eff_kw"]), sum(r_mean_e["inputs"]["solar_eff_kw"])))
check("  ...solar_quantile_k is logged in the inputs for replay",
      r_cons_e["inputs"].get("solar_quantile_k") == 1.0, r_cons_e["inputs"].get("solar_quantile_k"))


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
