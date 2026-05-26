# Project To-Do List

## Personal — Home Automation

### Energy Agent (in progress — observing since 2026-05-25)

- [ ] **Run manually for a few days** — build confidence in agent decisions before scheduling
- [ ] **Fix price forecast** — `get_price_forecast()` returns empty; find correct Amber sensor attribute key
- [ ] **Schedule via cron** — bake API key into crontab (not env var), handle Mac sleep
- [ ] **Verify overnight behaviour** — does agent correctly decide to pre-charge at cheap overnight prices? Check morning logs
- [ ] **June 1 demand window** — verify agent handles peak month logic correctly (no grid import 3–9pm)
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

## Done ✅

- [x] Install Solcast integration via HACS for solar forecasting
- [x] Add Solcast-aware cloudy day detection to morning charge trigger (Rule 9)
- [x] Dynamic grid charge target — shortfall=0 drives reserve instead of fixed 100%
- [x] Emergency low SoC automation — pre-9:30am gap-filler
- [x] Reactive cheap-window trigger — charging starts immediately when window opens
- [x] Autonomous mode banned — confirmed exports at 4¢ while buying at 11¢
- [x] True SoC sensor via Tessie live_status — replaces floor-clipped gateway reading
- [x] 30-min averaged home load — smooths stove/kettle spikes from forecast

## Product Design — Battery Control Service

- [ ] **Savings dashboard — "what did this cost me without the agent?"** — core product metric. Show daily/weekly/monthly $ saved vs a naive baseline (e.g. always charging at flat rate, no demand window management, no solar optimisation). Broken down by: demand charge avoided, cheap-window vs peak charging differential, solar self-consumption gain. User should see "this week the agent saved you $34" front and centre — not buried in a notification. Key insight from user research: people paying for a service need visible proof of value, not just operational logs. Consider: daily summary notification (not every cycle), a persistent dashboard card, and a monthly email/report. Also relevant: "kWh of additional battery life preserved" as an alternative metric for users who care about hardware longevity over cost.

- [ ] **Migrate energy agent to Anthropic Managed Agents** — once local version is stable; solves Mac-sleep scheduling problem via hosted infrastructure; MCP Tunnels enables reaching HA (localhost) securely; "Dreaming" feature could allow agent to self-improve from past decisions; relevant as Sol infrastructure layer for multi-tenant deployments ($0.08/session-hr + tokens)

- [ ] Define service concept — multi-battery cloud control with dynamic tariff awareness
- [ ] Research MPC architecture for multi-tenant battery optimisation service
- [ ] Register as Tesla Fleet API developer — path to direct access without Tessie
- [ ] Investigate multi-battery API support (Sonnen, BYD, etc.)
- [ ] Investigate multi-tariff support (Amber AU, Octopus UK, Tibber EU)
