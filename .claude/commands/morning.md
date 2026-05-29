Read the following files in order:
1. CLAUDE.md
2. CONTEXT.md
3. app/CONTEXT.md
4. todo.md
5. The last 50 lines of energy_log.md

Then give me a 4-part summary:
- **System status** — agent, automations, anything to watch today
- **App status** — Sol prototype, where it's up to
- **Today's priorities** — from todo.md and anything flagged in the log
- **Shadow-layer analysis** — LLM vs deterministic decision layer (see below)

## Shadow-layer analysis

The agent runs in shadow mode: each cycle logs both the LLM's actual decision and the
deterministic `compute_decision_context()` recommendation to `agent/decisions.jsonl`
(fields `computed_verdict`, `computed_context`, `shadow_action_match`, `shadow_mode_match`).

For this section, read recent records from `agent/decisions.jsonl` (skip `daily_accuracy`
rows; focus on roughly the last day or two of cycles, and any records that carry the
`computed_verdict` field) and report:

- **Agreement rate** — % of cycles where action matched (`shadow_action_match`) and, among
  charge cycles, where mode matched (`shadow_mode_match`).
- **Divergences** — for each disagreement: timestamp, what the LLM did vs what the
  deterministic layer recommended (`rule_fired`), and a one-line read on *which was right*
  given the prices/SoC/solar in that record. Call out any that look like a real bug in the
  deterministic layer (e.g. a metric mis-firing) vs the LLM being over/under-cautious.
- **Recommendation on next steps** — where we are on the re-architecture path and what's
  needed to advance: Phase 4 (collect divergence data through the first June peak-month
  week) → Phase 5 (cutover with kill-switch) → Phase 6 (slim the prompt). State whether
  the data so far supports trusting the deterministic layer yet, or what to fix/watch first.

If there are too few shadow records to be meaningful yet, say so and note how many cycles
have accumulated.

## Standing instructions for the session

After giving the morning summary, apply these rules for the rest of the session without needing to be asked:

- **Update `energy_rules.md` immediately** whenever a rule changes, is clarified, or a new one is added — don't wait to be prompted
- **Update `energy_log.md`** with a dated entry at the end of any meaningful work block (not just end of day)
- **Update `CONTEXT.md`** if automation count, agent behaviour, or system architecture changes
- When editing `agent/energy_agent.py` system prompt, mirror the change in `energy_rules.md` in the same response
