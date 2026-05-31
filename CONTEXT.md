# Home Energy Automation — Session Context

*Read this at the start of every session. Then read any files referenced below that are relevant to what you're working on.*

---

## What this project is

A Home Assistant-based battery optimisation system for a single residential site in Glebe, Sydney. It controls a Tesla Powerwall 2 using price forecasts (Amber Electric dynamic tariff) and solar forecasts (Solcast) to minimise electricity bills and avoid network demand charges.

This is also the personal testbed for **Sol** — a multi-tenant battery optimisation product being built in `app/`. The rules and architecture here will eventually be replaced by Sol's MPC solver.

---

## The site

| Hardware | Detail |
|----------|--------|
| **Battery** | Tesla Powerwall 2, 13.5 kWh usable, ~5 kW charge/discharge |
| **Solar** | SolarEdge inverter, ~5 kW peak (6.12 kWp DC) |
| **EV** | Polestar 4 (~100 kWh), charged via Zappi 2 |
| **AC** | Daikin, 3 zones, ~3.5 kW max load |
| **Tariff** | Amber Electric, Ausgrid EA116 |
| **Location** | Glebe, Sydney (grid: Ausgrid) |

**Key tariff facts (EA116):**
- Demand charge applies **Nov, Dec, Jan, Feb, Mar, Jun, Jul, Aug** — 3–9pm daily
- Off-peak months (no demand charge): **Apr, May, Sep, Oct**
- Solar Sponge window: **10am–3pm** (cheapest grid import)
- Export penalty if export during 10am–3pm exceeds threshold

---

## How the system works

**Control mechanism**: `backup_reserve_percent` via Tessie REST API (the only writable Powerwall parameter available without Tesla Fleet API access).
- Set to `100%` → Powerwall charges toward full
- Set to `20%` → normal floor, self-consumption mode
- Set to `5%` → deep discharge floor during demand window (peak months only)

**Known limitation**: Cannot command a specific charge rate. Tesla's firmware decides how aggressively to pull from grid in `self_consumption` mode — typically ~1.7 kW. This is why the battery sometimes doesn't reach 100% by 3pm even when grid is cheap. Full solution requires Tesla Fleet API or MPC with rate commands.

**Dynamic grid charge target**: `sensor.battery_grid_charge_target`
- Formula: `clamp(95 − (net_solar_kWh / 13.5 × 100), 5, 95)`
- Represents the SoC the battery needs to reach so solar covers the rest of the day
- Agent uses this as a reference; recalculates live every 30 min as Solcast revises forecasts

**True SoC sensor**: `sensor.tessie_powerwall_charge`
- The local Powerwall gateway (`sensor.tesla_powerwall_2_charge`) floors SoC at `backup_reserve_percent`, so it shows 20% when battery is actually at 16%
- Tessie `live_status` REST poll provides true cloud SoC
- All emergency/condition checks use Tessie sensor; gateway used as fallback

**Solar forecast accuracy sensors** (all from Solcast/BJReplay integration):
- `sensor.solcast_pv_forecast_power_now` — Solcast instantaneous estimate in **W** (÷1000 for kW)
- `sensor.solcast_pv_forecast_forecast_this_hour` — expected generation this hour in **Wh** (÷1000 for kWh)
- `sensor.solcast_pv_forecast_forecast_next_hour` — expected generation next hour in **Wh** (÷1000 for kWh)
- `sensor.solcast_pv_forecast_forecast_remaining_today` — remaining today in **kWh** (no conversion)

The agent compares `forecast_this_hour` (hourly aggregate, more stable) against `sensor.solaredge_current_power` (actual inverter W) to determine forecast accuracy: `good` / `poor` / `unreliable`. On an unreliable forecast, `remaining_today` is ignored and the agent treats it as a zero-solar day. `forecast_next_hour` gives forward-looking context — if next hour is also low, don't wait for solar to improve.

**Home load smoothing**: `sensor.home_load_30min_average`
- Instantaneous `load_power` spiked from stove/kettle and distorted the solar shortfall forecast
- 30-min rolling average (HA statistics platform) used in grid charge target and forecast card

**Tessie API credentials:**
- Energy site ID: `2252120180790091`
- Token: in `config/secrets.yaml`
- Endpoints: `POST /api/1/energy_sites/{id}/backup` with `{"backup_reserve_percent": N}`

**Solcast credentials:**
- API Key: `I6bgkuZyCcOuP4YeRmJWBaWkIgxoYCPW`
- Resource ID: `fd2e-343e-680f-b27e`
- DC capacity: 6.12 kWp, AC: ~5 kW, Tilt: 0° (flat roof)
- Integration: HACS "HA Solcast PV Solar Forecast Integration" by BJReplay

