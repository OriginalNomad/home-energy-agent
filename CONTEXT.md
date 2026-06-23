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

**Known limitation**: Cannot command a specific charge rate. Tesla's firmware decides how aggressively to pull from grid in `self_consumption` mode. **Phase 2.5-A data (17 days)** shows: peak ~1.66 kW at 60% SoC, tapering to 0.876 kW at 80% and 0.625 kW at 90%. `_avg_charge_rate_kw()` now uses these model rates (from `agent/model_params.json`) for fill time projections instead of a flat 1.7 kW assumption. Full rate control requires Tesla Fleet API or MPC.

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

## Infrastructure (as of 2026-06-05)

| Host | Role |
|------|------|
| **Mac Studio** (`192.168.68.70`) | Runs Home Assistant (localhost:8123), development machine |
| **Raspberry Pi 5** (`energypi.local`, `192.168.0.67`) | Runs energy agent cron, cloudflared tunnel |
| **GitHub** (`OriginalNomad/home-energy-agent`) | Single repo, auto-deployed to Pi on each cron run |

**Agent cron on Pi** (`~/home-energy-agent`): every 30 min does `git pull -q` then runs agent. Deploy = `git push` from Mac.
**Cloudflare Tunnel**: `https://agent.sol.io` → Pi cloudflared → `http://192.168.68.70:8123`. Systemd service, connects via Sydney edge.
**HA external URL**: `https://agent.sol.io`. Trusted proxies configured for Pi subnet + Docker bridge.

## System architecture (as of 2026-06-23)

Three layers control the system. Read this before assuming any automation is "in charge":

**Layer 1 — Intent**: encoded in the agent system prompt (`agent/energy_agent.py`). Goals in priority order: no demand charges, EV never from battery, minimise cost, use solar. Changes rarely.

**Layer 2 — Agent** (`agent/energy_agent.py`): Python script running every 30 min via cron on the Pi. **As of 2026-06-06, `DETERMINISTIC_AUTHORITATIVE = True` — the deterministic rule layer (`compute_decision_context()`) drives all control actions before the LLM runs.** The LLM runs for narrative/logging only; its `set_*` calls are no-op'd. Kill-switch: flip `DETERMINISTIC_AUTHORITATIVE = False` to revert to LLM-authoritative. Logs decisions to `agent/agent_decisions.log` (plain text) and `agent/decisions.jsonl` (structured JSON per cycle). Also writes to `agent/energy_log.db` (SQLite, via `data_logger.py` — wired in 2026-06-06, Phase 2.5-A clock running).

Key agent capabilities added 2026-06-02:
- **Demand-window reserve guard (Rule 2 backstop)**: at the start of every `run_agent()` cycle, before the LLM runs — if peak month AND 15:00–21:00 AND `reserve > 10%`, immediately calls Tessie API to set reserve=5%, bypassing HA rest_commands entirely. Prevents the June 2 failure mode (reserve stranded at charging floor during demand window, battery unable to discharge).
- **HA rest_command health check**: each cycle checks `/api/services` for the `rest_command` domain; warns loudly if missing so config failures surface immediately rather than silently for 36h.
- **Daily energy journal** (`agent/log_daily_energy.py`, cron 21:05): comprehensive per-day record — solar forecast/actual, battery SoC trajectory, grid import/export by window, price profiles, demand window pass/fail (billing-accurate: peak 30-min avg kW), agent decision rollup. Persisted to `agent/daily_energy.jsonl`. Supersedes the narrower `log_demand_window.py`.
- **`sensor.demand_window_monitor`** pushed to HA via REST API (no config change) each hour + after daily recompute. Feeds two Markdown dashboard cards: (1) peak 30-min import bars per day, (2) pass/fail timeline with min SoC.
- **June 2 demand window breach**: SoC reached only 81% (target 85%), reserve stuck at 80%. `battery_pre_demand_window_reset` automation fired at 2:55pm but errored — `rest_command` had failed to load at HA startup on June 1 (truncated payload, fixed but HA never restarted). Grid covered cooking load at 7pm. Fixed: Tessie API direct call to drop reserve → HA restart → rest_commands now loading cleanly.

