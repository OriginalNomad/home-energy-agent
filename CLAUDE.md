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
| `config/automations.yaml` | HA automations — **source of truth, but only what's deployed is running** (see below) |
| `config/configuration.yaml` | Sensors, REST commands, template sensors — same deploy rule |
| `deploy_ha_config.sh` | Deploys `config/` to the live HA on the Pi. `--check` diffs without changing anything |

### HA config is deployed, not read in place

`config/` in this repo is **not** read by Home Assistant. The live instance is the
Docker `homeassistant` container **on the Pi**, config mounted from
`~/homeassistant/config` — that is both what the agent talks to (`localhost:8123`)
and what the dashboard at `http://energypi.local:8123` shows. A second, vestigial
HA container on the Mac Studio is not used.

Editing `config/` changes nothing until deployed:

```bash
./deploy_ha_config.sh --check    # diff repo vs live
./deploy_ha_config.sh            # backup, copy, validate, reload (no restart)
```

Before trusting any claim about what an automation or template sensor does,
run `--check`. On 2026-07-22 the live config was found to be 7 weeks behind the
repo, so several fixes recorded in the log as "deployed" had never run.
| `ARCHITECTURE.md` | Self-learning system design — 4-layer architecture, implementation roadmap, data logger → calibration models → MPC |
| `agent/data_logger.py` | Layer 1 closed-loop SQLite logger — foundation for self-calibrating models (not yet wired into energy_agent.py) |