---

## System architecture (as of 2026-05-31)

Three layers control the system. Read this before assuming any automation is "in charge":

**Layer 1 — Intent**: encoded in the agent system prompt (`agent/energy_agent.py`). Goals in priority order: no demand charges, EV never from battery, minimise cost, use solar. Changes rarely.

**Layer 2 — Agent** (`agent/energy_agent.py`): Claude-powered Python script. Runs every 30 min via cron. Reads HA sensor state + Amber price forecast + Solcast solar forecast, reasons about trade-offs, sets `backup_reserve_percent`, Powerwall mode, and Zappi mode. Logs decisions to `agent/agent_decisions.log` (plain text) and `agent/decisions.jsonl` (structured JSON per cycle). The agent handles all *strategic* decisions.

Key agent capabilities added 2026-05-31:
- **Historical price model (Rule 15)**: `HISTORICAL_PRICE_MODEL = True`. Grid charge target now computed from rolling 7-day price percentiles (p25/p75) — at cheap prices, discounts solar forecast and adds insurance floor. Self-calibrating. Rollback: set flag to False.
- **Insurance floor**: `input_number.battery_max_insurance_floor_pct` (default 70%) — minimum SoC to lock in while prices are cheap, guards against cheap window closing early.
- **Sliding forecast detector (Rule 17)**: `_detect_sliding_forecast()` — if cheap window has been "1–2h away" for 3+ cycles but never arrived, treats forecast as unreliable and charges now.
- **Solar-unreliable autonomous escalation (Rule 16)**: when solar unreliable, uses 1.5h buffer instead of 0.5h for autonomous escalation — fills from grid before cheap window closes.
- **EV 3-phase progression (Rule 18)**: Eco (trickle while cheaper upcoming) → Fast (at cheapest moment) → Eco+ (target met). Thresholds user-settable via HA sliders.
- **EV Case 6 — negative FIT solar dump (Rule 19)**: FIT < 0¢ + battery ≥ 85% + EV < 100% → Eco+ to absorb surplus solar rather than paying to export.
- **FIT price read**: `sensor.1a_wigram_road_glebe_feed_in_price` now in state + JSONL.
- **Solar zero threshold raised 8am → 9am**: flat-roof panels don't produce before ~9am; zero output at 8am is expected, not a forecast failure.
- **`battery_autonomous_revert_target_reached` automation fixed**: changed from Tessie OR gateway to Tessie only — gateway floors at reserve level, causing premature revert when reserve=100%.
- **New HA sliders**: `ev_ultra_cheap_threshold_c`, `ev_eco_gap_c`, `battery_charge_price_threshold_c`, `battery_max_insurance_floor_pct`.
- **55 unit tests** in `agent/test_decision.py`.

