"""Receding-horizon LP optimiser for battery dispatch — SHADOW MODE prototype.

This is migration-path step 2 from PRODUCT.md ("Optimisation Engine — Depth"):
a small linear program that reads the *same* state + forecasts as the LLM and the
deterministic `compute_decision_context()` layer, and emits a verdict in the *same
shape* so all three can be A/B'd on live data. It is NOT in the control path.

Why an LP instead of a rule stack: battery optimisation under a dynamic tariff is a
finite-horizon optimal control problem. The "charge in the cheapest window", "85% by
2:55pm", "Solar Sponge floor", "don't charge now if cheaper is coming" rules are all
*emergent outputs* of cost minimisation — the LP derives them instead of hard-coding
them. In particular the demand-window protection is NOT a 2:55pm/85% rule here: it's a
heavy penalty on grid import during 3-9pm, so the optimiser pre-charges *exactly* enough
to cover the evening load from battery, derived from the load + solar forecasts.

Receding horizon (MPC): we solve the whole horizon but only the *first* slot's action is
used; the agent re-solves every 30 min with fresh forecasts. That is what makes this
robust to forecast error — we only ever commit to the near-term slot (the accurate part).

Solver: scipy.optimize.linprog (HiGHS). Everything is a pure function of its inputs —
no HTTP, no clock reads, no globals — so it is unit-testable in milliseconds.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime

import numpy as np

try:
    from scipy.optimize import linprog
    _HAVE_SCIPY = True
except Exception:                       # pragma: no cover - scipy expected present
    _HAVE_SCIPY = False


# ───────────────────────────── parameters ─────────────────────────────

@dataclass
class OptParams:
    capacity_kwh: float = 13.5          # usable battery energy
    p_charge_max_kw: float = 5.0        # max charge power
    p_discharge_max_kw: float = 5.0     # max discharge power
    export_limit_kw: float = 5.0        # approved export
    eta: float = 0.95                   # one-way efficiency (round-trip ≈ eta^2)
    slot_h: float = 0.5                 # forecast bucket length (Amber = 30 min)

    soc_min_pct: float = 5.0            # hardware floor
    soc_max_pct: float = 100.0

    cycle_cost_c: float = 1.5           # ¢/kWh throughput — battery wear penalty
    demand_penalty_c: float = 1000.0    # ¢/kWh added to import inside the demand window
                                        #   (peak months) — encodes the asymmetric loss:
                                        #   a demand charge dwarfs any energy cost.
    feedin_offset_c: float = 6.0        # if no feed-in forecast given, approximate
                                        #   feedin(t) ≈ max(0, price(t) − offset)

    # robustness knob — the elicited risk tolerance maps to this (PRODUCT.md):
    # scale the solar forecast down (plan against a conservative quantile) and the
    # load up. 0.0 = trust the point forecast; higher = more conservative.
    risk: float = 0.0

    # action-mapping thresholds (verdict translation)
    grid_charge_threshold_kw: float = 0.3   # grid-sourced charge above this ⇒ "charge"
    fast_threshold_kw: float = 2.5          # grid-sourced charge above this ⇒ autonomous

    demand_window_start_h: int = 15         # 3pm
    demand_window_end_h: int = 21           # 9pm


# ───────────────────────────── helpers ─────────────────────────────

def _norm_time(t: str) -> str:
    """Normalise 'YYYY-MM-DDTHH:MM' and 'YYYY-MM-DD HH:MM' to a common key."""
    return (t or "").replace("T", " ").strip()[:16]


def _slot_hour(t: str) -> int | None:
    """Hour-of-day for a forecast slot's start time, or None if unparseable."""
    n = _norm_time(t)
    try:
        return int(n[11:13])
    except (ValueError, IndexError):
        return None


def _build_solar_series(price_forecast, solar_forecast, n, fallback_kw):
    """Align solar (kW) to the price-forecast time grid; 0 where unknown."""
    smap = {_norm_time(s.get("time", "")): float(s.get("kw_est", 0.0))
            for s in (solar_forecast or [])}
    out = []
    for f in price_forecast[:n]:
        key = _norm_time(f.get("time", ""))
        out.append(smap.get(key, fallback_kw if not smap else 0.0))
    return out


# ───────────────────────────── core LP ─────────────────────────────