Key agent capabilities added 2026-06-06 (session 10, continued):
- **Phase 5 cutover — `DETERMINISTIC_AUTHORITATIVE = True`**: deterministic rule layer now owns the control path. LLM narrative-only. Fixes class of bug where LLM constructs locally valid reasoning leading to wrong action (e.g. charging during demand window). Kill-switch at top of file.
- **`_guarded_set_reserve()` in TOOL_MAP**: blocks any `set_powerwall_reserve(N > 10)` during 3–9pm peak months. Belt-and-suspenders with the pre-flight guard. Fixes June 6 demand window — reserve stuck at 80% for 7 consecutive cycles because LLM was overriding the guard.
- **`data_logger.py` wired into `energy_agent.py`**: `energy_log.db` created on Pi startup. `log_cycle_start`, `log_price_forecast`, `log_agent_decision` called each cycle (guarded by `_HAVE_DATA_LOGGER`). Phase 2.5-A (charge rate model) buildable ~2026-06-13.

Key agent capabilities added 2026-06-06 (session 10):
- **Tessie SoC=0 sanity guard (`_build_battery_state()`)**: new function called from `get_current_state()`. If Tessie returns 0% or gateway reads >15% above Tessie when gateway is reliable (`gateway > reserve`), substitutes gateway and sets `tessie_soc_failed=True`. Prevents panic charging when Tessie has a cloud API hiccup. Three new JSONL fields: `soc_tessie_pct`, `soc_gateway_pct`, `tessie_soc_failed`.
- **Hold ≠ arming (CRITICAL system prompt block)**: explicit guidance that `set_reserve(high_target)` starts charging immediately because `backup_reserve_percent > soc` triggers the Powerwall. When waiting for a cheaper window, leave reserve at 5% unless the survival projection fails. Formula: `projected_soc = soc − (hours_to_window × home_load_kw / 13.5 × 100)`. If projected > 5%: no action. If projected ≤ 5%: set reserve to drain + 8% only.
- **5% survival floor (replaces 20% threshold)**: the 20% Minimum Battery Threshold is the floor for intentional discharge decisions (arbitrage, normal operation), NOT a pre-cheap-window top-up target. Rule 1 and Rule 7 Step 1 rewritten in `energy_rules.md` to use the projection formula. Battery is allowed to drain toward 5% while waiting for Solar Sponge.

Key agent capabilities added 2026-06-05:
- **`_demand_reserve_guard_fired` NameError fixed**: variable was set inside `run_agent()` but never initialised at module level. Caused every `log_decision()` call to crash silently since session 6 (Jun 2), breaking JSONL writes, HA notifications, logbook, and dashboard helpers. Fix: one-line module-level initialisation.
- **`battery_grid_charge_target` 85% floor (peak months)**: template sensor in `configuration.yaml` now clamps to 85% minimum in peak months before 3pm. Previously returned 13% on a cloudy day (Solcast-optimistic), which caused `battery_autonomous_revert_target_reached` to fire immediately after autonomous mode was set (battery already above 13%). Now the automation correctly waits until 85% is reached.
- **Wait-and-go-hard strategy (Rule 22)**: `_cheapest_go_hard_slot()` scans price forecast each cycle for the cheapest slot where `hours_until + fill_fast_85h + 0.5h ≤ deadline`. If a slot ≥1¢ cheaper than current price exists and is feasible: `wait_for_cheap_go_hard` (hold). If no cheaper slot: `peak_charge_now` (self_consumption now). `go_hard_slot` exposed in REFERENCE block and JSONL.
- **Receding horizon Solar Sponge rate selection (Rule 23)**: once in Solar Sponge with grid charge needed, mode is recalculated every cycle. Autonomous only when `fill_slow_85h ≥ deadline − 1h`. Otherwise self_consumption — next cycle will reassess as solar updates. Every cycle is an independent optimization; mode is never preserved from previous cycle.