Key agent capabilities added 2026-05-29:
- **Short-term memory**: last 3 decisions from `decisions.jsonl` injected into every cycle. Agent can detect stateless deferral (holding 2+ cycles for a cheap window that never arrives).
- **Deferral limit**: if 2+ consecutive holds + price within 2¢ of prior cycles → flat-then-spike, charge now.
- **Time-based escalation (Rule 13)**: peak month hard deadline maths every cycle from 9am; non-peak soft deadline via `hours_to_cheap_end`.
- **`hours_to_cheap_end`**: replaces `hours_to_spike` (first price > 30¢). LLM-facing definition (system prompt) is the first *sustained* +4¢ rise. The deterministic shadow layer now uses an improved **scale-free daily-shape** version (bottom-30% band of the day's trough→evening-peak swing, with a 5¢ flat-day guard) — fixes under-reporting on gradual ramps (see below).
- **Deterministic decision layer + shadow mode (added 2026-05-29, NOT in control path)**: `compute_decision_context()` is a pure function that reproduces the agent's arithmetic (deadline maths, fill times, spread, zero-solar/deferral detectors, effective cost target) and emits a recommended verdict `{action, target_pct, mode, rule_fired}` via an ordered decision tree. Each live cycle it's computed and injected into the prompt as a *reference only* block; both the LLM's actual decision and the computed verdict are logged to `decisions.jsonl` (`computed_verdict`, `shadow_action_match`, `shadow_mode_match`) for divergence measurement. Covered by `agent/test_decision.py` (28 unit tests). Plan: collect divergence through the first June peak week → cutover with kill-switch → slim the prompt.
- **Solar zero-override**: if actual solar = 0 kW in 2+ of last 3 daylight cycles, treat as zero-solar day regardless of Solcast/Open-Meteo forecasts. Evidence beats model predictions.
- **Solar Sponge minimum floor (Rule 14)**: 10am–1pm, SoC < 50% → always charge to 50%, spread table irrelevant.
- **Price risk asymmetry**: evening prices have fat right tail — Solar Sponge charging is insurance, not arbitrage.

**Layer 3 — Rules** (HA automations, always active): hard constraints that fire deterministically regardless of agent decisions. React in seconds. Cannot be overridden by the agent. Handle safety, demand window, export guard, and edge cases.

---

## Current automation status (as of 2026-05-31)

**24 automations** in `config/automations.yaml` — **12 active (safety/monitoring), 12 disabled (agent handles)**

**Active — safety & monitoring:**

| ID | Purpose |
|----|---------|
| `battery_startup_set_reserve_floor` | Set 5% reserve on HA startup |
| `battery_autonomous_revert_target_reached` | Revert to self_consumption when charge target reached (Tessie OR gateway sensor, 30s) |
| `battery_autonomous_export_safety_net` | Emergency revert if battery exports to grid in autonomous mode (30s) |
| `battery_pre_demand_window_reset` | Set reserve to 5% at 2:55pm — CRITICAL for June+ demand window |
| `battery_post_demand_window_restore` | Restore reserve at 9pm after demand window |
| `battery_demand_window_low_warning` | Alert: low SoC during demand window |
| `battery_demand_window_critical_warning` | Alert: critical SoC, grid import imminent |
| `battery_negative_price_charge` | Charge to 100% on negative spot price (Rule 8) |
| `battery_negative_price_reset` | Reset reserve when price goes positive |
| `battery_low_soc_emergency_charge` | Charge if critically low + cheap price |
| `solar_inverter_underperformance_alert` | Alert when inverter under-produces vs Solcast |
| `ev_plugged_in_notify` | Alert when EV connects with SoC/price snapshot |

**Disabled — agent handles these decisions:**

| ID | Why disabled |
|----|-------------|
| `battery_morning_charge_trigger` | Agent decides morning charge timing |
| `battery_solar_sponge_mode_check` | Agent handles solar sponge reserve management |
| `battery_cheap_window_autonomous_charge` | Agent decides when to use autonomous mode |
| `battery_autonomous_revert_cheap_ended` | Agent manages mode reversion |
| `battery_target_exceeds_reserve` | Agent updates reserve dynamically |
| `battery_cheap_window_ended` | Agent handles cheap window end |
| `battery_charge_complete_reset` | Agent resets reserve when charged |
| `battery_overnight_safety_topup` | Agent handles overnight charging decisions |
| `battery_morning_reserve_reset` | Agent sets reserve at 6am cycle |
| `battery_winter_overnight_precharge` | Agent handles winter overnight charging |
| `battery_cloudy_day_topup` | Agent handles cloudy day top-up |
| `ev_charge_mode_manager` | Agent sets Zappi mode each 30-min cycle |

**SmartShift (Amber's control)**: OFF since 11:30am 18 May 2026.

---

## Charge mode policy (as of 2026-05-29)

- **`self_consumption`**: normal operation, ~1.7 kW grid charge rate. Used for long cheap windows (3h+) or when price spread doesn't justify urgency.
- **`autonomous` + `reserve=100%`**: fast ~5 kW grid charge. `reserve=100%` is the export guard. HA safety net (`battery_autonomous_export_safety_net`) reverts to self_consumption within 30s if export is detected. Previously banned but re-enabled after safety net was patched (2026-05-25).

**Autonomous mode is only justified when the price spread warrants it:**

| Spread (cheap now vs upcoming expensive) | Action |
|------------------------------------------|--------|
| < 5¢ | Don't charge — hold for a better window |
| 5–8¢ | `self_consumption` only, and only for long windows (3h+) |
| 8–15¢ | `self_consumption` for long windows; `autonomous` if window < 2h AND need > 15% SoC |
| > 15¢ | `autonomous` justified — real arbitrage, go hard |

**Peak month demand window overrides spread logic entirely** — if battery won't reach 85% SoC by 2:55pm via solar + self_consumption, use autonomous regardless of spread. The demand charge (~$100/month) dwarfs any charging cost calculation.

---

## EV status (as of 2026-05-29)

**Zappi plug_status values confirmed:**
- `"EV Disconnected"` — not plugged in
- `"EV Connected"` — plugged in, not charging
- `"Charging"` — actively charging

**EV charging policy:**
- Default: **Eco+** — charges only from actual solar export past the meter. Battery never discharged for EV.
- Fast mode only for: price < 5¢, EV SoC < 30% + price < 20¢, battery at/above reserve floor (Case 4), or battery charging from grid below reserve (Case 5)
- Agent (`ev_charge_mode_manager` disabled) sets Zappi mode each 30-min cycle

**Polestar entity IDs (sensor prefix: `sensor.polestar_7853_`):**
- `battery_charge_level` — SoC %
- `charging_status` — charging state
- `charger_connection_status` — connection
- `range` — estimated range
- Noisy timestamp sensors excluded from recorder/logbook: `estimated_fully_charged_time`, `last_updated_*`

**Not yet built:**
- Daikin AC load shedding during demand window
- Price spike arbitrage (Rule 10)

---

## Key files

| File | What it contains |
|------|-----------------|
| `energy_rules.md` | Full rule-set (Rules 1–14), all business logic, decision priority order, known limitations |
| `ea116_tariff.md` | EA116 tariff structure — demand charge, Solar Sponge, export penalty |
| `energy_log.md` | Chronological log of what was built each day and observations |
| `todo.md` | Personal and product to-do lists |
| `PRODUCT.md` | Full product design doc — Sol architecture, MPC design, multi-tenant vision |
| `config/automations.yaml` | The actual HA automations (12 active, 12 disabled) |
| `config/configuration.yaml` | HA config — sensors, REST commands, template sensors |
| `agent/energy_agent.py` | Claude-powered optimisation agent — the strategic decision layer |
| `agent/backtest.py` | Peak-month scenario backtest — feeds the real agent synthetic scenarios, stubs all reads/writes. Validate demand-window logic before June 1 |
| `agent/test_decision.py` | 28 unit tests for `compute_decision_context()` — pure, no API calls, run in ms |
| `agent/.env` | API keys (gitignored — not in repo) |
| `agent/agent_decisions.log` | Plain-text decision log (one line per cycle, committed to git) |
| `agent/decisions.jsonl` | Structured JSON decision log — full context per cycle, foundation for analyst agent and accuracy tracking |

---

## What to watch for

**June 1 is tomorrow** — demand window logic activates for the first time live. Watch:
- Does agent read `is_peak_month = True` from the first cycle?
- Does it apply 85% SoC target by 2:55pm deadline maths?
- Does `battery_pre_demand_window_reset` fire at 2:55pm as backstop?
- Does `battery_autonomous_revert_target_reached` (now Tessie-only) hold correctly until target is genuinely reached?

**Historical price model** — first live run was 2026-05-31. Watch `cost_target_method: historical` in JSONL. p25/p75 will shift as June peak-month prices accumulate (larger swings expected). May need to tune `CHEAP_BAND_ALPHA` and `MIN_DAILY_SWING`.

**June 1 is critical** — demand window logic activates. Any grid import 3–9pm sets the monthly demand charge. The agent must ensure battery ≥85% SoC by 2:55pm on peak month days. The `battery_pre_demand_window_reset` automation is the last-resort backstop at 2:55pm.

**On rainy/cloudy days in peak months**: Solar won't cover the deficit. Agent must use autonomous mode during the cheap window (typically 10am–2pm) to charge fast enough. At 1.7kW self_consumption rate, there may not be enough time — autonomous (5kW) is needed.

**Monitoring questions:**
- Does agent correctly use autonomous mode on cloudy peak-month mornings?
- Does `battery_autonomous_export_safety_net` catch any misbehaviour within 30s?
- Does overnight agent cycle correctly identify cheap windows and defer charging to them?
- June 1: does the agent recognise peak month and apply 2:55pm target from day one?
- **Shadow divergence**: each `/morning` review now reports LLM-vs-deterministic agreement rate and divergences. Collect through the first June peak week before deciding on cutover (Phase 5). Watch whether divergences are deterministic-layer bugs vs the LLM being over/under-cautious.

---

## The bigger picture

The system now has three layers:
1. **Intent** — goals encoded in the agent system prompt (minimise cost, no demand charges, EV never from battery)
2. **Agent** — Claude reasons about real-time state + forecasts and makes strategic decisions every 30 min
3. **Rules** — HA automations enforce hard constraints that the agent cannot override (demand window, export guard)

This is a working prototype of what Sol will productise. The agent replaces the brittle rule-based approximations with genuine look-ahead reasoning. The rules layer stays — some constraints must be deterministic and fast regardless of the reasoning layer above.

**When something behaves unexpectedly:**
- Check `/tmp/energy_agent.log` for the agent's decision narrative — it explains its reasoning
- Check HA Activity (logbook) for the timeline of mode/reserve changes
- Check `energy_rules.md` for the underlying business logic
- The safety automations (export guard, demand window reset) fire independently — check HA automations if the agent seems to be overridden

The Sol product (`/Users/simonmonk/Simon Projects/Home Energy Console/`) will eventually replace the agent with a proper MPC solver, but the three-layer architecture (intent → optimiser → safety rules) remains the design.
