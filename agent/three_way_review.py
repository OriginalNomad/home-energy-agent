"""Three-way decision divergence watch: LLM vs deterministic vs LP optimiser.

Two modes:
  • Live records that already carry `optimizer_verdict` are read as-is.
  • Older records (logged before the optimiser was wired in) are back-filled by
    re-running optimize_battery() against the price/solar/state stored in each record,
    so we get a three-way picture over the whole backlog without waiting for cron.

Usage:
  python3 agent/three_way_review.py            # last 2 days
  python3 agent/three_way_review.py --all       # whole file
  python3 agent/three_way_review.py --since 2026-06-01
"""

import json
import os
import re
import sys
from datetime import datetime
from typing import Optional

from optimizer import optimize_battery, _synth_solar_from_record, _state_from_record

JSONL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "decisions.jsonl")


def _llm_charged(rec) -> bool:
    """Did the LLM actually charge this cycle? (reserve raised above SoC, or autonomous)."""
    reserve_set = None
    mode_auto = False
    for a in rec.get("actions", []):
        m = re.search(r"reserve\((\d+)", a)
        if m:
            reserve_set = int(m.group(1))
        if "autonomous" in a:
            mode_auto = True
    soc = rec.get("soc")
    if mode_auto:
        return True
    if reserve_set is not None and soc is not None:
        return reserve_set > soc
    return False


def _opt_for(rec) -> Optional[dict]:
    """Optimiser verdict: use the logged one if present, else back-fill."""
    if rec.get("optimizer_verdict"):
        return rec["optimizer_verdict"]
    prices = rec.get("price_forecast_6h")
    times = rec.get("price_forecast_6h_times") or []
    if not prices:
        return None
    pf = [{"time": times[i] if i < len(times) else f"slot{i}",
           "cents_kwh": c, "descriptor": ""} for i, c in enumerate(prices)]
    try:
        now = datetime.fromisoformat(rec["ts"])
        out = optimize_battery(_state_from_record(rec), pf,
                               _synth_solar_from_record(rec, pf), now)
        return out["verdict"]
    except Exception as e:
        return {"action": "error", "rule_fired": str(e)[:40]}


def main():
    args = sys.argv[1:]
    since = None
    if "--all" in args:
        since = "0000"
    elif "--since" in args:
        since = args[args.index("--since") + 1]
    else:
        # default: last 2 calendar days present in the file
        pass

    recs = []
    for line in open(JSONL):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("record_type") == "daily_accuracy":
            continue
        if not r.get("computed_verdict"):
            continue
        recs.append(r)

    if since is None and recs:
        days = sorted({r["ts"][:10] for r in recs})
        since = days[-2] if len(days) >= 2 else days[0]
    recs = [r for r in recs if r["ts"][:10] >= since]

    n = 0
    llm_det = llm_opt = opt_det = consensus = 0
    backfilled = 0
    divergences = []

    for r in recs:
        det = r["computed_verdict"]
        opt = _opt_for(r)
        if opt is None or opt.get("action") == "error":
            continue
        if not r.get("optimizer_verdict"):
            backfilled += 1
        n += 1

        llm_c = _llm_charged(r)
        det_c = det.get("action") == "charge"
        opt_c = opt.get("action") == "charge"

        if llm_c == det_c:
            llm_det += 1
        if llm_c == opt_c:
            llm_opt += 1
        if opt_c == det_c:
            opt_det += 1
        if llm_c == det_c == opt_c:
            consensus += 1
        else:
            divergences.append((r, llm_c, det, det_c, opt, opt_c))

    if n == 0:
        print("No comparable records in range.")
        return

    print(f"Three-way decision review — {recs[0]['ts'][:16]} → {recs[-1]['ts'][:16]}")
    print(f"{n} cycles  ({backfilled} optimiser verdicts back-filled, "
          f"{n - backfilled} logged live)\n")
    print(f"  three-way consensus (charge/hold all agree): {consensus}/{n} = {100*consensus/n:.0f}%")
    print(f"  LLM ↔ deterministic : {llm_det}/{n} = {100*llm_det/n:.0f}%")
    print(f"  LLM ↔ optimiser     : {llm_opt}/{n} = {100*llm_opt/n:.0f}%")
    print(f"  optimiser ↔ rules   : {opt_det}/{n} = {100*opt_det/n:.0f}%")

    # Highlight cycles where the optimiser disagrees with BOTH others (the new signal)
    opt_alone = [d for d in divergences
                 if d[5] != d[1] and d[5] != d[3]]  # opt differs from llm AND from det
    print(f"\n=== optimiser disagrees with BOTH LLM and rules ({len(opt_alone)}) ===")
    for r, llm_c, det, det_c, opt, opt_c in opt_alone[:25]:
        print(f"{r['ts'][11:16]} soc={r.get('soc')} p={r.get('price_c')}¢ peak={r.get('is_peak_month')}"
              f" | LLM={'charge' if llm_c else 'hold'}"
              f" | rules={det.get('action')}[{det.get('rule_fired')}]"
              f" | OPT={opt.get('action')}[{opt.get('rule_fired')}]")

    print(f"\n=== all other divergences ({len(divergences)-len(opt_alone)}) ===")
    for r, llm_c, det, det_c, opt, opt_c in divergences:
        if (r, llm_c, det, det_c, opt, opt_c) in opt_alone:
            continue
        tags = []
        if llm_c != det_c:
            tags.append("LLM≠rules")
        if llm_c != opt_c:
            tags.append("LLM≠opt")
        if opt_c != det_c:
            tags.append("opt≠rules")
        print(f"{r['ts'][11:16]} soc={r.get('soc')} p={r.get('price_c')}¢"
              f" | LLM={'charge' if llm_c else 'hold'}"
              f" | rules={det.get('action')}[{det.get('rule_fired')}]"
              f" | OPT={opt.get('action')}[{opt.get('rule_fired')}]"
              f"  ({', '.join(tags)})")


if __name__ == "__main__":
    main()