Key agent capabilities added 2026-06-23 (session 14):
- **Rules 24 & 25 — peak survival charge/wait**: if battery projected to drain below 5% before Solar Sponge, either charge now (`peak_solar_cover_survival`) or wait for Sponge if ≤3h away and ≥5¢ cheaper (`peak_survival_wait_for_sponge`). Addresses Jun 23 case: battery drained to 8% at 7am and emergency-charged at 42¢.
- **Phase 2.5-A — charge rate model**: `agent/model_params.json` built from 17 days of `energy_log.db`. SoC-dependent rates (1.66 kW at 60%, 0.876 kW at 80%, 0.625 kW at 90%). `_avg_charge_rate_kw()` replaces flat 1.7 kW in all fill-time calculations. Autonomous mode still uses flat 5.0 kW (no data yet).
- **LP solar_unreliable fix**: `optimizer.py` zeros solar series when `state['solar_unreliable']=True`. Stops LP from firing `mpc_solar_only` on cloudy mornings (source of all LP divergences in prior analysis).
- **Phase 7 — selective narrative**: routine hold cycles skip LLM API call; `_build_auto_summary()` writes `[auto]` entry directly. ~60-70% of cycles now skip the LLM.
- **EV notification fix**: `log_decision()` always reads `sensor.polestar_7853_battery_charge_level` for EV SoC (was conditional on plug state); EV notification now always shows EV SoC + plug status, never battery SoC.
- **Emergency automation hardened**: `battery_low_soc_emergency_charge` now has 20¢ absolute price ceiling + hardcoded 85% reserve target in peak months before 3pm. **Needs HA reload.**
- **Demand window warning debounced**: `for: "0:01:00"` added to both warning automation triggers. **Needs HA reload.**
- **86 unit tests** (was 75).

Key agent capabilities added 2026-06-03:
- **Home load deduction in solar sufficiency check**: `compute_decision_context()` now computes `net_expected_solar = max(expected_solar - home_load_kw * window_h, 0)` and uses it in `kwh_needed_85`. Fixes the bug where `peak_target_met` fired at 25% SoC on sunny-forecast days because raw Solcast remaining was used without deducting home consumption.
- **`peak_solar_will_cover` rule**: renamed from `peak_target_met` when SoC < 85%. The two cases are semantically distinct: one means the battery actually reached target; the other means the solar projection covers the remaining gap.
- **`solar_will_cover` rule (non-peak)**: if reliable solar forecast (net of home load) covers the gap to cost_target before 1pm, the deterministic layer holds. Encodes the correct default: on a sunny forecast day, hold-until-you-must rather than trickle-charge. Escalation fires if solar underdelivers as the day progresses.
- **LP horizon extension**: `_build_hourly_price_model()` computes per-hour-of-day median prices from the last 7 days of decisions.jsonl. `_extend_forecast_to_demand_window()` appends synthetic 30-min slots from the end of the Amber ~6h forecast to 22:00. The LP now sees the 15:00–21:00 demand-window block and applies `demand_penalty_c = 1000 ¢/kWh` on those slots — fixing the systematic `mpc_solar_only` divergence on peak mornings where the demand window was beyond the Amber horizon.
- **`daily_energy.jsonl` schema**: `solar.accuracy` renamed to `solar.forecast_vs_actual_ratio`; `agent.forecast_accuracy_category` added ("good"/"poor"/"unreliable") — key predictor for learning agent of demand-window breach risk.
- **68 decision tests, 12 optimizer tests** — all pass.

Key agent capabilities added 2026-06-01:
- **Overnight hold (Rule 20)**: `overnight_hold` flag — when nighttime (20:00–07:00) AND price > 10¢ AND SoC > 25%, hold and wait for Solar Sponge rather than charging at overnight rates. `SOLAR_SPONGE_PRICE_THRESHOLD = 10¢` constant controls the threshold. Fires before deferral_limit so it can't be overridden by repeated holds. 60 unit tests.
- **Battery Forecast card fixes**: evening mode now shows charging status when active (was always showing "solar done · discharging"); goal/projected section hidden after 3pm; reserve now reads Tessie only (was showing stale agent helper value).
- **LP optimiser shadow layer (`agent/optimizer.py`, NOT in control path)**: a third decision layer. A pure receding-horizon LP (scipy HiGHS) reads the same state + price + solar forecasts and emits a verdict in the same `{action, target_pct, mode, rule_fired}` shape as `compute_decision_context()`. Demand-window protection is a heavy import penalty 3–9 pm (peak months), not a fixed 85%/2:55pm rule, so it pre-charges exactly enough to cover the evening load. `run_agent()` computes it in a separate try/except (cannot affect control); `log_decision()` writes `optimizer_verdict`, `optimizer_context`, `optimizer_action_match` (vs LLM), `optimizer_vs_deterministic` to `decisions.jsonl` — a three-way A/B (LLM vs deterministic vs LP) per cycle. Guarded by `_HAVE_OPTIMIZER`. 9 tests in `agent/test_optimizer.py`. Rationale: PRODUCT.md "Optimisation Engine — Depth".

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
- **60 unit tests** in `agent/test_decision.py`.

