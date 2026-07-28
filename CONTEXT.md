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

## Network topology (matters for any non-energy integration)

The Pi is **dual-homed** on two separate networks:

| Interface | IP | Network | Role |
|-----------|-----|---------|------|
| `eth0` (wired) | 192.168.0.67/24 | 192.168.0.x | **Default route.** Energy segment — Powerwall gateway etc. |
| `wlan0` (WiFi) | 192.168.68.80/22 | 192.168.68.x | Home LAN — Sonos speakers, Meross plugs, general devices |

HA runs in the `homeassistant` Docker container in **`host`** network mode. Because the
**default route is eth0**, any integration relying on multicast/SSDP/mDNS discovery (Sonos,
Meross, etc.) broadcasts out eth0 and never finds devices on the 192.168.68.x WiFi side — and
advertises the unreachable eth0 IP for callbacks. **Fix pattern: seed device IPs manually and
force the callback/advertise address to `192.168.68.80`.** Sonos does this via the `sonos:`
block in `configuration.yaml` (`hosts:` + `advertise_addr`). Speakers/plugs on 68.x should have
**reserved DHCP leases** or the hardcoded IPs go stale (Hallway already drifted once — see
energy_log 2026-07-24). None of this touches the battery agent, which talks to HA at
`localhost:8123`.

---

## How the system works

**Control mechanism**: `backup_reserve_percent` via Tessie REST API (the only writable Powerwall parameter available without Tesla Fleet API access).
- Set to `100%` → Powerwall charges toward full
- Set to `20%` → normal floor, self-consumption mode
- Set to `5%` → deep discharge floor during demand window (peak months only)

**Known limitation**: Cannot command a specific charge rate. Tesla's firmware decides how aggressively to pull from grid in `self_consumption` mode. **Rebuilt 2026-07-22 from instantaneous `battery_power`** (~30 s resolution, 10 days, n=53–432/bucket) rather than 30-min SoC deltas: `self_consumption` is a tight **1.67 kW** at every bucket 0–70% (80/90% keep conservative legacy values 0.96/0.71). `autonomous` is **5.0 kW to 70%, 2.92 at 80%, 1.84 at 90%** — previously n=2–5 so the agent assumed a flat 5.0 kW and was optimistic exactly where the 2:55pm deadline is decided. `_avg_charge_rate_kw()` reads these from `agent/model_params.json`. Full rate control requires Tesla Fleet API or MPC.

**Open anomaly — 5 kW `self_consumption` grid charging (2026-07-22)**: raising `backup_reserve_percent` above SoC made the Powerwall import at a sustained **5 kW** while `default_real_mode` stayed `self_consumption`. Three events, including one triggered manually with the agent uninvolved — so it is Powerwall behaviour, not ours. Eliminated: mode switch, HA automations, Storm Watch, Amber SmartShift, reserve−SoC gap (median 1.67 kW in *every* gap bucket), SoC level, measurement artefacts. **Unexplained**: 10 days of 30-second data give a median of 1.67 kW for the same operation, with a clean date boundary at 07-22. Leading hypothesis is an overnight firmware push (`26.18.3`) — unverifiable, recorded as hypothesis. Watch whether it persists. Two earlier claims of mine about this model (a "long right tail", then "self_consumption is really 5 kW") were both **retracted** — see energy_log. **RESOLVED 2026-07-25**: firmware 26.18.3 was *confirmed* on the gateway (`site_info version`) and the 2026-07-24 experiment recovered the rate dial (gap 5→1.67 kW, 10→3.96, 20+→5). Rule 31 now chases `reserve = SoC + 6` to hold ~1.7 kW on self_consumption charges; the 5 kW slam only happens on autonomous (intended) or when the HA emergency automation fires (separate).

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

**Secrets — single source of truth is the Pi (established 2026-07-28):**
- The agent runs on the Pi, so **production secrets live in exactly one place:**
  `energypi.local:~/home-energy-agent/agent/.env`. Rotate/update keys there
  (`ssh -t energypi.local nano ~/home-energy-agent/agent/.env`). The **Mac's `agent/.env` is
  DEV-ONLY** (only `ANTHROPIC_API_KEY`, for local script runs) and now carries a banner saying so —
  editing it does **not** change what the running agent uses. That Mac/Pi mismatch is exactly what
  caused the 2026-07-26 key-expiry outage. `.env` is gitignored; `agent/.env.example` documents the
  model. Full-line `#` comments are safe in `.env`; avoid inline comments after a value.

**Tessie API credentials:**
- Regenerate the token at **tessie.com → Settings → Developer** (regenerating kills the old token
  immediately — see the two-file update + `!secret` deploy in `todo.md`).
- Energy site ID: `2252120180790091` (an identifier, not a secret)
- Token lives in **two** files on the Pi (both need the new value on rotation): the agent reads
  `TESSIE_TOKEN` from `agent/.env`; HA reads `tessie_bearer` from the **root-owned**
  `~/homeassistant/config/secrets.yaml` (`tessie_bearer: "Bearer <token>"`, edit with `sudo nano`).
  `config/configuration.yaml`'s 4 rest headers (2 `rest:` sensors + `powerwall_set_backup_reserve` +
  `powerwall_set_mode`) reference `!secret tessie_bearer` — **no token inline** (migrated + deployed
  2026-07-28 during the Tessie rotation; code scrubbed 2026-07-26). After a token change, HA's `rest:`
  sensors and `rest_command:` need `rest.reload` + `rest_command.reload` (or an HA restart) to pick up
  the new secret — a plain `deploy_ha_config.sh` reload does *not* cover those two domains.
- Endpoints: `POST /api/1/energy_sites/{id}/backup` with `{"backup_reserve_percent": N}`

**Solcast credentials:**
- API Key: stored in the HA Solcast integration config (not in this repo)
- Resource ID: `fd2e-343e-680f-b27e`
- DC capacity: 6.12 kWp, AC: ~5 kW, Tilt: 0° (flat roof)
- Integration: HACS "HA Solcast PV Solar Forecast Integration" by BJReplay