def optimize_battery(state: dict,
                     price_forecast: list[dict],
                     solar_forecast: list[dict],
                     now: datetime,
                     params: OptParams | None = None,
                     feedin_forecast: list[float] | None = None) -> dict:
    """Solve the receding-horizon dispatch LP and return a verdict + diagnostics.

    Returns the same outer shape as compute_decision_context():
      {"verdict": {action, target_pct, mode, rule_fired}, ...diagnostics}
    so it can be logged and compared side-by-side.
    """
    p = params or OptParams()

    prices = [float(f.get("cents_kwh", 0.0)) for f in price_forecast]
    H = len(prices)
    soc0_pct = float(state.get("soc_pct", state.get("soc", 50.0)) or 50.0)
    soc0_kwh = soc0_pct / 100.0 * p.capacity_kwh
    is_peak = bool(state.get("is_peak_month"))
    home_load = float(state.get("home_load_kw", 0.5) or 0.5)

    if not _HAVE_SCIPY:
        return _verdict_only("hold", None, None, "no_solver", H, soc0_pct)
    if H < 3:
        return _verdict_only("hold", None, None, "insufficient_forecast", H, soc0_pct)

    # forecasts on the price grid, with the robustness knob applied
    solar_unreliable = bool(state.get("solar_unreliable", False))
    solar = _build_solar_series(price_forecast, solar_forecast, H, fallback_kw=0.0)
    if solar_unreliable:
        solar = [0.0] * H                    # mirror deterministic: ignore Solcast when flag set
    else:
        solar = [s * (1.0 - 0.5 * p.risk) for s in solar]        # conservative solar
    load = [home_load * (1.0 + 0.3 * p.risk)] * H                # conservative load
    if feedin_forecast and len(feedin_forecast) >= H:
        feedin = [float(x) for x in feedin_forecast[:H]]
    else:
        feedin = [max(0.0, pr - p.feedin_offset_c) for pr in prices]

    # variable layout: x = [c(H), d(H), gi(H), ge(H), soc(H)]
    nC, nD, nGI, nGE, nSOC = 0, H, 2 * H, 3 * H, 4 * H
    nvars = 5 * H
    dt = p.slot_h

    # ── objective (minimise total ¢ over horizon) ──
    cost = np.zeros(nvars)
    for t in range(H):
        in_window = is_peak and _in_demand_window(price_forecast[t], p)
        imp_c = prices[t] + (p.demand_penalty_c if in_window else 0.0)
        cost[nGI + t] = imp_c * dt                  # pay to import
        cost[nGE + t] = -feedin[t] * dt             # earn to export
        cost[nC + t] = p.cycle_cost_c * dt          # wear on charge
        cost[nD + t] = p.cycle_cost_c * dt          # wear on discharge
    # value terminal stored energy so the plan doesn't dump the battery at horizon end
    terminal_value_c = min(prices)                  # conservative: cheapest buy avoided
    cost[nSOC + (H - 1)] = -terminal_value_c

    # ── equality constraints ──
    A_eq, b_eq = [], []
    # (1) power balance: solar + import + discharge = load + charge + export
    for t in range(H):
        row = np.zeros(nvars)
        row[nGI + t] = 1.0
        row[nD + t] = 1.0
        row[nC + t] = -1.0
        row[nGE + t] = -1.0
        A_eq.append(row)
        b_eq.append(load[t] - solar[t])
    # (2) SoC recursion: soc[t] = soc[t-1] + (eta*c - d/eta)*dt
    for t in range(H):
        row = np.zeros(nvars)
        row[nSOC + t] = 1.0
        row[nC + t] = -p.eta * dt
        row[nD + t] = (1.0 / p.eta) * dt
        if t == 0:
            A_eq.append(row); b_eq.append(soc0_kwh)
        else:
            row[nSOC + (t - 1)] = -1.0
            A_eq.append(row); b_eq.append(0.0)

    # ── variable bounds ──
    soc_lo = p.soc_min_pct / 100.0 * p.capacity_kwh
    soc_hi = p.soc_max_pct / 100.0 * p.capacity_kwh
    bounds = []
    bounds += [(0.0, p.p_charge_max_kw)] * H            # c
    bounds += [(0.0, p.p_discharge_max_kw)] * H         # d
    bounds += [(0.0, None)] * H                         # gi  (import always allowed; penalised in window)
    bounds += [(0.0, p.export_limit_kw)] * H            # ge
    bounds += [(soc_lo, soc_hi)] * H                    # soc

    res = linprog(cost, A_eq=np.array(A_eq), b_eq=np.array(b_eq),
                  bounds=bounds, method="highs")

    if not res.success:
        # should be rare (import is never hard-blocked) — fail safe toward protection
        return _verdict_only("charge", 85, "autonomous", "mpc_infeasible_fallback",
                             H, soc0_pct)

    x = res.x
    c0, d0, gi0, ge0 = x[nC], x[nD], x[nGI], x[nGE]
    soc_traj_pct = [round(x[nSOC + t] / p.capacity_kwh * 100.0, 1) for t in range(H)]

    # ── translate first-slot plan into a grid-intent verdict ──
    solar_surplus0 = max(0.0, solar[0] - load[0])
    grid_charge0 = max(0.0, c0 - solar_surplus0)        # battery charge sourced from grid

    if grid_charge0 > p.grid_charge_threshold_kw:
        mode = "autonomous" if grid_charge0 > p.fast_threshold_kw else "self_consumption"
        # target = peak SoC the plan builds to over the next ~6h (12 slots)
        horizon_lookahead = min(H, 12)
        target = int(round(max(soc_traj_pct[:horizon_lookahead])))
        rule = "mpc_charge_grid"
    elif c0 > p.grid_charge_threshold_kw:
        # charging, but only from solar surplus — no reserve change needed
        mode, target, rule = None, None, "mpc_solar_only"
        return _finish("hold", target, mode, rule, p, prices, feedin, solar, load,
                       soc_traj_pct, res.fun, x, H, soc0_pct, grid_charge0)
    else:
        mode, target, rule = None, None, "mpc_hold"
        return _finish("hold", target, mode, rule, p, prices, feedin, solar, load,
                       soc_traj_pct, res.fun, x, H, soc0_pct, grid_charge0)

    return _finish("charge", target, mode, rule, p, prices, feedin, solar, load,
                   soc_traj_pct, res.fun, x, H, soc0_pct, grid_charge0)


