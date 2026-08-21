#!/usr/bin/env python3
"""Offline sweep: replay the LP over logged cycles under (solar_quantile_k, exec_charge_derate).

Reads decisions.jsonl cycles that carry optimizer_context.inputs (logged since 2026-08-01),
reconstructs optimize_battery()'s arguments from those per-slot input series, and re-solves
the LP at a grid of the two robustness knobs. Reports, against the DETERMINISTIC layer (the
validated control action), the two directional divergence counts:

  A = DET charge & LP hold   → LP UNDER-charges vs the rule layer (the cause-(b) class we
                               want the knobs to REDUCE), split by solar_unreliable:
                                 A_trusted  (solar_eff > 0) → the solar-quantile lever
                                 A_zeroed   (solar_unreliable) → the exec_charge_derate lever
  B = DET hold  & LP charge   → LP OVER-charges vs the rule layer (the false-alarm cost we
                               must NOT inflate).

A good knob setting drives A down sharply while keeping B ~flat.

Run on the Pi (needs the venv + the EDITED optimizer.py in the same dir):
  scp optimizer.py replay_solar_quantile.py $PI_HOST:/tmp/lp_test/
  ssh $PI_HOST 'cd /tmp/lp_test && source ~/home-energy-agent/agent/venv/bin/activate \
      && python3 replay_solar_quantile.py \
           --jsonl ~/home-energy-agent/agent/decisions.jsonl \
           --model ~/home-energy-agent/agent/model_params.json'
"""
import argparse
import json
from datetime import datetime

from optimizer import optimize_battery, OptParams

PEAK_MONTHS = {11, 12, 1, 2, 3, 6, 7, 8}


def load_cycles(path):
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("daily_accuracy") or "ts" not in r:
                continue
            inp = (r.get("optimizer_context") or {}).get("inputs")
            if not inp or not inp.get("slot_times"):
                continue
            det = (r.get("computed_verdict") or {}).get("action")
            if det not in ("charge", "hold"):
                continue
            out.append(r)
    return out


def reconstruct(r):
    """Build optimize_battery() args from a logged cycle's inputs."""
    inp = r["optimizer_context"]["inputs"]
    times = inp["slot_times"]
    price_c = inp["price_c"]
    solar_raw = inp["solar_raw_kw"]
    n = min(len(times), len(price_c), len(solar_raw))
    price_forecast = [{"time": times[i], "cents_kwh": float(price_c[i]), "descriptor": ""}
                      for i in range(n)]
    # feed RAW Solcast as kw_est so optimize_battery re-applies the correction+quantile itself
    solar_forecast = [{"time": times[i], "kw_est": float(solar_raw[i])} for i in range(n)]
    month = int(r["ts"][5:7])
    state = {
        "soc_pct": r.get("soc"),
        "is_peak_month": r.get("is_peak_month", month in PEAK_MONTHS),
        "home_load_kw": inp.get("load_kw", 0.5),
        "solar_unreliable": bool(inp.get("solar_unreliable", False)),
    }
    now = datetime.fromisoformat(r["ts"])
    return state, price_forecast, solar_forecast, now


def replay(cycles, model_params, k, derate):
    """Return per-cycle replayed LP action for the given knob setting."""
    params = OptParams(solar_quantile_k=k, exec_charge_derate=derate)
    acts = []
    for r in cycles:
        try:
            state, pf, sf, now = reconstruct(r)
            if state["soc_pct"] is None:
                acts.append(None)
                continue
            out = optimize_battery(state, pf, sf, now, params=params, model_params=model_params)
            acts.append(out["verdict"]["action"])
        except Exception as e:
            acts.append(("ERR", str(e)[:60]))
    return acts


def score(cycles, acts):
    """Directional divergence counts vs the deterministic layer."""
    A_trusted = A_zeroed = B = agree = comparable = 0
    for r, a in zip(cycles, acts):
        if not isinstance(a, str):          # None or ERR
            continue
        det = r["computed_verdict"]["action"]
        comparable += 1
        if det == a:
            agree += 1
        if det == "charge" and a == "hold":
            if bool(r["optimizer_context"]["inputs"].get("solar_unreliable")):
                A_zeroed += 1
            else:
                A_trusted += 1
        elif det == "hold" and a == "charge":
            B += 1
    return dict(comparable=comparable, agree=agree, A_trusted=A_trusted,
               A_zeroed=A_zeroed, A=A_trusted + A_zeroed, B=B)


def load_daily(path):
    """Index daily_energy.jsonl by date."""
    out = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("date"):
                out[r["date"]] = r
    return out


