Session close-out. Work through these checks in order, fixing anything that's missing before moving to the next step.

## 1. Review what happened this session

Read the last 100 lines of `energy_log.md` to understand what was done today. This is the baseline for the checks below.

## 2. Check energy_rules.md

Read `energy_rules.md`. For each change made to `agent/energy_agent.py` system prompt today:
- Is the rule reflected accurately?
- Is the rule number correct and consistent with the system prompt?
- Is the rationale ("why") present, not just the mechanic?

Fix any gaps before continuing.

## 3. Check CONTEXT.md

Read `CONTEXT.md`. Verify:
- Architecture section reflects current agent capabilities (any new rules, memory features, or logic changes)
- Key files table is accurate (correct log paths, rule count)
- Automation count and status is still correct
- "As of" dates are today or recently updated
- "What to watch for" section reflects any upcoming milestones or known issues

Fix any gaps before continuing.

## 4. Check energy_log.md

The log should have a dated entry for today covering everything significant — not just the last thing done. Check for gaps:
- Were any bugs found and fixed? Logged?
- Were any rules added or changed? Logged?
- Were any HA cards, sensors, or automations changed? Logged?
- Were any decisions made about approach or architecture? Logged?
- Was any agent behaviour observed (good or bad)? Logged?

Add any missing entries before continuing.

## 5. Check todo.md

Read `todo.md`. Verify:
- Any items completed today are marked `[x]`
- Any new work items surfaced today are added
- Any items that turned out to be wrong or irrelevant are removed or updated

## 6. Commit and push

Stage and commit all changed files with a message summarising what was done today. Push to GitHub.

## 7. Final summary

Give a 3-part closing summary:
- **What changed** — rules, agent behaviour, HA config, documentation
- **What's live** — anything the agent will behave differently on next cycle
- **Next session priorities** — top 2–3 things from todo.md to pick up next time