# ───────────────────────────── result packing ─────────────────────────────

def _in_demand_window(slot: dict, p: OptParams) -> bool:
    h = _slot_hour(slot.get("time", ""))
    return h is not None and p.demand_window_start_h <= h < p.demand_window_end_h


def _verdict_only(action, target, mode, rule, H, soc0_pct):
    return {
        "verdict": {"action": action, "target_pct": target, "mode": mode,
                    "rule_fired": rule},
        "horizon_slots": H,
        "soc_now_pct": round(soc0_pct, 1),
        "projected_cost_c": None,
        "soc_trajectory_pct": None,
        "grid_charge_now_kw": None,
    }


def _finish(action, target, mode, rule, p, prices, feedin, solar, load,
            soc_traj_pct, objective_c, x, H, soc0_pct, grid_charge0):
    nGI = 2 * H
    total_import_kwh = round(float(sum(x[nGI:nGI + H])) * p.slot_h, 2)
    return {
        "verdict": {"action": action, "target_pct": target, "mode": mode,
                    "rule_fired": rule},
        "horizon_slots": H,
        "soc_now_pct": round(soc0_pct, 1),
        "projected_cost_c": round(float(objective_c), 1),     # horizon $ (¢), incl. wear & terminal value
        "projected_import_kwh": total_import_kwh,
        "soc_trajectory_pct": soc_traj_pct,
        "grid_charge_now_kw": round(float(grid_charge0), 2),
        "min_price_c": round(min(prices), 1),
        "max_price_c": round(max(prices), 1),
    }


# ───────────────────────────── standalone demo / sanity ─────────────────────────────

def _synth_solar_from_record(rec: dict, price_forecast: list[dict]) -> list[dict]:
    """Rough solar series for the standalone demo from a JSONL record's solar fields."""
    this_h = float(rec.get("solar_this_hour_kwh") or 0.0)
    next_h = float(rec.get("solar_next_hour_kwh") or 0.0)
    out = []
    for i, f in enumerate(price_forecast):
        kw = (this_h if i < 2 else next_h if i < 4 else 0.0) * 2.0  # kWh/30min → kW
        out.append({"time": f["time"], "kw_est": kw})
    return out


def _state_from_record(rec: dict) -> dict:
    return {
        "soc_pct": rec.get("soc"),
        "is_peak_month": rec.get("is_peak_month"),
        "home_load_kw": rec.get("home_load_kw"),
    }


def _demo_from_jsonl(path: str):
    """Run the optimiser against the most recent real cycle in decisions.jsonl."""
    import json
    last = None
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("price_forecast_6h") and r.get("computed_verdict"):
                last = r
    if not last:
        print("no usable record found")
        return
    times = last.get("price_forecast_6h_times") or []
    pf = [{"time": times[i] if i < len(times) else f"slot{i}",
           "cents_kwh": c, "descriptor": ""}
          for i, c in enumerate(last["price_forecast_6h"])]
    state = _state_from_record(last)
    solar = _synth_solar_from_record(last, pf)
    now = datetime.fromisoformat(last["ts"])
    out = optimize_battery(state, pf, solar, now)
    print(f"cycle ts        : {last['ts']}")
    print(f"soc / peak month: {last.get('soc')}% / {last.get('is_peak_month')}")
    print(f"prices (¢)      : {last['price_forecast_6h']}")
    print(f"LLM actions     : {last.get('actions')}")
    print(f"deterministic   : {last['computed_verdict']}")
    print(f"OPTIMISER       : {out['verdict']}")
    print(f"  grid charge now: {out.get('grid_charge_now_kw')} kW | "
          f"proj import: {out.get('projected_import_kwh')} kWh | "
          f"soc traj: {out.get('soc_trajectory_pct')}")


if __name__ == "__main__":
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    _demo_from_jsonl(os.path.join(here, "decisions.jsonl"))
