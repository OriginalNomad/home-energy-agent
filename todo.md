# Project To-Do List

## Personal — Home Automation

### Energy Agent (in progress — observing since 2026-05-25)

- [x] **Run manually for a few days** — agent running on cron since 2026-05-29, confidence building through June peak week (2026-06-01)
- [x] **Fix price forecast** — was not empty (stale note); Amber sensor returns mixed 5-min + 30-min intervals. Now resampled to uniform 30-min buckets so deadline/spread maths is valid. Fail-loud warning on empty. (2026-05-29)
- [x] **Schedule via cron** — cron job running, API key baked in, Mac Studio with sleep disabled is sufficient (2026-05-31)
- [x] **Verify overnight behaviour** — confirmed June 1/2: agent held overnight (Rule 20), SoC drained 72%→36% without charging, Solar Sponge charged correctly in the morning. (2026-06-01/02)
- [x] **June 1 demand window** — PASSED ✅ Battery at 99% entering 3pm demand window, zero grid imports 3–9pm, agent held correctly throughout. Overnight_hold also validated. (2026-06-01)
- [x] **June 2 demand window** — PARTIAL BREACH ⚠️ rest_command broken since June 1 restart → 2:55pm automation failed silently → reserve stuck at 80% → grid import during cooking at 7pm. Fixed manually via Tessie API + HA restart. rest_command now working.
- [x] **Agent health check for HA rest_commands** — added to `run_agent()` pre-flight: checks `/api/services` each cycle, warns loudly if `rest_command` domain missing. (2026-06-02)
- [x] **Agent demand-window reserve guard** — added to `run_agent()` pre-flight: if peak month + in demand window (15–21h) + reserve > 10%, drops reserve to 5% via Tessie directly before the LLM runs. Bypasses HA rest_commands entirely. (2026-06-02)
- [x] **`_demand_reserve_guard_fired` NameError fixed** — broke JSONL + HA notifications + dashboard helpers since Jun 2. One-line fix. (2026-06-05)
- [x] **`battery_grid_charge_target` 85% floor** — was reverting autonomous mode immediately on cloudy days (sensor returned 13% from Solcast-optimistic formula). Peak-month floor added to `configuration.yaml`. (2026-06-05)
- [x] **Wait-and-go-hard charging strategy (Rule 22)** — `_cheapest_go_hard_slot()` scans forecast each cycle; `wait_for_cheap_go_hard` holds for cheaper slot; `peak_charge_now` charges when no better slot. (2026-06-05)
- [x] **Receding horizon Solar Sponge rate selection (Rule 23)** — mode recalculated every cycle in Solar Sponge; autonomous only when `fill_slow_85h ≥ deadline − 1h`; otherwise self_consumption. (2026-06-05)
- [x] **Hold ≠ arming fix** — agent was setting reserve=85% while "waiting" for Solar Sponge, immediately triggering charging at expensive morning prices. CRITICAL block added to system prompt: only raise reserve above 5% if projected_soc ≤ 5% at cheap window. Projection formula replaces 20% threshold throughout. (2026-06-06)
- [x] **Tessie SoC=0 guard (`_build_battery_state()`)** — Tessie occasionally returns 0% (cloud API hiccup). New function cross-references gateway; if Tessie implausible + gateway reliable (`gateway > reserve`), substitutes gateway + sets `tessie_soc_failed=True`. Prevents panic reserve increases from false empty readings. (2026-06-06)
- [ ] **Watch Tessie reliability** — `tessie_soc_failed` flag now logged in JSONL. Monitor frequency of Tessie=0 failures in `/morning` review; if recurring, investigate whether Tessie API polling rate needs to be throttled or token needs renewal.
- [ ] **Re-architecture Phase 4 — collect shadow divergence** *(through first June peak week)*: shadow mode now logs LLM vs deterministic verdict each cycle. Review via the `/morning` shadow-layer section; tag each divergence as deterministic-layer bug vs LLM over/under-cautious. Goal: enough data to trust (or fix) the deterministic layer before cutover.
- [x] **LP optimiser — extend price horizon to 24h (done 2026-06-03)**: `_build_hourly_price_model()` + `_extend_forecast_to_demand_window()` added to energy_agent.py. Appends synthetic 30-min slots to 22:00 using per-hour-of-day medians from the last 7 days of decisions.jsonl. LP now sees the 15:00–21:00 demand-window block and applies `demand_penalty_c=1000¢/kWh` on those slots. Test 7 in test_optimizer.py confirms correct pre-charge on extended horizon.
- [ ] **LP optimiser — add `--live-only` flag to `three_way_review.py`**: filter to records where `optimizer_verdict` is present in the JSONL (not back-filled), re-run agreement analysis. Live records only have real solar forecasts; back-fill is contaminated by `_synth_solar_from_record()` overstating generation. (Less urgent now that horizon extension is live — divergence pattern should shift from cause-c to something more meaningful.)
- [ ] **Re-architecture Phase 4b — three-way divergence watch** *(through first June peak week)*: `agent/three_way_review.py` now reports LLM vs deterministic vs LP optimiser. Live `optimizer_verdict` accumulates from 2026-06-01 11:00 cron onward; review via `/morning`. Tag divergences (a) bug / (b) LLM caution / (c) optimiser trusts point forecast. Goal: enough data to decide which shadow layer to trust at cutover.
- [ ] **LP optimiser — wire in `solar_unreliable` flag**: when `solar_unreliable=True`, discount Solcast `remaining_today` input by 50% (or use historical p25 for that hour). Confirmed Phase 5 blocker — LP systematically holds on cloudy mornings while LLM+det correctly charge.
- [ ] **Dynamic demand-window target**: 85% is a fixed conservative rule. On flat-price days with variable solar, a smarter target (based on expected 3-9pm load) would reduce unnecessary charging. LP naturally computes this when authoritative — lower priority until Phase 5.
- [ ] **Re-architecture Phase 5 — cutover to LP-authoritative (target: after solar_unreliable fix + 1-2 days validation)**: add `OPTIMIZER_AUTHORITATIVE = False` flag to top of `energy_agent.py`. When True: LP verdict drives control commands; deterministic layer overrides on higher-urgency rules; LLM runs for narrative only, `set_*` tool calls are no-op'd. Kill-switch: flip back to False.
- [ ] **Re-architecture Phase 6 — slim the prompt**: once deterministic layer is authoritative, remove the arithmetic the LLM no longer needs to do in its head; unify the LLM-facing `hours_to_cheap_end` prose onto the scale-free model.
- [ ] **Re-architecture Phase 7 — selective narrative**: once Phase 6 is done, consider dropping the LLM call on routine cycles (hold overnight, target_met) and only calling it when a high-stakes rule fires (autonomous, peak_deadline_autonomous) or the verdict changes from the previous cycle. Normal cycles log `rule_fired` + numbers to JSONL only; interesting cycles get the full narrative. Reduces cost from ~$97/month to near-zero without losing visibility on decisions that matter.
- [ ] **Tune `α` / `MIN_DAILY_SWING`** in `_hours_to_cheap_end` against June peak-month forecasts (larger swings than the flat May days the 0.30 / 5¢ first-pass was set on).
- [ ] **Tune historical price model** — `CHEAP_BAND_ALPHA`, `MAX_INSURANCE_FLOOR`, `PRICE_HISTORY_DAYS` after first week of June peak-month data accumulates. May need to raise insurance floor or adjust p25/p75 thresholds for larger winter price swings.
- [ ] **Tune overnight hold threshold** — `SOLAR_SPONGE_PRICE_THRESHOLD = 10¢` is a first-pass value. If Solar Sponge prices are regularly above 10¢ in winter, raise it. Review after first week of June data.
- [ ] **Verify Zappi "Eco" mode string** — confirm myenergi integration accepts exactly `"Eco"` (verified in HA States as of 2026-05-31, but worth checking after any integration updates).
- [ ] **Set initial values on new HA sliders** — HA was restarted 2026-06-02 evening; check sliders reset to defaults and re-set: `ev_ultra_cheap_threshold_c=6`, `ev_eco_gap_c=1.5`, `battery_charge_price_threshold_c=12`, `battery_max_insurance_floor_pct=70`.
- [x] **Sliding forecast detector** — implemented `_detect_sliding_forecast()`, fires `rule_fired: "sliding_forecast"` after 3+ cycles of phantom cheap window. (2026-05-31)
- [ ] **Sliding forecast display** — expose forecast snapshot data from `decisions.jsonl` as HA sensor so past forecasts can be overlaid on the Amber price chart, making sliding visible.
- [x] **Consider moving agent into HA** — Mac Studio with sleep disabled + cron job is sufficient; no need to move into HA
- [x] **Deploy agent to Raspberry Pi 5** — Pi running at `energypi.local`, agent on cron with auto git pull, Mac cron removed. (2026-06-05)
- [x] **Cloudflare Tunnel — `https://agent.sol.io`** — HA accessible remotely via Pi tunnel. Chrome, HA apps (Mac + iOS) confirmed working. (2026-06-05)
- [x] **Repo consolidation** — `home-energy-automation` deleted, `home-energy-console` renamed to `home-energy-agent`. Single repo accessible from all devices. (2026-06-05)
- [ ] **Migrate HA to Pi** — install HA via Docker on Pi, restore from Mac backup, update agent `.env` to `localhost:8123`. Simplifies architecture; no urgency while Mac Studio is always on.
- [ ] **Wire `data_logger.py` into `energy_agent.py`** — ~20 lines in 3 places (after `get_current_state`, after `get_price_forecast`, inside `log_decision`). Starts the Phase 2.5-A data collection clock.
- [ ] **Switch SD card boot to SSD** — Pi is already running from SSD (`/dev/sda2`); confirm boot order in `raspi-config` to ensure it always boots from SSD, not SD card.

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
