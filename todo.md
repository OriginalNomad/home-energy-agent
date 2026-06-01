# Project To-Do List

## Personal — Home Automation

### Energy Agent (in progress — observing since 2026-05-25)

- [x] **Run manually for a few days** — agent running on cron since 2026-05-29, confidence building through June peak week (2026-06-01)
- [x] **Fix price forecast** — was not empty (stale note); Amber sensor returns mixed 5-min + 30-min intervals. Now resampled to uniform 30-min buckets so deadline/spread maths is valid. Fail-loud warning on empty. (2026-05-29)
- [x] **Schedule via cron** — cron job running, API key baked in, Mac Studio with sleep disabled is sufficient (2026-05-31)
- [ ] **Verify overnight behaviour** — overnight_hold rule now in place (don't charge >10¢ when Solar Sponge is coming). First live test tonight June 1 — confirm agent holds overnight and charges during Solar Sponge tomorrow morning
- [ ] **June 1 demand window** — verify agent handles peak month logic correctly (no grid import 3–9pm) ← **TODAY, first live test** — check 3pm SoC and demand window behaviour in morning log
- [ ] **Re-architecture Phase 4 — collect shadow divergence** *(through first June peak week)*: shadow mode now logs LLM vs deterministic verdict each cycle. Review via the `/morning` shadow-layer section; tag each divergence as deterministic-layer bug vs LLM over/under-cautious. Goal: enough data to trust (or fix) the deterministic layer before cutover.
- [ ] **LP optimiser — extend price horizon to 24–48h** *(blocker for trusting the optimiser)*: shadow LP (`agent/optimizer.py`) under-charges because Amber's ~6h forecast never reaches the evening demand-window peak, so the LP sees a flat-cheap window and holds. Fix: synthesise evening/overnight prices from the historical price model (p25/p75 by time-of-day) to extend the horizon, OR apply the 3–9pm import penalty to the whole block regardless of forecast length. Found via `agent/three_way_review.py` on 2026-06-01: back-filled optimiser held 0/77 cycles (caveat: back-fill overstates solar). Solar-quantile `risk` knob is a second-order fix only.
- [ ] **LP optimiser — extend price horizon to 24h (June 2, NEXT)**: synthesise prices beyond Amber's ~6h by appending p25/p75-by-hour from the existing `load_price_history()` data. Target slots 15:00–21:00 specifically — exposing all 6h of the demand window makes the pre-charge arbitrage obvious to the LP. Add a test case: cloudy peak morning with zero solar, LP should switch from hold to charge.
- [ ] **LP optimiser — add `--live-only` flag to `three_way_review.py` (June 3)**: filter to records where `optimizer_verdict` is present in the JSONL (not back-filled), re-run agreement analysis. Live records only have real solar forecasts; back-fill is contaminated by `_synth_solar_from_record()` overstating generation.
- [ ] **Re-architecture Phase 4b — three-way divergence watch** *(through first June peak week)*: `agent/three_way_review.py` now reports LLM vs deterministic vs LP optimiser. Live `optimizer_verdict` accumulates from 2026-06-01 11:00 cron onward; review via `/morning`. Tag divergences (a) bug / (b) LLM caution / (c) optimiser trusts point forecast. Goal: enough data to decide which shadow layer to trust at cutover.
- [ ] **Re-architecture Phase 5 — cutover to LP-authoritative (target June 4)**: add `OPTIMIZER_AUTHORITATIVE = False` flag to top of `energy_agent.py`. When True: LP verdict drives control commands; deterministic layer overrides on higher-urgency rules; LLM runs for narrative only, `set_*` tool calls are no-op'd. Flip to True once 48h of live LP data shows correct peak-day pre-charging. Kill-switch: flip back to False.
- [ ] **Re-architecture Phase 6 — slim the prompt**: once deterministic layer is authoritative, remove the arithmetic the LLM no longer needs to do in its head; unify the LLM-facing `hours_to_cheap_end` prose onto the scale-free model.
- [ ] **Re-architecture Phase 7 — selective narrative**: once Phase 6 is done, consider dropping the LLM call on routine cycles (hold overnight, target_met) and only calling it when a high-stakes rule fires (autonomous, peak_deadline_autonomous) or the verdict changes from the previous cycle. Normal cycles log `rule_fired` + numbers to JSONL only; interesting cycles get the full narrative. Reduces cost from ~$97/month to near-zero without losing visibility on decisions that matter.
- [ ] **Tune `α` / `MIN_DAILY_SWING`** in `_hours_to_cheap_end` against June peak-month forecasts (larger swings than the flat May days the 0.30 / 5¢ first-pass was set on).
- [ ] **Tune historical price model** — `CHEAP_BAND_ALPHA`, `MAX_INSURANCE_FLOOR`, `PRICE_HISTORY_DAYS` after first week of June peak-month data accumulates. May need to raise insurance floor or adjust p25/p75 thresholds for larger winter price swings.
- [ ] **Tune overnight hold threshold** — `SOLAR_SPONGE_PRICE_THRESHOLD = 10¢` is a first-pass value. If Solar Sponge prices are regularly above 10¢ in winter, raise it. Review after first week of June data.
- [ ] **Verify Zappi "Eco" mode string** — confirm myenergi integration accepts exactly `"Eco"` (verified in HA States as of 2026-05-31, but worth checking after any integration updates).
- [ ] **Set initial values on new HA sliders** after HA restart: `ev_ultra_cheap_threshold_c=6`, `ev_eco_gap_c=1.5`, `battery_charge_price_threshold_c=12`, `battery_max_insurance_floor_pct=70`.
- [x] **Sliding forecast detector** — implemented `_detect_sliding_forecast()`, fires `rule_fired: "sliding_forecast"` after 3+ cycles of phantom cheap window. (2026-05-31)
- [ ] **Sliding forecast display** — expose forecast snapshot data from `decisions.jsonl` as HA sensor so past forecasts can be overlaid on the Amber price chart, making sliding visible.
- [x] **Consider moving agent into HA** — Mac Studio with sleep disabled + cron job is sufficient; no need to move into HA

- [x] **Add InfluxDB** — pipe HA sensor history into InfluxDB for long-term retention and analysis (default SQLite rolls off after 10 days)
- [ ] **InfluxDB dashboards & reports** — set up Data Explorer queries and dashboards for battery SoC history, charging patterns, price vs SoC correlation, daily 3pm SoC outcomes
- [ ] **Add structured decision log** — timestamped event log every time an automation fires: `{timestamp, automation_id, trigger, conditions, action, soc, price, solar_forecast}` — foundation for future ML
- [ ] Monitor battery automations over several days and review rule-set *(in progress — June 1 demand window is first real test)*
- [ ] Review Tessie ~A$10/month cost vs savings achieved — cancel if not justified
- [ ] Replace Tessie with direct Tesla Fleet API (personal OAuth) — eliminate $10/month fee
- [ ] Build EV charging automation (Rules 4 & 5)
- [ ] Test Polestar 4 SoC and charging state sensors alongside Zappi entities
- [ ] Investigate Daikin AC integration for HA
- [ ] Build Rule 10 price spike arbitrage (>50¢ feed-in threshold)
- [ ] Add Solcast forecast change trigger to solar sponge check — update reserve immediately when Solcast revises, not just at next 30-min tick
- [ ] Confirm `sensor.solcast_pv_forecast_power_now` entity name in HA States (used in inverter underperformance alert)
- [ ] Confirm SolarEdge inverter AC rated output for Solcast config (check label on inverter box)
- [ ] **BOM solar forecast investigation** — Solcast and Open-Meteo have both been unreliable (today: forecast solar arriving from 11am, actual zero all day). Investigate BOM API (api.weather.bom.gov.au) for official gridded solar radiation forecasts. Track Solcast vs SolarEdge actuals in InfluxDB to quantify accuracy. Goal: replace or weight-adjust Solcast with a more reliable source, or build a "forecast confidence" metric from the accuracy history.
- [ ] **Daily 3pm accuracy review** — `decisions.jsonl` now logs `goal_3pm_soc`, `projected_3pm_soc`, and daily accuracy records (actual vs projected at 3pm). Build InfluxDB dashboard showing forecast accuracy over time — which morning cycle hours had best projection accuracy, how often projection was optimistic vs pessimistic.
- [ ] **Amber price forecast accuracy + risk premium derivation** *(needs ~4 weeks of data — build in late June/July)*: for each JSONL record, reconstruct the 12 forecasted times from `ts` + 30min intervals, pull actual prices from InfluxDB, compute signed error (actual − forecast) per slot. Bucket by time-of-day: Solar Sponge (10am–3pm), evening (4pm–9pm), overnight. Build error distribution per bucket — mean, 75th and 90th percentile. The 75th percentile error for evening slots becomes the empirical risk premium to add to the spread table, replacing the current gut-feel 5¢ threshold. This makes the spread threshold data-driven rather than assumed.

## Done ✅

- [x] Install Solcast integration via HACS for solar forecasting
- [x] Add Solcast-aware cloudy day detection to morning charge trigger (Rule 9)
- [x] Dynamic grid charge target — shortfall=0 drives reserve instead of fixed 100%
- [x] Emergency low SoC automation — pre-9:30am gap-filler
- [x] Reactive cheap-window trigger — charging starts immediately when window opens
- [x] Autonomous mode banned — confirmed exports at 4¢ while buying at 11¢
- [x] True SoC sensor via Tessie live_status — replaces floor-clipped gateway reading
- [x] 30-min averaged home load — smooths stove/kettle spikes from forecast
- [x] Peak-month backtest harness (`agent/backtest.py`) — validate demand-window logic before June 1 (2026-05-29)
- [x] SoC-sensor trust bug fixed — agent was judging "target met" off the floor-clipped gateway; guidance added to system prompt + Rule 6 (2026-05-29)
- [x] Deterministic decision layer (`compute_decision_context`) + 28 unit tests (`agent/test_decision.py`) (2026-05-29)
- [x] Shadow mode wired in — logs LLM + deterministic verdict per cycle to `decisions.jsonl` (2026-05-29)
- [x] `_hours_to_cheap_end` rewritten as scale-free daily-shape model — fixes gradual-ramp under-reporting (2026-05-29)
- [x] `/morning` review extended with shadow-layer analysis section (2026-05-29)

## Product Design — Battery Control Service

- [ ] **Savings dashboard — "what did this cost me without the agent?"** — core product metric. Show daily/weekly/monthly $ saved vs a naive baseline (e.g. always charging at flat rate, no demand window management, no solar optimisation). Broken down by: demand charge avoided, cheap-window vs peak charging differential, solar self-consumption gain. User should see "this week the agent saved you $34" front and centre — not buried in a notification. Key insight from user research: people paying for a service need visible proof of value, not just operational logs. Consider: daily summary notification (not every cycle), a persistent dashboard card, and a monthly email/report. Also relevant: "kWh of additional battery life preserved" as an alternative metric for users who care about hardware longevity over cost.

- [ ] **Migrate energy agent to Anthropic Managed Agents** — once local version is stable; solves Mac-sleep scheduling problem via hosted infrastructure; MCP Tunnels enables reaching HA (localhost) securely; "Dreaming" feature could allow agent to self-improve from past decisions; relevant as Sol infrastructure layer for multi-tenant deployments ($0.08/session-hr + tokens)

- [ ] **Analyst agent — rules improvement loop** — a second agent that runs daily/weekly (not every 30 min) to review the full decision log + actual outcomes from InfluxDB (SoC at 3pm, grid import events, cost vs baseline). Identifies systemic gaps: *"on cloudy days the agent consistently starts charging too late"*, *"the flat-then-spike threshold is too tight"*. Outputs proposed rule changes in plain English for human review → approved changes update the system prompt → operational agent improves. Separate from the operational agent so it doesn't pollute real-time decision context. Complement with short-term memory (last 2-3 decisions) fed into the operational agent for intra-day tracking (*"I've been charging 2 cycles, SoC rising as expected"*). This is the self-improvement loop that makes the system genuinely learn rather than just execute.

- [ ] Define service concept — multi-battery cloud control with dynamic tariff awareness
- [ ] Research MPC architecture for multi-tenant battery optimisation service
- [ ] Register as Tesla Fleet API developer — path to direct access without Tessie
- [ ] Investigate multi-battery API support (Sonnen, BYD, etc.)
- [ ] Investigate multi-tariff support (Amber AU, Octopus UK, Tibber EU)