Key agent capabilities added 2026-05-29:
- **Short-term memory**: last 3 decisions from `decisions.jsonl` injected into every cycle. Agent can detect stateless deferral (holding 2+ cycles for a cheap window that never arrives).
- **Deferral limit**: if 2+ consecutive holds + price within 2¢ of prior cycles → flat-then-spike, charge now.
- **Time-based escalation (Rule 13)**: peak month hard deadline maths every cycle from 9am; non-peak soft deadline via `hours_to_cheap_end`.
- **`hours_to_cheap_end`**: replaces `hours_to_spike` (first price > 30¢). LLM-facing definition (system prompt) is the first *sustained* +4¢ rise. The deterministic shadow layer now uses an improved **scale-free daily-shape** version (bottom-30% band of the day's trough→evening-peak swing, with a 5¢ flat-day guard) — fixes under-reporting on gradual ramps (see below).
- **Deterministic decision layer + shadow mode (added 2026-05-29, NOT in control path)**: `compute_decision_context()` is a pure function that reproduces the agent's arithmetic (deadline maths, fill times, spread, zero-solar/deferral detectors, effective cost target) and emits a recommended verdict `{action, target_pct, mode, rule_fired}` via an ordered decision tree. Each live cycle it's computed and injected into the prompt as a *reference only* block; both the LLM's actual decision and the computed verdict are logged to `decisions.jsonl` (`computed_verdict`, `shadow_action_match`, `shadow_mode_match`) for divergence measurement. Covered by `agent/test_decision.py` (60 unit tests). Plan: collect divergence through the first June peak week → cutover with kill-switch → slim the prompt.
- **Solar zero-override**: if actual solar = 0 kW in 2+ of last 3 daylight cycles, treat as zero-solar day regardless of Solcast/Open-Meteo forecasts. Evidence beats model predictions.
- **Solar Sponge minimum floor (Rule 14)**: 10am–1pm, SoC < 50% → always charge to 50%, spread table irrelevant.
- **Price risk asymmetry**: evening prices have fat right tail — Solar Sponge charging is insurance, not arbitrage.

**Layer 3 — Rules** (HA automations, always active): hard constraints that fire deterministically regardless of agent decisions. React in seconds. Cannot be overridden by the agent. Handle safety, demand window, export guard, and edge cases.

---

## Current automation status (as of 2026-06-09)

**25 automations** in `config/automations.yaml` — **13 active (safety/monitoring), 12 disabled (agent handles)**

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
| `battery_low_soc_emergency_charge` | Charge if critically low + cheap price + price ≤20¢ + **NEEDS RELOAD** (20¢ ceiling + 85% target in peak months added 2026-06-23) |
| `solar_inverter_underperformance_alert` | Alert when inverter under-produces vs Solcast |
| `ev_plugged_in_notify` | Alert when EV connects with SoC/price snapshot |
| `sensor_watchdog_morning` | 09:30 daily: checks 8 sensors for unavailable/stale (>2h), sends persistent notification |

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
- Price spike arbitrage (Rule 10) — deprioritised; demand window conflict makes it rarely viable (see energy_rules.md Rule 10)

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
| `agent/test_decision.py` | 60 unit tests for `compute_decision_context()` — pure, no API calls, run in ms |
| `agent/optimizer.py` | LP/MPC optimiser (shadow only) — receding-horizon scipy LP; verdict shape matches the deterministic layer for three-way A/B. See PRODUCT.md "Optimisation Engine — Depth" |
| `agent/test_optimizer.py` | 9 unit tests for the LP optimiser — pure, no API calls |
| `agent/.env` | API keys (gitignored — not in repo) |
| `agent/agent_decisions.log` | Plain-text decision log (one line per cycle, committed to git) |
| `agent/decisions.jsonl` | Structured JSON decision log — full context per cycle, foundation for analyst agent and accuracy tracking |
| `agent/log_daily_energy.py` | Daily (21:05 cron) energy journal → `daily_energy.jsonl`. Comprehensive: solar forecast/actual, price by window, battery trajectory, grid import/export, demand window pass/fail (billing-accurate), agent decision rollup. Reads HA history API + decisions.jsonl, no HA config change |
| `agent/daily_energy.jsonl` | Durable per-day energy record (survives HA recorder rolloff) — source of truth for dashboard cards and future learning agent |
| `agent/demand_window_summary.py` | Pushes `sensor.demand_window_monitor` into HA via REST API (month peak kW + rolling per-day history). Reads daily_energy.jsonl. Crons: 21:05 + hourly. Feeds two Markdown dashboard cards |
| `agent/data_logger.py` | Closed-loop SQLite logger — one row per cycle in `energy_log.db`. Foundation for self-calibrating models (Phase 2.5-A+). Wired in 2026-06-06. |
| `agent/energy_log.db` | SQLite DB on Pi only (gitignored). Accumulates state + price forecasts + decisions each cycle. Inspect: `ssh energypi.local "agent/venv/bin/python home-energy-agent/agent/data_logger.py"` |

---

## What to watch for

**June 1 demand window — PASSED ✅ (2026-06-01).** Agent correctly held overnight (Rule 20), charged via Solar Sponge 09:30–14:30 (39%→96% at 7–11¢), entered demand window at 99% SoC, zero grid imports 3–9pm. Rule 2 maintained. Backstop automation did not need to fire.

**June 2 demand window — PARTIAL BREACH ⚠️ (2026-06-02).** SoC reached 81% (target 85%). `battery_pre_demand_window_reset` (2:55pm automation) errored silently — `rest_command` had failed to load at the June 1 HA restart due to a truncated payload. Reserve stuck at 80% all evening; battery couldn't discharge; grid covered cooking load at 7pm (~2.7 kW peak 30-min import). Fixed: Tessie API direct call + HA restart. **Agent pre-flight demand-window reserve guard now prevents recurrence** — drops reserve to 5% via Tessie directly at the start of every demand-window cycle, independent of HA.

**Jun 5 demand window — passed.** Battery charged autonomously from 28% after 9:21am. `battery_grid_charge_target` floor fix deployed live. Receding horizon rules active.

**Jun 6 premature charging bug — fixed (2026-06-06).** Agent set reserve=85% at 8:30am while intending to hold for Solar Sponge (10am, 1.5h away). Root cause: 20% MBT threshold incorrectly used as pre-cheap-window floor. Fixed: hold ≠ arming CRITICAL block in system prompt; projection formula replaces threshold check; 5% survival floor only. Also fixed: Tessie SoC=0% hiccup caused reserve to be set to 80% overnight, resulting in unnecessary 14¢ charging at 7am. Fixed with `_build_battery_state()` gateway fallback.

**LP optimiser horizon extension — done (2026-06-03).** `_build_hourly_price_model()` + `_extend_forecast_to_demand_window()` added. LP now sees 15:00–21:00 demand-window block with 1000¢/kWh penalty.

**Phase 5 complete (2026-06-06)** — `DETERMINISTIC_AUTHORITATIVE = True`. Deterministic layer now drives control. LLM is narrative-only. LP optimiser remains shadow for divergence tracking.

**Phase 6 complete (2026-06-09)** — system prompt slimmed from ~470 lines to ~65 lines (86% reduction). All decision arithmetic removed. LLM prompt explicitly states it is a narrative logger only; `set_*` calls are no-ops.

**Session 13 fixes (2026-06-10)**:
- **LLM narrative fix — FIT/EV confusion**: LLM was citing FIT (feed-in tariff) as the reason for Zappi mode selection. FIT is irrelevant to EV charging except Case 6 (negative-FIT solar dump). `SYSTEM_PROMPT` updated: EV cases block now explicitly restricts FIT reference to Case 6 only, and prohibits citing FIT for standard mode selections.
- **LLM narrative fix — spread definition**: LLM was defining `spread_c` as `import_price − FIT` (buy vs sell). `spread_c` is `current_import_price − forward_min_c` (buy now vs buy later). `SYSTEM_PROMPT` updated: explicit CRITICAL block added defining spread correctly and prohibiting the FIT-based definition. Root cause of both errors: Phase 6 prompt slim left `fit_price_cents_kwh` visible in state with no definition of spread, so LLM latched onto FIT as the nearest available price variable.
- **Rule 10 (price spike arbitrage) deprioritised**: decided not to build as a manual rule — demand window conflict makes it rarely viable. `energy_rules.md` and `todo.md` updated.

**Session 12 fixes (2026-06-09)**:
- Race condition between `battery_low_soc_emergency_charge` automation and det layer HOLD fixed: automation no longer has 20% minimum floor; HOLD verdict now unconditionally clears reserve to 5%.
- `peak_deadline_autonomous` false positives fixed: now checks `price <= forward_min` before escalating to autonomous (was firing during Solar Sponge when we were already at the cheapest price).
- data_logger double-insert fixed: `_cycle_context["db_cycle_id"]` guard prevents second `log_cycle_start()` call per cycle. 141 orphaned rows cleaned from Pi DB.
- `sensor_watchdog_morning` automation added: checks 8 sensors at 09:30 for staleness/unavailability.

**Tesla app backup reserve — set to 5% (2026-06-07).** Previously 80%, which caused the reserve to drift back to 80% whenever Tessie's cloud command didn't fully persist to Powerwall hardware. Now 5% is the firmware fallback — safe. The pre-flight guard and `_guarded_set_reserve()` override upward as needed.

**HA automation YAML vs UI discrepancy**: automations.yaml has no `enabled: false` entries — all 21 battery automations show as enabled in the file. HA UI enable/disable state is stored separately in HA's internal storage. Confirmed via HA UI: `battery_winter_overnight_precharge` and `battery_cloudy_day_topup` are disabled. The other 10 "agent handles" automations need verification in HA UI. Most critical to confirm disabled: `battery_cheap_window_autonomous_charge` (sets reserve=100% when Amber cheap window opens).

**LP solar_unreliable gap** — LP still doesn't consume the `solar_unreliable` flag. On cloudy mornings LP would hold while det correctly charges. Not a control issue now (det is authoritative), but blocks LP-authoritative cutover if/when that's pursued.

**Historical price model** — first live run was 2026-05-31. Watch `cost_target_method: historical` in JSONL. p25/p75 will shift as June peak-month prices accumulate. May need to tune `CHEAP_BAND_ALPHA` and `MIN_DAILY_SWING`.

**On rainy/cloudy peak days**: Solar won't cover the deficit. Agent must escalate to autonomous during the cheap window (10am–2pm). At 1.7kW self_consumption rate there may not be enough time — autonomous (5kW) is needed. Watch Rule 16 (`nonpeak_solar_unreliable_autonomous`) firing correctly.

**Monitoring questions:**
- Does overnight_hold (Rule 20) prevent high-price overnight charging each night?
- Does agent correctly escalate to autonomous on a cloudy peak morning?
- Does `battery_autonomous_export_safety_net` catch any misbehaviour within 30s?
- **Three-way shadow**: does the LP (once horizon is fixed) agree with LLM+rules on peak-day pre-charge decisions? Review via `/morning`.

---

## The bigger picture

The system now has four layers:
1. **Intent** — goals encoded in the agent system prompt (minimise cost, no demand charges, EV never from battery)
2. **Deterministic rule layer** — `compute_decision_context()` drives all control actions every 30 min (authoritative since 2026-06-06)
3. **LLM narrative** — Claude reads state and writes the log entry; its `set_*` calls are no-op'd
4. **HA automations** — hard constraints that fire independently (demand window, export guard, emergency)

This is a working prototype of what Sol will productise. The agent replaces the brittle rule-based approximations with genuine look-ahead reasoning. The rules layer stays — some constraints must be deterministic and fast regardless of the reasoning layer above.

**When something behaves unexpectedly:**
- Check `/tmp/energy_agent.log` for the agent's decision narrative — it explains its reasoning
- Check HA Activity (logbook) for the timeline of mode/reserve changes
- Check `energy_rules.md` for the underlying business logic
- The safety automations (export guard, demand window reset) fire independently — check HA automations if the agent seems to be overridden

The Sol product (`/Users/simonmonk/Simon Projects/Home Energy Console/`) will eventually replace the agent with a proper MPC solver, but the three-layer architecture (intent → optimiser → safety rules) remains the design.
