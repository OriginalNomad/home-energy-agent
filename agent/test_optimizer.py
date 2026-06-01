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


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