def outcomes(cycles, model_params, daily, derate):
    """Per-day: divergence counts (baseline vs a chosen derate) beside the day's ACTUAL outcome.

    NOTE ON WHAT THIS CAN AND CANNOT SAY: the outcome (window pass/fail, min SoC, cost) is what
    the DETERMINISTIC layer actually produced — it did the charging. It does NOT directly validate
    the LP's *counterfactual*: a single-cycle replay scores each cycle independently, so an LP
    morning-hold is not carried forward to see whether the battery would still have hit 85% by
    2:55pm. What this DOES show is whether divergences cluster on tight vs benign days, and the
    cost context — i.e. whether DET's extra insurance charging was even needed on these days. A
    true LP validation needs a full-day CLOSED-LOOP simulation (flagged as the next step).
    """
    base = replay(cycles, model_params, 0.0, 1.0)
    der = replay(cycles, model_params, 0.0, derate)
    per_day = {}
    for r, a_b, a_d in zip(cycles, base, der):
        if not (isinstance(a_b, str) and isinstance(a_d, str)):
            continue
        d = r["ts"][:10]
        det = r["computed_verdict"]["action"]
        pd = per_day.setdefault(d, dict(bA=0, bB=0, dA=0, dB=0))
        if det == "charge" and a_b == "hold":
            pd["bA"] += 1
        elif det == "hold" and a_b == "charge":
            pd["bB"] += 1
        if det == "charge" and a_d == "hold":
            pd["dA"] += 1
        elif det == "hold" and a_d == "charge":
            pd["dB"] += 1

    print(f"\n── Outcome context: per-day divergences vs ACTUAL day outcome (derate={derate}) ──")
    print("  date        pass  minSoC  peakImp  cost$   solar   | base A/B | der A/B")
    for d in sorted(per_day):
        pd = per_day[d]
        rec = daily.get(d, {})
        dw = rec.get("demand_window", {})
        passed = "Y" if dw.get("passed") else ("n" if rec else "?")
        min_soc = dw.get("min_soc_pct", "?")
        peak = dw.get("peak_30min_import_kw", "?")
        cost = dw.get("cost_est_dollars", rec.get("grid", {}).get("net_cost_c", "?"))
        ratio = rec.get("solar", {}).get("forecast_vs_actual_ratio", "?")
        print(f"  {d}   {passed:>3}   {str(min_soc):>5}   {str(peak):>6}   {str(cost):>5}   "
              f"{str(ratio):>5}   | {pd['bA']:>2}/{pd['bB']:<2}   | {pd['dA']:>2}/{pd['dB']:<2}")
    print("  (A = LP under-charges vs rule layer; B = LP over-charges. All 8 days are peak winter days.)")
    print("  ⚠️  Outcome = what DET produced, NOT the LP counterfactual — see the docstring. A full-day")
    print("      closed-loop LP sim is the real validation before any cutover.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--daily", help="daily_energy.jsonl — enables per-day outcome context")
    args = ap.parse_args()

    with open(args.model) as fh:
        model_params = json.load(fh)
    cycles = load_cycles(args.jsonl)
    days = sorted(set(r["ts"][:10] for r in cycles))
    print(f"Loaded {len(cycles)} replayable cycles over {days[0]}..{days[-1]} ({len(days)} days)\n")

    # ── fidelity: does baseline replay reproduce the ORIGINAL logged LP verdict? ──
    base_acts = replay(cycles, model_params, k=0.0, derate=1.0)
    errs = [a for a in base_acts if not isinstance(a, str) and a is not None]
    match = total = 0
    for r, a in zip(cycles, base_acts):
        logged = (r.get("optimizer_verdict") or {}).get("action")
        if isinstance(a, str) and logged in ("charge", "hold"):
            total += 1
            if a == logged:
                match += 1
    print(f"FIDELITY  baseline(k=0,derate=1.0) replay vs logged optimizer_verdict: "
          f"{match}/{total} = {100*match/max(total,1):.1f}%  "
          f"(mismatch expected from nightly model drift)\n")

    base = score(cycles, base_acts)
    print(f"BASELINE (k=0, derate=1.0):  agree with DET {base['agree']}/{base['comparable']} "
          f"= {100*base['agree']/max(base['comparable'],1):.1f}%  |  "
          f"A(under-charge)={base['A']} [trusted {base['A_trusted']} / zeroed {base['A_zeroed']}]  "
          f"B(over-charge)={base['B']}")

    def row(k, derate):
        s = score(cycles, replay(cycles, model_params, k, derate))
        return (f"  k={k:<4} derate={derate:<4} | agree {100*s['agree']/max(s['comparable'],1):5.1f}%"
                f" | A={s['A']:<3} (trusted {s['A_trusted']:<3} zeroed {s['A_zeroed']:<3})"
                f" | B={s['B']:<3}")

    print("\n── Sweep 1: solar_quantile_k (derate=1.0) — targets the solar-TRUSTED sub-class ──")
    for k in (0.0, 0.5, 1.0, 1.5, 2.0):
        print(row(k, 1.0))

    print("\n── Sweep 2: exec_charge_derate (k=0.0) — targets the solar-ZEROED sub-class ──")
    for d in (1.0, 0.7, 0.5, 0.3):
        print(row(0.0, d))

    print("\n── Sweep 3: combined (both knobs) ──")
    for k in (0.5, 1.0):
        for d in (0.7, 0.5):
            print(row(k, d))

    if args.daily:
        outcomes(cycles, model_params, load_daily(args.daily), derate=0.3)


if __name__ == "__main__":
    main()