---

## ⚠️ HA config is DEPLOYED, not read in place (established 2026-07-22)

`config/` in this repo is **not** read by Home Assistant. The live instance is the
Docker `homeassistant` container **on the Pi** (`~/homeassistant/config`) — both what
the agent talks to and what `http://energypi.local:8123` shows. There is exactly one HA —
the Mac's second instance was retired 2026-07-22.

```bash
./deploy_ha_config.sh --check    # diff repo vs live — run this before trusting any
./deploy_ha_config.sh            # backup, validate, reload (no restart)
```

This was found on 2026-07-22 with live **7 weeks behind** the repo, meaning several
fixes logged as "deployed" had never run — most seriously the `battery_grid_charge_target`
85% peak floor, whose absence made autonomous mode self-cancelling on peak days. Now in
sync; `--check` reports zero drift.

---

## Infrastructure (as of 2026-07-22 — consolidated onto the Pi)

| Host | Role |
|------|------|
| **Raspberry Pi 5** (`energypi.local`, `192.168.0.67`) | **Runs everything live**: Home Assistant (Docker, `~/homeassistant/config` → `/config`), energy agent cron, cloudflared tunnel |
| **Mac Studio** (`192.168.68.70`) | Development machine only. **HA container retired 2026-07-22** (stopped, `--restart=no`). InfluxDB container still running but nothing feeds it |
| **GitHub** (`OriginalNomad/home-energy-agent`) | Single repo, auto-deployed to Pi on each cron run |

**There is one Home Assistant, and it is on the Pi.** Until 2026-07-22 a second instance
ran on the Mac with a Jun 4 config; nothing pointed at it, and its existence is how the
repo's `config/` drifted 7 weeks out of sync unnoticed. Retired with `docker stop` +
`docker update --restart=no` — reversible via `docker start homeassistant`, but don't:
two instances is the bug.

