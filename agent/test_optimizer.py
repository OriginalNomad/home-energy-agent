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

# 8. solar_unreliable flag: peak month, demand window visible in extended horizon.
#    Strong solar (4 kW) → without flag LP trusts it → solar-only/hold.
#    With flag LP zeroes solar → must grid-charge to avoid demand penalty.
_pf8 = _prices([9, 9, 10, 10, 11, 12, 13, 14, 15, 16, 17, 18], start="2026-06-03 09:00")
_pf8 += [{"time": f"2026-06-03 {h:02d}:{m:02d}", "cents_kwh": 25.0,
           "descriptor": "synthetic_historical"}
          for h in range(15, 21) for m in [0, 30]]
_solar8 = [{"time": f["time"], "kw_est": 4.0} for f in _pf8]
r8_ok  = optimize_battery({"soc_pct": 30, "is_peak_month": True, "home_load_kw": 1.0},
                           _pf8, _solar8, now_am)
r8_bad = optimize_battery({"soc_pct": 30, "is_peak_month": True, "home_load_kw": 1.0,
                            "solar_unreliable": True},
                           _pf8, _solar8, now_am)
check("solar_unreliable=False + strong solar → solar-only or hold",
      r8_ok["verdict"]["rule_fired"] in ("mpc_solar_only", "mpc_hold"), r8_ok["verdict"])
check("solar_unreliable=True zeroes forecast → charges from grid",
      r8_bad["verdict"]["action"] == "charge", r8_bad["verdict"])

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
