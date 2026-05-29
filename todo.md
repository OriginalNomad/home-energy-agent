# Project To-Do List

## Personal — Home Automation

### Energy Agent (in progress — observing since 2026-05-25)

- [ ] **Run manually for a few days** — build confidence in agent decisions before scheduling
- [x] **Fix price forecast** — was not empty (stale note); Amber sensor returns mixed 5-min + 30-min intervals. Now resampled to uniform 30-min buckets so deadline/spread maths is valid. Fail-loud warning on empty. (2026-05-29)
- [ ] **Schedule via cron** — bake API key into crontab (not env var), handle Mac sleep
- [ ] **Verify overnight behaviour** — does agent correctly decide to pre-charge at cheap overnight prices? Check morning logs
- [ ] **June 1 demand window** — verify agent handles peak month logic correctly (no grid import 3–9pm)
- [ ] **Re-architecture Phase 4 — collect shadow divergence** *(through first June peak week)*: shadow mode now logs LLM vs deterministic verdict each cycle. Review via the `/morning` shadow-layer section; tag each divergence as deterministic-layer bug vs LLM over/under-cautious. Goal: enough data to trust (or fix) the deterministic layer before cutover.
- [ ] **Re-architecture Phase 5 — cutover with kill-switch**: once divergence data supports it, let the deterministic verdict drive (LLM advisory/oversight only), behind a flag that reverts to LLM-authoritative instantly.
- [ ] **Re-architecture Phase 6 — slim the prompt**: once deterministic layer is authoritative, remove the arithmetic the LLM no longer needs to do in its head; unify the LLM-facing `hours_to_cheap_end` prose onto the scale-free model.
- [ ] **Tune `α` / `MIN_DAILY_SWING`** in `_hours_to_cheap_end` against June peak-month forecasts (larger swings than the flat May days the 0.30 / 5¢ first-pass was set on).
- [ ] **Consider moving agent into HA** — run as `shell_command` triggered by HA automation, avoids Mac sleep problem

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
