# Claude Code — Project Instructions

## On every session start

1. Read `CONTEXT.md` — this is the canonical state of the project
2. Read any other files referenced in CONTEXT.md that are relevant to what we're working on
3. Do not assume anything about the current state of automations, sensors, or rules without reading the source files

---

## On every session end

Before closing, always:

1. **Update `energy_rules.md`** — if any rules changed, were clarified, or new ones were added
2. **Update `energy_log.md`** — add a dated entry summarising what was done, what was observed, and any decisions made
3. **Update `CONTEXT.md`** — reflect any changes to automation count, live status, sensors, or key decisions
4. **Commit and push all changes to GitHub** — so the next session (on any device) starts from the correct state

If the session was read-only (no changes made), still add a brief log entry if anything meaningful was discussed or decided.

---

## Key behavioural rules

- **Never assume** the log summary is the source of truth — always read the actual `automations.yaml` if there's any doubt about what's running
- **Autonomous mode** is used with `reserve=100%` for fast (~5 kW) grid charging during cheap windows. Without `reserve=100%` it causes unwanted export. Never use autonomous mode without `reserve=100%`.
- **self_consumption** is used for all other charging phases (~1.7 kW from grid)
- **Rule 2 is absolute** — no grid import 3–9 pm in peak months (Nov, Dec, Jan, Feb, Mar, Jun, Jul, Aug), no exceptions
- **June 1** is the start of the next peak month — demand window logic must be solid before then

---

## Key files

| File | Purpose |
|------|---------|
| `CONTEXT.md` | Session onboarding — current system state, automation count, what to watch |
| `energy_rules.md` | Full rule-set and business logic — source of truth for *why* things work the way they do |
| `energy_log.md` | Chronological log of changes, observations, and decisions |
| `todo.md` | Outstanding work items |
| `config/automations.yaml` | The actual running automations |
| `config/configuration.yaml` | Sensors, REST commands, template sensors |
