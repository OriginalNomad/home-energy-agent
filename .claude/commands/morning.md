Read the following files in order:
1. CLAUDE.md
2. CONTEXT.md
3. app/CONTEXT.md
4. todo.md
5. The last 50 lines of energy_log.md
6. ARCHITECTURE.md (skim — focus on the implementation roadmap section and any Phase markers)

Then give me a 6-part summary:
- **System status** — agent, automations, anything to watch today
- **App status** — Sol prototype, where it's up to
- **Today's priorities** — from todo.md and anything flagged in the log
- **Three-way decision analysis** — LLM vs deterministic vs LP optimiser (see below)
- **Daily energy journal review** — schema completeness check (see below)
- **Architecture progress** — where we are on the self-learning roadmap (see below)

## Three-way decision analysis

The agent runs three decision layers in parallel; only the LLM is in the control path.
Each cycle logs all three to `agent/decisions.jsonl`:
1. **LLM** — what the agent actually did (the control path).
2. **Deterministic** — `compute_decision_context()`, the rule-tree shadow layer.
3. **LP optimiser** — `optimizer.py`, the receding-horizon linear program (shadow,
   added 2026-06-01). Fields appear only on cycles run after the optimiser was wired in,
   so older records will lack them — that's expected.

For this section, read recent records from `agent/decisions.jsonl` (skip `daily_accuracy`
rows; focus on roughly the last day or two of cycles, and any records that carry the
`computed_verdict` field) and report:

**Key JSONL field names** (use these exactly — wrong names silently return null):
- LLM action: `actions` (list, empty = hold), `reserve_set` (int or null), `mode_set` (str or null)
- State: `soc` (not `soc_pct`), `price_c` (not `price_now_c`), `mode_before`
- Deterministic: `shadow_action_match`, `shadow_mode_match`, `computed_verdict` (dict with `action/mode/rule_fired`), `computed_context` (dict with `spread_c`, `forward_min_c`, `hours_to_cheap_end`, `hours_to_deadline`, `deferral_detected`)
- LP optimiser: `optimizer_verdict` (dict with `action/target_pct/mode/rule_fired`), `optimizer_context` (dict with `grid_charge_now_kw`, `projected_import_kwh`, `soc_trajectory_pct`, `projected_cost_c`, `horizon_slots`), `optimizer_action_match` (optimiser vs LLM), `optimizer_vs_deterministic` (optimiser vs rule layer)
- LLM held = `actions == []` (no set_reserve or set_mode calls made)
- Optimiser rule_fired values: `mpc_charge_grid` / `mpc_solar_only` / `mpc_hold` / `mpc_infeasible_fallback` / `insufficient_forecast` / `no_solver`

Report:

- **Agreement rates** — three pairwise numbers over the cycles that carry all fields:
  LLM↔deterministic (`shadow_action_match`, plus `shadow_mode_match` among charge cycles),
  LLM↔optimiser (`optimizer_action_match`), and optimiser↔deterministic
  (`optimizer_vs_deterministic`). A handy headline is the **three-way consensus rate** —
  % of cycles where all three agree on charge-vs-hold.
- **Divergences** — focus on cycles where the optimiser disagrees with the other two (the
  new signal), then any LLM↔deterministic disagreements as before. For each: timestamp,
  what each layer did (`rule_fired`), and a one-line read on *which was right* given the
  prices/SoC/solar in that record. Distinguish three causes:
  (a) a real bug in a layer (a metric mis-firing),
  (b) the LLM being over/under-cautious, and
  (c) the optimiser trusting a point forecast the others hedge against (e.g. it picks
      `mpc_solar_only`/hold while the rules charge as cheap insurance) — these are the
      robust-MPC cases that motivate a conservative solar quantile, not bugs.
