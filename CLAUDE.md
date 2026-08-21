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
4. **Deploy any `config/` changes** — `./deploy_ha_config.sh`, then `--check` to confirm zero
   drift. Editing `config/` without deploying leaves the repo and the running system disagreeing
5. **Commit and push all changes to GitHub** — so the next session (on any device) starts from the correct state

If the session was read-only (no changes made), still add a brief log entry if anything meaningful was discussed or decided.

---

## Key behavioural rules

- **Never assume** the log or a doc is the source of truth. For automations and template
  sensors, run `./deploy_ha_config.sh --check` — the repo file is not necessarily what's
  running. For live values, read the sensor from HA.
- **Verify before claiming.** On 2026-07-22 three confident claims turned out to be wrong:
  a charge-rate "tail" that was a query bug (mode changed mid-interval), a "sensor glitch"
  that was the user overriding manually, and an automation description read from a file
  nothing loads. When a quick ad-hoc query contradicts tested code, suspect the query first.
- **Autonomous mode** is used with `reserve=100%` for fast (~5 kW) grid charging during cheap windows. Without `reserve=100%` it causes unwanted export. Never use autonomous mode without `reserve=100%`.
- **self_consumption** is used for all other charging phases. Measured at **~1.5 kW**
  (p25 1.35 / median 1.61 / p90 1.89, 45 days), tapering above 80% SoC — not the flat
  1.7 kW originally assumed. Rates live in `agent/model_params.json`.
- **Rule 2 is absolute** — no grid import 3–9 pm in peak months (Nov, Dec, Jan, Feb, Mar, Jun, Jul, Aug), no exceptions
- **Manual override**: `input_boolean.agent_manual_override` suspends the rule layer's
  commands (auto-expires 12h, fails open). It does **not** suspend the Rule 2 demand-window
  guard or the HA safety automations.

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
| `ARCHITECTURE.md` | Self-learning system design — 4-layer architecture, implementation roadmap, data logger → calibration models → MPC |
| `agent/data_logger.py` | Layer 1 closed-loop SQLite logger — wired into `energy_agent.py` since 2026-06-06; `energy_log.db` accumulating |
| `agent/build_models.py` | Builds `model_params.json` from `energy_log.db` — solar corrector + charge rate model. Run on the Pi |

---

## HA config is deployed, not read in place

`config/` in this repo is **not** read by Home Assistant. The live instance is the
Docker `homeassistant` container **on the Pi**, config mounted from
`~/homeassistant/config` — that is both what the agent talks to (`localhost:8123`)
and what the HA dashboard shows.

Editing `config/` changes nothing until deployed:

```bash
./deploy_ha_config.sh --check    # diff repo vs live
./deploy_ha_config.sh            # backup, validate, reload (no restart)
```

**Before trusting any claim about what an automation or template sensor does, run
`--check`.** On 2026-07-22 the live config was found to be 7 weeks behind the repo,
so several fixes recorded in the log as "deployed" had never run — including the
`battery_grid_charge_target` 85% peak floor, whose absence made autonomous mode
self-cancelling on peak days.

There is exactly **one** Home Assistant, on the Pi. A second instance ran on the Mac
until 2026-07-22 and was retired (`docker stop` + `--restart=no`); its existence is
how the drift went unnoticed. Do not start it.