**Agent cron on Pi** (`~/home-energy-agent`): every 30 min does `{ git pull -q || true; }` then runs
agent. Code deploy = `git push` from Mac. **The pull is decoupled from the run (2026-07-24)** — a
failed or conflicted `git pull` can no longer stop the agent; it runs on whatever code is checked
out. **Nightly at 02:00** a second cron runs `build_models.py` to retrain the models.
**`agent/model_params.json` is gitignored/machine-local (2026-07-24)** — rebuilt on the Pi nightly,
never tracked; read the live model with `ssh energypi.local "cat ~/home-energy-agent/agent/model_params.json"`
(the Mac's copy is a stale snapshot). This pairing defuses the deploy hazard where a tracked,
locally-modified `model_params.json` made `git pull` abort and silently stopped the agent.
**HA config deploy**: `./deploy_ha_config.sh` (see the warning block above) — *not* git.
**Cloudflare Tunnel**: `https://agent.sol.io` → Pi cloudflared → `http://localhost:8123` (the Pi's own HA). Systemd service, Sydney edge. Verified still 200 after the Mac HA was stopped.
**HA external URL**: `https://agent.sol.io`. Trusted proxies cover localhost + Pi/Mac subnets + Docker bridge.

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

Key agent capabilities added 2026-06-27 (session 16):
- **Rule 26 — peak early morning hold** (`peak_early_morning_hold`): in the peak month block, when no cheaper slot is found in the Amber forecast and price > 10¢, fires `hold` instead of `peak_charge_now` whenever autonomous mode has ≥2h of margin (`fill_fast_85 < hours_to_2:55 - 2h`). Prevents charging into transient overnight/early-morning price spikes when Solar Sponge is still hours away. Physics-based (not clock-based): works correctly from midnight through 9:30am+ regardless of SoC. `peak_charge_now` now fires primarily when price is already at/below Solar Sponge threshold (10¢) and no cheaper slot exists. 3 tests added; 1 updated. **109 unit tests**.
- **Root cause**: 2026-06-27 5am charging at 24¢ (realized: spike from 19¢→24¢→19¢). Amber forecast at 5am showed spike continuing → `peak_charge_now` fired. Rule 26 would have held.
- **Phase 2.5-B — solar corrector + autonomous charge rate wiring**: LP optimizer (`optimizer.py`) now applies per-hour-of-day Solcast bias correction from `model_params.json["solar_correction"]` in `_build_solar_series()`. Autonomous charge rate in `optimize_battery()` now uses `_model_avg_rate_kw()` (bucket-weighted average over SoC range) from `model_params.json["charge_rate_kw"]["autonomous"]` instead of flat 5.0 kW. `energy_agent.py` loads full `model_params.json` at startup and passes it to `optimize_battery()`. New `agent/build_models.py` script builds both models from `energy_log.db` and writes `model_params.json` — **must be run on Pi once home** (autonomous data still empty; solar_correction not yet built). Commands in `todo.md`.

Key agent capabilities added 2026-06-24 (session 15):
- **Deadline rollover fix**: `hours_to_2_55` now rolls over to next day when past 2:55pm (e.g. 23:00 → 15.9h). Previously clamped to 0.0h, causing overnight autonomous escalation.
- **overnight_hold boundary fix**: `soc >= 25` (was `> 25`). At exactly 25% SoC the hold previously fell through to the bugged deadline path.
- **peak_deadline_autonomous always-autonomous fix**: when `fill_slow_85 >= hours_to_2_55`, previously used self_consumption if prices were flat. Self_consumption can't physically reach 85% in time regardless of price. Now always goes autonomous when in deadline urgency.
- **103 unit tests** (was 101; 2 pre-existing failures fixed by the autonomous fix).

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
- **Insurance floor**: `input_number.battery_max_insurance_floor_pct` — minimum SoC to lock in while prices are cheap, guards against cheap window closing early. Live value in the HA console; never restated here.
- **Sliding forecast detector (Rule 17)**: `_detect_sliding_forecast()` — if cheap window has been "1–2h away" for 3+ cycles but never arrived, treats forecast as unreliable and charges now.
- **Solar-unreliable autonomous escalation (Rule 16)**: when solar unreliable, uses 1.5h buffer instead of 0.5h for autonomous escalation — fills from grid before cheap window closes.
- **EV 3-phase progression (Rule 18)**: Eco (trickle while cheaper upcoming) → Fast (at cheapest moment) → Eco+ (target met). Thresholds user-settable via HA sliders.
- **EV Case 6 — negative FIT solar dump (Rule 19)**: FIT < 0¢ + battery ≥ 85% + EV < 100% → Eco+ to absorb surplus solar rather than paying to export.
- **FIT price read**: `sensor.1a_wigram_road_glebe_feed_in_price` now in state + JSONL.
- **Solar zero threshold raised 8am → 9am**: flat-roof panels don't produce before ~9am; zero output at 8am is expected, not a forecast failure.
- **`battery_autonomous_revert_target_reached` automation fixed**: changed from Tessie OR gateway to Tessie only — gateway floors at reserve level, causing premature revert when reserve=100%.
- **New HA sliders**: `ev_ultra_cheap_threshold_c`, `battery_max_insurance_floor_pct`. (`ev_eco_gap_c` was dropped from `configuration.yaml` at some point but left in the docs — it was real, the retired Mac HA's restore_state has it at 1.0 on 2026-06-02. `battery_charge_price_threshold_c` was never wired to anything and was deleted. Both cleaned up 2026-07-23.)
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

Key agent capabilities added 2026-07-28 (session 22):
- **Robustness — LLM narrative call fault-isolated**: the LLM loop is wrapped in try/except; on failure
  (expired key / Anthropic outage / network) the cycle no longer crashes — it degrades to the
  deterministic `_build_auto_summary` + `log_decision` fallback, so `decisions.jsonl` / dashboard /
  notifications still get written (control was never at risk; it runs earlier). Guards: `_llm_logged`
  prevents a double-write on a later-turn failure; the fallback reads `_cycle_context["decision_context"]`
  so it can't itself NameError. New JSONL field **`llm_narrative_failed`** marks degraded cycles. Fixes
  the 2026-07-26 outage where an expired key crashed the agent and blanked all logging.
- **Robustness — liveness heartbeat** (`_send_heartbeat()`, `HEALTHCHECK_URL` in the Pi's `.env`): the
  agent pings a Healthchecks.io dead-man's-switch each completed cycle (`ok` / `degraded:…`), `/fail` on
  a hard crash. No-op if the URL is unset; never raises. Alert cadence (period 30m / grace ~2h) is set on
  the check, not in code. Catches Pi-down / cron-broken / crash-loop — the one failure HA can't
  self-report. Deferred: a tighter HA-side staleness check during the 3–9pm demand window.
- **Secrets single source of truth = the Pi**, and **all three live keys rotated** (HA / Solcast /
  Tessie); Tessie's 4 `configuration.yaml` headers migrated to `!secret tessie_bearer` (0 inline tokens).
  See the "Secrets" block above.

Key agent capabilities added 2026-07-22 (session 17):
- **Manual override kill-switch**: `input_boolean.agent_manual_override`. While ON the rule layer still computes and logs its verdict (cycles tagged `manual_override` in `decisions.jsonl`, so they can be excluded from divergence analysis) but sends no commands, leaving the user's reserve/mode in place. Auto-expires after 12h (`MANUAL_OVERRIDE_MAX_HOURS`), fails open if HA is unreachable. Suppresses hold verdicts too — a hold would otherwise drive reserve back to 5% and undo the manual setting. **Does not suppress** the Rule 2 demand-window guard or the HA safety automations.
- **LP optimiser SoC fix**: from its 2026-06-01 wire-in until 2026-07-22 `optimize_battery()` received a hardcoded **50% SoC** every cycle (it read a top-level `soc_pct`; `energy_agent.py` nests it under `state["battery"]`). **All three-way divergence analysis before 2026-07-22 is void.** Fixed; `_require_soc_pct()` now raises rather than defaulting. Phase 4 divergence clock restarted 2026-07-22.
- **Phase 2.5-B activated**: `build_models.py` ran successfully for the first time (three bugs fixed to get there). Solar corrector live — Solcast over-forecasts by ~7× at 08:00 and ~6× at 09:00 in winter, converging to ~1.4× by 13:00.
- **Rule 27 — manual override** (`input_boolean.agent_manual_override`): see energy_rules.md. Suppresses the rule layer's commands (including HOLD, which would otherwise clear reserve to 5% and undo a manual setting). Auto-expires 12h, fails open. Does *not* suspend the Rule 2 demand-window guard or any HA automation.
- **`sensor.solar_forecast_corrected`**: Solcast remaining-today weighted per-hour by measured site bias from `model_params.json`, pushed each cycle. Attributes carry `today_total_kwh`, `tomorrow_kwh`, `effective_ratio` and the raw figures. Diagnostic only — nothing in the control path reads it.
- **`sensor.battery_remaining_to_full`**: template sensor, kWh to 100% using the same 13.5 kWh the agent uses.
- **Charge rate model rebuilt from instantaneous power**: self_consumption 1.67 kW flat 0–70%; autonomous 5.0 kW to 70% then 2.92 at 80%, 1.84 at 90%. The autonomous taper was previously absent (n=2–5 → flat 5.0 kW), making the agent optimistic exactly where the 2:55pm deadline is decided.
- **118 decision tests + 16 optimizer tests.**

Key agent capabilities added 2026-07-26 (session 21):
- **Rule 33 — receding-horizon deadline escalation** (`DEADLINE_GENTLE_LEAD`,
  `FAST_ESCALATE_BUFFER_H=1.5`). The peak deadline branch used to jump straight to autonomous (5 kW)
  the instant self_consumption could no longer fill the *whole* 85% gap in time (`fill_slow ≥
  hours_remaining`). On 2026-07-26 that slammed 5 kW at 10:00 with SoC 16% and ~4.9h to the deadline —
  ~3h of slack, at the worst-informed moment (winter-morning solar credit ≈ 0). Now it escalates to
  autonomous only at the fast rate's point-of-no-return (`hours_remaining ≤ fill_fast +
  FAST_ESCALATE_BUFFER_H`); below that it leads with a gentle self_consumption charge
  (`peak_deadline_gentle_lead`) and re-evaluates each cycle. The buffer *is* the demand-charge safety
  margin. **Control-path change**, kill-switch reverts to straight-to-autonomous.
- **Hold verdict reverts autonomous mode + drops reserve unconditionally.** A `hold` inheriting
  `mode=autonomous` from an earlier deadline charge could not be stopped — under 26.18.3 autonomous
  grid-charges at ~5 kW regardless of reserve, and the hold's reserve-drop was gated on
  `sensor.powerwall_backup_reserve`, which read a stale **5%** while the true setpoint was ~57%. The
  hold branch (`_execute_deterministic_verdict`) now commands `self_consumption` when the mode isn't
  already that, and forces reserve=5% (untrusting the lagging sensor) when it reverts; steady-state
  holds still send nothing. This was the direct cause of today's un-stoppable 5 kW charge.
- **Incident: expired `ANTHROPIC_API_KEY` crashed the agent every cycle** at the LLM narrative call
  (control survived — it runs before that line — but all post-crash logging died). Fixed by updating
  the key in the **Pi's** `.env` (the user had updated only the Mac's, which never syncs — `.env` is
  gitignored). Robustness follow-ups (LLM try/except, liveness alert, key-expiry ping, secrets
  single-source, Pi fallback) are logged in `todo.md`, not yet built.
- **222 decision + 16 optimizer + 11 build_models tests** (was 221/16/11).

Key agent capabilities added 2026-07-25 (session 20):
- **Rule 31 — gentle self_consumption charge controller** (`_gentle_charge_reserve()`,
  kill-switch `GENTLE_CHARGE_CONTROL`, tunable `SELF_CONS_CHARGE_OFFSET_PTS=6`). Firmware 26.18.3
  made a fixed reserve=85 pull 5 kW from any large reserve−SoC gap; the agent now chases
  `reserve = min(SoC + offset, target)` on self_consumption charges → ~1.6 kW cycle-average,
  restoring the rate the whole verdict tree already budgets. **autonomous unchanged** (reserve=100,
  full 5 kW, export-guarded). It's a *chase* re-set each cycle; the `min(…,target)` cap can't
  overshoot or export; SoC-unreadable falls back to the old fixed-reserve behaviour. New JSONL
  fields `charge_target_pct`/`reserve_cmd_pct`/`charge_offset_pts`/`charge_rate_intent` let
  build_models later calibrate the offset→rate dial. All in `energy_agent.py`
  (`_execute_deterministic_verdict`); verdict tree untouched. **Control-path change.**
  **Does NOT govern the HA emergency automation** (still slams 5 kW when it fires — that's the
  separate survival-floor reconciliation — see next bullet). 7 new tests (combined total below).
  **Watch:** confirm model_params self_consumption stays ~1.67 (the `min(10-day,2-day)` window holds
  it there through ~07-27; after that the restored gentle physics keeps measured power low). If it
  ever reads high while the controller is active, add a fill-time clamp so the deadline budget can't
  under-count charging time.
- **Rule 30 revised — rule layer defends a 12% overnight floor** (`SURVIVAL_FLOOR_DEFENSE`,
  `OVERNIGHT_SURVIVAL_FLOOR_PCT=12`, `SURVIVAL_FLOOR_TARGET_PCT=20`). The 07-24 fix (lower the
  emergency automation 20→10) was necessary but not sufficient: on 07-25 the battery rode to 5%
  overnight and the automation fired at 07:00 (SoC 5 < 10), the 07:30 HOLD cleared reserve to 5%,
  and the oscillation recurred. Root cause: the rule layer's floor (5%) is *below* the automation
  trigger (10%), and lowering the trigger only relocates the sawtooth. Fix (user's choice): when a
  HOLD verdict lands at instantaneous SoC ≤ 12%, override to a gentle self_consumption top-up
  (`survival_floor_defend`, target 20%) — Rule 31 makes it ~1.6 kW. The battery never reaches the
  floor, so the automation never fires in normal operation (stays a true "agent dead" backstop).
  Composes with Rules 22/25 (they ride down to 12, the floor catches there). Post-processing
  override in `compute_decision_context` — only touches holds, never a deadline autonomous charge,
  never in the demand window. Cost ~12¢/night of arbitrage; kill-switch reverts to ride-to-5%.
  5 tests. **Watch:** confirm the oscillation is gone on the next low-SoC morning (SoC should sit
  ~12–15 overnight, not 5, and the emergency automation should not fire).
- **Rule 32 — decide on the 30-min slot, not the 5-min spot** (`PRICE_USE_30MIN_SLOT`).
  `sensor.…_general_price` is `duration:5`; the agent sampled it once per 30-min cycle and treated
  that coin-flip as the interval price (on 2026-07-23 12:00 it sampled 9¢ → EV Fast when the 30-min
  value was 11¢ → Eco). `compute_decision_context()` now anchors `price` on `price_forecast[0]` (the
  current interval, bucketed+averaged by `get_price_forecast()`), which fixes **every** threshold at
  once (spread, forward_min, hours_to_cheap_end, deferral/sliding, cost-target, all three EV
  thresholds) since `price` is the single anchor. Falls back to the spot when the forecast is empty
  or the flag is off. Logs `price_used_c` + `price_spot_c` to `decisions.jsonl` `computed_context`.
  HA automations still read the 5-min sensor (coarse 20¢/0¢ thresholds — separate). Hysteresis
  deferred. 3 new tests; one pre-existing test had a spot/forecast mismatch it *relied on*, made
  consistent. **Combined session-20 total: 221 decision + 16 optimizer + 11 build_models.**

Key agent capabilities added 2026-07-24 (session 19):
- **Solar accuracy now measured against the bias-corrected forecast** (`_solar_accuracy()` +
  new `_hour_solar_ratio()`). Raw Solcast over-forecasts this flat roof ~7× at 08:00 in winter, so
  `actual/raw` read `unreliable` on a *normal* morning and zeroed `expected_solar`, forcing needless
  grid charging (the 2026-07-24 08:00 over-charge). Accuracy is now `actual ÷ (raw × hour ratio)`;
  falls back to raw when the hour is uncalibrated or Solcast attrs are missing. **Control-path
  change — makes the agent charge less eagerly; deployed live 2026-07-24 mid-morning.** Directly
  addresses the open "check `solar_unreliable` calibration" item. energy_rules Rule 11 updated.
- **Asymmetric charge-rate window** (`_aggregate_charge_rates()` in `build_models.py`, pure +
  unit-tested). Headline `kw = min(10-day median, 2-day median)` → **falls within ~1 day, rises
  only on sustained evidence**. Resolves the open "make the charge-rate window asymmetric" item.
  Keeps `self_consumption` at 1.67 today (intended); its value is a *safe* flip to ~5 kW once the
  07-22 regime sustains (~07-27), with fall-fast protection if it reverts. `kw_long`/`kw_short`
  recorded. **Offline builder change — no live effect until `build_models.py` is next run on the Pi.**
- **8am over-charge root-caused** (see energy_log 2026-07-24): emergency automation firing at
  SoC<20 + 3× pessimistic rate + zeroed solar credit, all compounding out of an overnight drain to
  8%. The LP shadow held correctly throughout (only LP↔det divergence in 60 cycles).
- **Rule 30 — one overnight survival floor** (config change, deployed live). Resolves the open
  survival-floor contradiction: `battery_low_soc_emergency_charge` trigger lowered **20% → 10%** to
  match the rule layer's designed 5%-floor ride (it holds to the reserve floor overnight by design;
  the 20% automation fought that every low-SoC morning). 10% keeps ~one agent-cycle of margin above
  the 5% physical reserve. Peak-month 85% target / demand-window / 20¢ / 07:00–22:00 guards
  unchanged. energy_rules Rule 30.
- **Nightly model rebuild + deploy-hazard defused** (Pi infra). `build_models.py` now runs on a
  02:00 cron (was hand-run). `model_params.json` untracked + gitignored (derived machine-local
  state); agent cron pull decoupled (`{ git pull || true; } && …`) so a git problem can never stop
  the agent. Validated: ran `build_models.py` on the Pi, tree stayed clean. Read the live model via
  SSH now. Resolves the "cron build_models + pull hazard" todo.
- **219 tests** (was 199): 192 decision + 16 optimizer + 11 build_models (new `test_build_models.py`).

Key agent capabilities added 2026-07-23 (session 18):
- **Rule 28 — control inputs are range-checked (`SETTINGS_SPEC`)**: the **7** `input_number`
  helpers are read every cycle by `compute_decision_context()` and were previously trusted with
  no validation and no audit trail. **No target values live in code or docs** — the HA console is
  the single source of truth; `SETTINGS_SPEC` declares only `(alias, lo, hi)` bands, which are
  engineering limits rather than preferences. In-band values pass through untouched. Out-of-band
  values are substituted **for that cycle only**, preferring (1) the last *genuinely observed*
  in-band value HA reported, (2) a clamp to the nearest band edge, (3) omitting the key so the
  caller's own `.get(key, default)` applies. Nothing is ever written back to HA (validate-and-warn,
  not self-heal), so the agent structurally cannot move a slider. `settings_used` +
  `settings_violations` logged per cycle to `decisions.jsonl` — an audit trail independent of HA's
  recorder.
- **Two live faults this exposed**: `battery_max_insurance_floor_pct` read **0**, silently
  disabling Rule 15's insurance floor; and `ev_min_soc_pct` had drifted to **80**, firing
  `ev_case3_below_minimum` at 60% EV SoC and putting the Zappi on Fast on a peak morning with the
  house battery at 30% and falling. Both slipped through because `x or default` and
  `dict.get(k, default)` mean "if absent", not "if wrong".
- **Neither non-EV helper was on any dashboard** (verified against all five `lovelace*` files in
  the Pi's `.storage`). So the insurance floor's 0 was never chosen — with no `initial:`, an
  untouched `input_number` defaults to its `min`. **Rule 15's floor had been inert since the
  helper was created (2026-05-31).** Both now carded; user has set the floor to **30%**, in band,
  giving zero violations.
- **`battery_charge_price_threshold_c` deleted** — added in the *same commit* as
  `HISTORICAL_PRICE_MODEL` (13297f8) and never wired to anything; `energy_rules.md` never
  mentioned it. Rule 15's rolling p25/p75 already answers "is this cheap?" *and self-calibrates*.
  Its only effect was appearing in the LLM's state block, so narratives claimed a threshold was
  being respected that nothing enforced.
- **Test for whether a control deserves to exist** (stated, then corrected by the user): does it
  encode information the agent *cannot obtain*? Battery grid-charging is instrumental with a
  fully-known objective → derive it. The EV has exogenous value (it must be driven) → "I need the
  car tomorrow, so I'll pay 20¢" is a legitimate control. Failure modes differ: a stale
  market-fact threshold becomes **wrong** and misleads silently; a stale willingness-to-pay
  threshold becomes **non-binding but stays true**, and non-firing is self-evident.
- **`binary_sensor.peak_month`** — new template sensor so dashboards can show/hide cards on
  demand-charge months without embedding the EA116 month list in a `mode: storage` dashboard.
  Carries `peak_months`/`off_peak_months`/`yes_no` attributes. Deliberately *not* referenced from
  `battery_grid_charge_target` (which must not depend on another template entity resolving first);
  `test_peak_months_agree_across_agent_and_ha_config()` asserts all three copies of the month list
  match `PEAK_MONTHS`, and was verified to fail on deliberate divergence.
- **Rule 15's insurance floor is dormant 8 months a year** — gated on `not is_peak`, so active
  only Apr/May/Sep/Oct. Correct by design (Rule 13's 85%-by-2:55pm is a far higher floor in peak
  months), but it means a value set today does nothing until April.
- **Amber publishes 5-minute prices, and the agent treats one sample as the half-hour price**
  (`duration: 5` on the price sensor). It crossed the 10¢ EV threshold six times in twenty minutes
  on 2026-07-23; the 12:00 cycle sampled 9¢ while the dashboard showed 11¢. **Every threshold
  comparison in the system is made on effectively a sampled coin-flip.** HIGH in `todo.md`; the
  likely fix is the 30-min forecast slot the agent already fetches.
- **HA recorder is not capturing the `input_number` helpers** — a 6-day history query returns one
  row per entity while live states carry same-day `last_changed`, and `recorder:` excludes only 5
  Polestar sensors. Unexplained; tracked in `todo.md`. The agent's own per-cycle logging is the
  interim audit trail, but only the HA logbook can supply `context_user_id` (i.e. *who* wrote it).
- **Rule 29 — control layer now reasons from bias-corrected solar** (`USE_CORRECTED_SOLAR`,
  kill-switch). `_corrected_solar_breakdown()` extracted from the dashboard push so the control
  path and the card share one code path. Falls back to raw (never zero) if Solcast
  `detailedHourly` is unavailable. **183 decision tests + 16 optimizer tests** at session close (was 118).
  **Scope, honestly**: replaying all 21 cycles of 2026-07-23 raw-vs-corrected changed which rule
  fired in 18 and the *action* in none — the overnight holds were price-driven. This does not fix
  the drain-to-17% problem it was proposed for; it makes `kwh_needed_85` honest for the days where
  solar genuinely decides the outcome.
- **Overnight survival floor is contradictory across layers (OPEN)**: the rule layer holds while
  `projected_soc_at_sponge > 5%`, while `battery_low_soc_emergency_charge` triggers at **SoC < 20%**.
  On 2026-07-23 the automation fired at 08:00 and the 08:30 HOLD immediately cleared reserve back
  to 5% — the layers fighting. This, not the solar forecast, is what drove the 17% trough. Needs a
  decision on the intended floor; see `todo.md`.
- **5 kW `self_consumption` regime confirmed as persistent** (07-22 and 07-23, median 5.00 kW,
  92%/96% of samples >3 kW, vs 1.67 kW and 0–4% on 07-13→07-21). Below 70% SoC `self_consumption`
  and `autonomous` are now indistinguishable. `model_params.json` still reports 1.67 kW because
  `POWER_DAYS=10` and `kw` is the median — it cannot flip until ~2026-07-27. The agent is
  therefore planning against a 3× pessimistic rate; error is in the safe-but-costly direction.

**Layer 3 — Rules** (HA automations, always active): hard constraints that fire deterministically regardless of agent decisions. React in seconds. Cannot be overridden by the agent — including by the manual override. Handle safety, demand window, export guard, and edge cases.

---

## Current automation status (verified live 2026-07-22)

**27 automations** in `config/automations.yaml`, all deployed and loaded — **15 active
(safety/monitoring), 12 disabled (agent handles)**.

Plus **4 orphaned entities** in state `unavailable` — they exist in HA's entity registry
but not in any YAML, left over from automations deleted at some point. They cannot fire.
Clean up via HA UI → Entities if they become confusing:
`battery_intraday_cheap_window_charge_check`,
`battery_revert_to_self_consumption_once_emergency_floor_reached`,
`ev_freeze_powerwall_reserve_while_ev_charging`,
`ev_restore_powerwall_reserve_when_ev_stops_charging`.
(HA reports 31 automation entities = 27 real + 4 orphans.)

Counts below were read from the live HA, not from the file. To re-verify:
`./deploy_ha_config.sh --check` for drift, and query `/api/states` for enabled state.

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
| `battery_low_soc_emergency_charge` | Charge if critically low + cheap price + 20¢ ceiling + 85% target in peak months. Written 2026-06-23, **actually deployed 2026-07-22** |
| `solar_inverter_underperformance_alert` | Alert when inverter under-produces vs Solcast |
| `ev_plugged_in_notify` | Alert when EV connects with SoC/price snapshot |
| `sensor_watchdog_morning` | 09:30 daily: checks 8 sensors for unavailable/stale (>2h), sends persistent notification. **Only actually running since 2026-07-22** — written 2026-06-09 but never deployed |
| `ev_demand_window_guard` | Forces Zappi to Eco+ at 3pm so the EV can't draw during the demand window. Also only live since 2026-07-22 |
| `restore_virtual_sensors_on_startup` | Re-pushes virtual sensors after HA restart. **Currently broken** — calls a Mac path, and the script is outside the container's `/config` mount. Self-heals via hourly cron; see todo |

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

- **`self_consumption`**: normal operation, ~1.7 kW grid charge rate. Used for long cheap windows (3h+) or when price spread doesn't justify urgency. **Since 2026-07-25 (Rule 31)** the ~1.7 kW rate is *actively maintained* by chasing `reserve = SoC + 6` each cycle rather than writing a fixed high reserve — without this, firmware 26.18.3 makes any self_consumption charge slam 5 kW. Kill-switch: `GENTLE_CHARGE_CONTROL`.
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
| `config/automations.yaml` | HA automations — **deploy source, not what's running** until `./deploy_ha_config.sh`. 27 defined: 15 active, 12 disabled |
| `config/configuration.yaml` | HA config — sensors, REST commands, template sensors. Same deploy rule |
| `agent/energy_agent.py` | Claude-powered optimisation agent — the strategic decision layer |
| `agent/backtest.py` | Peak-month scenario backtest — feeds the real agent synthetic scenarios, stubs all reads/writes. Validate demand-window logic before a peak month |
| `agent/test_decision.py` | 183 unit tests for `compute_decision_context()` — pure, no API calls, run in ms |
| `agent/optimizer.py` | LP/MPC optimiser (shadow only) — receding-horizon scipy LP; verdict shape matches the deterministic layer for three-way A/B. See PRODUCT.md "Optimisation Engine — Depth" |
| `agent/test_optimizer.py` | 16 unit tests for the LP optimiser — pure, no API calls. Includes regression tests pinning the SoC contract (see 2026-07-22) |
| `agent/.env` | API keys (gitignored — not in repo) |
| `agent/agent_decisions.log` | Plain-text decision log (one line per cycle, committed to git) |
| `agent/decisions.jsonl` | Structured JSON decision log — full context per cycle, foundation for analyst agent and accuracy tracking |
| `agent/log_daily_energy.py` | Daily (21:05 cron) energy journal → `daily_energy.jsonl`. Comprehensive: solar forecast/actual, price by window, battery trajectory, grid import/export, demand window status (billing-accurate), agent decision rollup. Reads HA history API + decisions.jsonl, no HA config change. **Demand verdict is 3-way (2026-07-03): `classify_demand()` bands the peak 30-min-avg import — pass <0.5 kW (regulation-noise floor) / marginal 0.5–1.5 / breach ≥1.5 kW (sustained, preventable) — with `cost_est_dollars` at the EA116 rate ($11.5479/kW). Replaces the old single 0.10 kW pass threshold, which mislabelled ~$2 regulation-lag days as breaches.** |
| `agent/daily_energy.jsonl` | Durable per-day energy record (survives HA recorder rolloff) — source of truth for dashboard cards and future learning agent. `demand_window` now carries `status`+`cost_est_dollars` (plus legacy `passed`) |
| `agent/demand_window_summary.py` | Pushes `sensor.demand_window_monitor` into HA via REST API (month peak kW + est $/mo + rolling per-day history, re-scored from `peak_kw` under current bands). Reads daily_energy.jsonl. Crons: 21:05 + hourly. Feeds two Markdown dashboard cards |
| `agent/data_logger.py` | Closed-loop SQLite logger — one row per cycle in `energy_log.db`. Foundation for self-calibrating models (Phase 2.5-A+). Wired in 2026-06-06. |
| `agent/energy_log.db` | SQLite DB on Pi only (gitignored). Accumulates state + price forecasts + decisions each cycle. Inspect: `ssh energypi.local "agent/venv/bin/python home-energy-agent/agent/data_logger.py"` |
| `deploy_ha_config.sh` | Deploys `config/` to the live HA on the Pi. `--check` diffs without changing anything. **HA does not read `config/` directly** |
| `agent/build_models.py` | Calibration model builder — run on Pi after 2+ weeks of `energy_log.db` data. Builds solar_correction (per-hour Solcast bias ratio) and charge_rate_kw (autonomous rates by SoC bucket) and writes to `model_params.json`. Run: `cd ~/home-energy-agent && agent/venv/bin/python agent/build_models.py` |
| `agent/model_params.json` | Calibration parameters loaded at agent startup. Contains `solar_correction` (per-hour Solcast ratio), `charge_rate_kw.self_consumption` and `.autonomous`. **Gitignored / machine-local since 2026-07-24** — rebuilt on the Pi nightly (02:00 `build_models.py` cron), *not* tracked in git. The Mac's copy is a stale snapshot; **read the live model with** `ssh energypi.local "cat ~/home-energy-agent/agent/model_params.json"`. Charge-rate window is asymmetric (`kw = min(10-day, 2-day median)`; rises on sustained evidence, falls in ~1 day). Agent loads it with a graceful `{}` fallback, so absence degrades to hardcoded rates + raw Solcast. |

---

## What to watch for

**Open from 2026-07-26 (session 21) — watch these first:**

- **Rule 33 gentle-lead on the next low-SoC peak morning.** On a peak day with a low morning SoC the
  agent should now *gentle-lead* (self_consumption ~1.7 kW) from ~10am and only escalate to 5 kW
  autonomous near ~1pm if gentle + solar are falling behind — not slam 5 kW at 10am. Confirm it
  reaches 85% by 2:55pm without an early 5 kW burst. If it ever cuts it close, raise
  `FAST_ESCALATE_BUFFER_H` (currently 1.5h).
- **Hold reverts autonomous within one cycle.** After any autonomous charge, the first `hold` verdict
  should flip mode back to self_consumption and stop grid-charging. Confirmed by hand today; watch it
  happen unattended.
- **Agent liveness (robustness gap, NOT yet fixed).** Today's whole incident was an expired
  `ANTHROPIC_API_KEY` crashing the agent at the narrative call — control survived but logging went
  dark and it *looked* frozen. Until the LLM call is wrapped in try/except (top todo item), a key
  expiry or Anthropic outage will do this again. If `decisions.jsonl` stops updating, check
  `/tmp/energy_agent.log` on the Pi for a 401/traceback first.
- **Why it arrived empty.** The battery rode 11–18% overnight on `wait_for_cheap_go_hard` (prices
  never dropped below ~14¢), so it hit the peak morning near-empty and needed the big charge. Rule 33
  softens the *response*; the overnight strategy leaving it that low on a peak day is worth revisiting
  (todo). Note: Rule 30's 12% floor held — the emergency automation did **not** fire overnight.
- **Slider drift (ongoing check):** 2026-07-26 overnight — **0 `settings_violations`, all 7 helpers
  stable and in-band** (`ev_charge_target_pct` reads 90, was 100 on 07-25 — in-band, likely a
  deliberate change). Clean night.

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

**HA automation YAML vs UI enable state — resolved 2026-07-22.** `automations.yaml` has no `enabled: false` entries; enable/disable lives in HA's internal storage, so the file cannot tell you what's active. Now verified directly against the live `/api/states`: **15 on, 12 off**, matching the tables above. The previously-flagged risk `battery_cheap_window_autonomous_charge` (sets reserve=100% when the Amber cheap window opens) is confirmed **off** — it appears as `automation.battery_switch_to_autonomous_grid_charge_during_cheap_window`. To re-check, query `/api/states` and filter `automation.` rather than reading the YAML.

**LP was blind to SoC — fixed 2026-07-22.** From its 2026-06-01 wire-in until 2026-07-22 the LP received a hardcoded **50% SoC** on every cycle (`optimize_battery()` read a top-level `soc_pct`; `energy_agent.py` passes it nested under `state["battery"]`, so the default fired every time). **All three-way divergence analysis before 2026-07-22 is void** — including the "LP defers to cheapest slot" blocker and session 13's "cause (c)" conclusion. Fixed at the call site; `_require_soc_pct()` now raises instead of defaulting. Phase 4 divergence clock restarted 2026-07-22 — a fresh week of clean data is needed before the LP-to-control question can be reopened.

**Phase 2.5-B ACTIVATED (2026-07-22)** — `build_models.py` ran successfully for the first time (three bugs fixed to get there; see energy_log). `model_params.json` rebuilt from 45 days: `solar_correction` populated per local hour, `autonomous` charge rates populated. Key result — **Solcast over-forecasts by ~7× at 08:00 and ~6× at 09:00**, converging to ~1.4× by 13:00 (n=55–90/hour). Consequence: "solar at 7% of forecast" on a winter morning is *normal*, not a sensor fault. Autonomous buckets are n=2–5, mostly below `MIN_SAMPLES=5`, so most still fall back to the flat 5.0 kW prior — re-run `build_models.py` periodically as autonomous cycles accumulate.

**Historical price model** — first live run was 2026-05-31. Watch `cost_target_method: historical` in JSONL. p25/p75 will shift as June peak-month prices accumulate. May need to tune `CHEAP_BAND_ALPHA` and `MIN_DAILY_SWING`.

**On rainy/cloudy peak days**: Solar won't cover the deficit. Agent must escalate to autonomous during the cheap window (10am–2pm). At 1.7kW self_consumption rate there may not be enough time — autonomous (5kW) is needed. Watch Rule 16 (`nonpeak_solar_unreliable_autonomous`) firing correctly.

**Open from 2026-07-23 (session 18) — watch these first:**

- **Slider drift — is it still happening?** The user reported EV helpers repeatedly found higher
  than left (e.g. `ev_min_soc_pct` at 80 vs 30 set). Nothing in the repo writes them, and the
  agent structurally cannot (validate-and-warn never writes back). **`settings_used` is now logged
  every cycle**, so drift is pinned to a 30-min window automatically — check it each morning.
  First 2½ hours of data showed all six EV values rock stable. The reported incidents were
  overnight/early morning, so **tomorrow morning is the real test.**
- **HA's recorder is not capturing the `input_number` helpers.** Unexplained. This matters because
  only the logbook's `context_user_id` can say *who* wrote a value — the agent's own log can say
  what and when, but never who. Highest-value remaining item on the drift question.
- **The 5-minute price problem (HIGH).** Amber's price sensor carries `duration: 5`; the agent
  samples one 5-min price per 30-min cycle and treats it as the interval price. Every threshold
  comparison is effectively a coin-flip. Not fixed — needs a design decision (`todo.md`).
- **Survival floor contradiction — RESOLVED 2026-07-24 (Rule 30).** Emergency automation trigger
  lowered 20% → 10% to match the rule layer's designed 5%-floor ride. Deployed live. Watch that the
  oscillation is actually gone on the next genuine low-SoC morning (should now be rare — the battery
  is seldom below 10% except on a deep-drain night, and at that SoC the rule layer's own deadline
  logic is usually charging too, so they agree).
- **`self_consumption` charge rate should flip to ~5 kW around 2026-07-27** if the new regime
  holds — the long window needs 6 of 10 days. Until then the agent plans against 1.67 kW, i.e. 3×
  pessimistic (safe direction, but it charges earlier and dearer than needed). **Now retrains
  automatically** (nightly `build_models.py` cron, session 19) and the window is **asymmetric**
  (Rule: `kw = min(10-day median, 2-day median)` — rises only on sustained evidence, falls within
  ~1 day). As of the 07-24 rebuild most `self_consumption` buckets are still held at 1.67 (intended);
  note SoC=20% already reads 4.96 due to sparse old-regime samples in that bucket. Check the flip
  via `ssh energypi.local "cat ~/home-energy-agent/agent/model_params.json"` — no manual rebuild needed.
- **Rule 15's insurance floor does nothing until April** (gated `not is_peak`). The 30% set on
  2026-07-23 is parked, not active.

**Monitoring questions:**
- Does overnight_hold (Rule 20) prevent high-price overnight charging each night?
- Does agent correctly escalate to autonomous on a cloudy peak morning?
- Does `battery_autonomous_export_safety_net` catch any misbehaviour within 30s?
- **Three-way shadow**: now that the LP actually sees SoC (fixed 2026-07-22), does it agree with the rule layer on peak-day pre-charge decisions? Review via `/morning`. Note LLM↔det is tautologically 100% since the Phase 5 cutover — the only real signal is LP↔det.
- **LP timing margin**: under flat prices the LP defers charging to the last feasible slot with no buffer against forecast error. Watch whether the `risk` knob / a conservative solar quantile is needed.

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