- **Recommendation on next steps** — where we are on the re-architecture path and what's
  needed to advance: Phase 4 (collect divergence data through the first June peak-month
  week — now three-way) → Phase 5 (cutover with kill-switch) → Phase 6 (slim the prompt).
  State whether the data yet supports trusting *either* shadow layer, which one tracks
  reality better, and what to fix/watch first. If the optimiser keeps diverging via cause
  (c), note whether the `risk` knob / a conservative solar quantile would close the gap.

If there are too few shadow records to be meaningful yet, say so and note how many cycles
have accumulated — separately for the deterministic layer and the (newer) optimiser, since
the optimiser only began logging from its wire-in cycle.

## Daily energy journal review

`agent/daily_energy.jsonl` is the durable daily record that outlives HA's recorder and will
feed a future learning/analyst agent. Each morning, read the most recent record(s) and think
about whether the schema captures everything a learning agent would need to answer:

- "Why did this day pass or fail the demand window?"
- "Was the agent's charging strategy optimal given what actually happened?"
- "What patterns predict bad outcomes (cloudy + peak + low overnight SoC)?"

Specifically, check whether the current schema is missing any data that was **actually
available and relevant** in yesterday's decisions.jsonl cycles or HA sensor history. Examples
of things that might be worth adding as the system evolves:

- **Weather**: cloud cover, temperature (drives AC load), rain (drives solar accuracy)
- **EV**: was it plugged in, did it charge, how much energy did it take, what mode was used
- **AC/HVAC**: was the Daikin running during the demand window, what was the load contribution
- **Autonomous mode events**: did the agent escalate to autonomous, when, for how long
- **Forecast evolution**: how much did the Solcast/Amber forecasts shift during the day
- **Battery degradation signals**: charge/discharge cycles, depth of discharge
- **Cost**: estimated daily electricity cost (import×price − export×FIT)

Report one of:
- **Schema is complete** — no gaps found for the data currently available
- **Suggested additions** — list specific fields, where the data comes from (which sensor or
  JSONL field), and why a learning agent would need them. Don't suggest fields for data sources
  that don't exist yet (e.g. weather sensors not installed).

If you recommend additions, don't implement them automatically — just flag them. The user
decides whether to expand the schema.

## Architecture progress

`ARCHITECTURE.md` describes a 4-layer self-learning system. Each morning, check:

**Layer 1 — Data logger** (`agent/data_logger.py`, `agent/energy_log.db`):
- Is it wired into `energy_agent.py`? (Look for `data_logger.log_cycle_start`, `log_price_forecast`, `log_agent_decision` calls)
- If not yet wired in, that's the immediate next step — it's the foundation for everything else.
- If wired in: how many rows have accumulated? (`python agent/data_logger.py` for a quick health check)

**Layer 2 — Self-calibrating models** (not yet built):
- Has enough data accumulated to build Model 2 (charge rate)? Needs ~1 week.
- Has enough data accumulated to build Model 1 (solar corrector)? Needs ~2 weeks.
- Report current data age and row count so we know when these become buildable.

**Phase markers to track:**
- `Phase 2.5-A` — 1 week of logged data → build charge rate model
- `Phase 2.5-B` — 2 weeks → build solar corrector + nightly retraining
- `Phase 3` — MPC solver (after models validated)
- `Phase 4` — load model + EV scheduling

Report: which phase we're in, what's blocking the next phase, and whether any data threshold has been crossed since the last session.

If `data_logger.py` isn't yet wired into `energy_agent.py`, flag this prominently — it's the single highest-value item in the architecture roadmap.

## Standing instructions for the session

After giving the morning summary, apply these rules for the rest of the session without needing to be asked:

- **Update `energy_rules.md` immediately** whenever a rule changes, is clarified, or a new one is added — don't wait to be prompted
- **Update `energy_log.md`** with a dated entry at the end of any meaningful work block (not just end of day)
- **Update `CONTEXT.md`** if automation count, agent behaviour, or system architecture changes
- When editing `agent/energy_agent.py` system prompt, mirror the change in `energy_rules.md` in the same response
