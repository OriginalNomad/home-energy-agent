# Energy System Control Log

## 2026-05-26

**Agent first full overnight run**:
- Cron running every 30 min via `/usr/bin/python3 agent/energy_agent.py`
- Price forecast working: fixed entity to `sensor.1a_wigram_road_glebe_general_forecast` with `nem_date` key
- Agent correctly identified 13¢ window at 1:30am and deferred charging from 16¢ evening rate
- Emergency override fired at ~10pm when battery hit 7% — battery couldn't survive to 1:30am window, charged at 16¢
- Overnight logic refined: only charge immediately if battery can't reach cheap window; otherwise wait
- Autonomous mode re-enabled in agent with explicit conditions: short window (<2h), significant charge needed, always reserve=100%, HA 30s safety net as backstop
- Peak month 2:55pm target added to agent system prompt: battery must be ≥85% by 2:55pm in peak months; agent works backwards from this target each morning

**Rainy day observation (May 26)**:
- Battery only forecast to reach 21% SoC by 3pm — fine in May (off-peak) but would trigger demand charge in June
- Root cause: self_consumption mode only charges at 1.7kW; brief cheap window at 17-18¢ not enough time
- Agent now permitted to use autonomous mode (5kW) for short cheap windows — should charge significantly faster on rainy days
- June 1 is the first real test of peak month logic

**Solar forecast accuracy cross-check added**:
- New sensors added: `forecast_this_hour` (Wh) and `forecast_next_hour` (Wh) from Solcast BJReplay integration
- Accuracy check now uses `forecast_this_hour` (hourly aggregate, more stable than `power_now`) compared to actual inverter output
- `forecast_next_hour` exposed to agent as forward-looking context: if next hour is also low, don't wait for solar to improve
- Fixed unit bug: `power_now`, `this_hour`, `next_hour` are all in W or Wh — previously treated as kW/kWh. Now correctly divided by 1000.
- Agent system prompt updated to explain all three reference points and when to use each

**Autonomous mode — price spread logic added**:
- Autonomous mode (5kW) is now gated by price spread, not just window duration and SoC gap
- Spread < 5¢: don't charge at all, hold for better window
- Spread 5–8¢: self_consumption only, long windows only
- Spread 8–15¢: self_consumption for long windows; autonomous if window < 2h AND need > 15% SoC
- Spread > 15¢: autonomous justified — real arbitrage
- Peak month demand window overrides spread logic (demand charge ~$100/month outweighs all cost calculations)
- Confirmed correct: today (May, 20¢ now vs 16¢ later, 4¢ spread) agent correctly held — no charging

**Architecture formalised**:
- Three layers: Intent (system prompt) → Agent (strategic decisions, 30-min cycles) → Rules (safety constraints, millisecond triggers)
- 12 strategic HA automations disabled; 12 safety/monitoring automations remain active
- CONTEXT.md and energy_rules.md updated to reflect current architecture

---

## 2026-05-25

**CRITICAL INCIDENT — Battery exporting to grid in autonomous mode**:
- Battery in autonomous mode (charge target reached), SoC ~41%, reserve 100%, grid price 15¢ sell/8¢ buy
- Battery was discharging 4.38kW and exporting to grid — buying at 15¢ and selling at 8¢
- Root cause 1: `battery_autonomous_revert_target_reached` uses only `sensor.tessie_powerwall_charge` (Tessie cloud, ~2-min poll lag) with `for: "00:02:00"` — total latency up to 4 minutes before reverting
- Root cause 2: `battery_autonomous_export_safety_net` didn't fire because automations hadn't been reloaded after earlier changes
- User manually fixed: applied `rest_command.powerwall_set_mode → self_consumption`
- **Patch applied**: `battery_autonomous_revert_target_reached` now uses OR of Tessie + local gateway (`sensor.tesla_powerwall_2_charge`) — whichever responds first; `for:` reduced from 2 minutes → 30 seconds
- **Patch applied**: `battery_autonomous_export_safety_net` `for:` reduced from 1 minute → 30 seconds
- Local gateway sensor floors at reserve level (can't show below reserve%), but in autonomous mode battery is charging, so it reads above reserve — making it a valid fallback trigger
- Combined latency now: max ~30s after gateway detects target reached, vs up to 4 minutes before

**EV overnight gate extended to 9:30am**:
- EV was charging at 7kW from 6am at 18¢ — same cheap-window-is-always-True issue as overnight
- `amber_in_cheap_window` compares current to avg 4–8pm: True at 18¢ even when 13¢ was available at 10am
- Case 3b gate extended from `before: 06:00` to `before: 09:30` — EV won't fast charge before Solar Sponge window unless price < 10¢
- Gate ends at 9:30am when Solar Sponge logic takes over with proper cheap window pricing

**Battery morning charging improvements**:
- Observed: battery charging at 18¢ when 13¢ was available 2hrs later — same look-ahead gap as EV
- No demand charge applies before 3pm so no penalty for waiting
- Three changes made:
  1. `battery_morning_reserve_reset`: 8am → 6am — allows 3.5 extra hours of free discharge; battery arrives at cheap window at lower SoC and absorbs more cheap energy
  2. `battery_morning_charge_trigger`: off-peak months now require cheap window AND price < 15¢ (was: cheap window only)
  3. `battery_cheap_window_autonomous_charge`: before 10am requires price < 15¢; after 10am cheap window alone sufficient (deadline pressure)
- Peak months unchanged — always charge regardless of price (demand window risk overrides economics)
- Threshold of 15¢ chosen: above cheapest Solar Sponge slots (~8-13¢), below typical morning rates (~18-20¢)

**EV plug_status values confirmed (2026-05-24)**:
- `"EV Connected"` confirmed when plugged in; `"EV Disconnected"` when not

---

## 2026-05-24 (evening)

**Removed ev_protect_battery_while_charging and ev_release_battery_after_charging**:
- Backup mode approach had two unacceptable side effects:
  1. Home load went to grid (battery couldn't discharge for home while EV charged)
  2. Powerwall in backup mode charged FROM grid to reach reserve target (pulled 1.6kW at 20¢ unintended)
- Root cause: Powerwall cannot distinguish EV load from home load — both are "home load" on the same circuit
- No Tessie API call can say "discharge for home but not EV"
- Reverted to self_consumption with ev_charge_mode_manager controlling Zappi mode only
- EV-vs-battery policy deferred: ev_charge_mode_manager handles Eco during demand window (June+) which is the critical protection

**EV plug_status values confirmed (2026-05-24)**:
- `"EV Disconnected"` — confirmed earlier today
- `"EV Connected"` — confirmed this evening when EV plugged in
- Both `ev_plugged_in_notify` and `ev_charge_mode_manager` triggers now verified correct

**Eco+ mode confirmed working (2026-05-24 evening)**:
- EV plugged in at ~6pm, SoC 33%, grid 16¢, battery 58%
- ev_charge_mode_manager correctly fell to default (Eco+) — SoC above 30%, cheap window closed
- Zappi idle in Eco+ as expected — no solar export at 6pm, so no charging
- Battery discharging normally for home, unaffected by EV being plugged in
- To verify tomorrow: Eco+ activates when solar export appears during the day

**EV charging stuck in Fast mode after cheap window closed (observed ~6pm)**:
- Energy flow showed EV drawing power and grid importing 5.2kW after cheap window closed
- ev_charge_mode_manager should have switched Zappi to Eco when amber_in_cheap_window → False
- Root cause likely: automations not reloaded since earlier changes, or API call timing
- Fixed by: reload automations, manually restore self_consumption + reserve 5%

---

## 2026-05-24

**Correction to 2026-05-23 autonomous mode conclusion**:
The May 23 log incorrectly concluded that autonomous mode was "banned permanently." The accurate position is:
- `autonomous` mode with `reserve=100%` IS used — it is the mechanism for fast (~5 kW) grid charging during cheap windows (`battery_cheap_window_autonomous_charge`)
- `reserve=100%` is the critical export guard: prevents the Powerwall discharging or exporting while in autonomous mode
- What was banned was autonomous mode *without* `reserve=100%` — confirmed on May 23 to cause simultaneous import at 11¢ and export at 4¢
- `battery_solar_sponge_mode_check` was updated to use `self_consumption` (not autonomous) for its 30-min ticks; fast charging is handled by `battery_cheap_window_autonomous_charge` which fires reactively when the cheap window opens
- Confirmed working 2026-05-24: battery observed charging at 5 kW with `Mode: autonomous` displayed on dashboard card

**Bug fix: solar_sponge_mode_check default branch (2026-05-24)**:
- Observed: battery at 89% SoC, grid charge target 80%, reserve set to 100%, still buying from grid at 15¢
- Root cause: `battery_solar_sponge_mode_check` default branch (fires when solar is sufficient) was setting `reserve: 100` — every 30-min tick was resetting reserve to 100% even when SoC was already above the grid charge target
- Fix: default branch reserve changed from 100% to 5% — when solar covers the deficit, release the reserve and stop grid charging

**EV charging automations built (2026-05-24)**:
- `ev_charge_mode_manager`: sets Zappi mode every 10 min based on EV SoC, price, demand window, Powerwall state
- EV target SoC: 60% (mirrors battery's 95% but without a fixed deadline)
- Below 30% SoC + price < 20¢: Fast, charge both EV and Powerwall simultaneously (critical)
- Below 60% SoC + cheap window + battery near target: Fast (battery satisfied, EV takes grid)
- Below 60% SoC + cheap window + battery still charging: Eco (battery first, EV takes solar surplus)
- Ultra-cheap (< 5¢) or negative: Fast regardless of SoC
- Demand window: Eco always (no grid, Rule 2 absolute)
- Default: Eco (always absorb solar surplus)
- `ev_plugged_in_notify`: alerts on EV connection with SoC/price snapshot
- plug_status string values ("EV Connected" / "EV Disconnected") need verification when car is home tomorrow
- Discarded fixed price-tier approach (< 40¢ / < 20¢ / < 12¢ / < 8¢) in favour of `amber_in_cheap_window` as primary price gate — same approach as battery system
- Energy rules doc updated to reflect new EV charging logic

**Session: housekeeping, bug fix, system review**

- CONTEXT.md committed to GitHub repo (`OriginalNomad/home-energy-automation`) for mobile access
- CONTEXT.md updated to reflect current state: 22 automations live, dynamic charge target, Tessie SoC sensor, self_consumption-only policy
- Fixed bug: `sensor.battery_power_w` had erroneous `* -1` multiplier causing charging to display as discharging in dashboard cards (showed "Discharging at 5kW" during grid charging). Removed inversion — now matches standard convention (negative = charging, positive = discharging)
- InfluxDB dashboard: confirmed how to set per-cell time ranges independent of dashboard selector (hardcode `range(start: -24h)` in Flux query instead of `v.timeRangeStart`)
- Discussed power outage resilience: Mac Studio needs "Start up after power failure" enabled in Energy settings + Docker auto-start on login. Raspberry Pi + UPS is the better long-term unattended setup
- Automations confirmed working and logically consistent with observed solar/battery/grid pricing behaviour

## 2026-05-15
- Manually triggered battery charge twice via Amber app override
- SmartShift Battery Booster is ON but not automatically charging from grid
- Powerwall was in **Self-Powered mode** — switched to Time-Based Control mode in Tesla Energy app
- SmartShift manual override successfully charged battery, proving Amber DOES have Powerwall control
- Root cause: SmartShift Battery Booster automatic logic is not triggering grid charging despite having capability to do so
- Battery confirmed at 4% overnight despite SmartShift minimum reserve set to 40%
- 5 days of data (9-15 May) shows battery never exceeds ~50%, depletes by 9pm nightly
- Amber CEO confirmed EA116 demand window is 3-9pm daily in peak months
- Peak months: Nov, Dec, Jan, Feb, Mar, Jun, Jul, Aug
- Off-peak months (no demand window): Apr, May, Sep, Oct
- May confirmed as off-peak — demandWindow = false in Amber API at 3:05pm

## 2026-05-16
- **Amber app terminology discrepancy**: Amber shows battery as "consuming" when HA and Tesla app both show battery as "charging". This is a sign convention difference — Amber uses "consuming" to mean the battery is drawing power (i.e. charging), and "generating" to mean the battery is discharging to the home. Technically correct but counterintuitive. To be raised with Amber as a UX feedback item.

## 2026-05-16 (continued)
- Completed full energy automation rule-set (energy_rules.md, 10 rules)
- EA116 tariff documented in detail (ea116_tariff.md) — 30-min demand window, Solar Sponge 10am-3pm, export penalty
- Added Amber price forecast chart to dashboard (ApexCharts, data_generator approach)
- Added Amber Cheapest Charge Window template sensor to configuration.yaml
- Power Flow Card Plus updated: fixed battery SoC (was showing iPhone battery!), fixed grid entity, added EV node with charge mode display
- Tessie account created for Powerwall control — Tessie HA integration failing to load ("Invalid handler specified"), to be resolved tomorrow
- Confirmed Powerwall local API write access blocked in firmware 26.10.3 (Home_Owner role is read-only for POST requests)
- Powerwall currently in self_consumption mode, backup_reserve at 5%

## 2026-05-18

### Tessie Integration — Protobuf Conflict (resolved via workaround)
- **Root cause confirmed**: `pypolestar` (Polestar 4 custom integration) and `tesla_fleet_api` both define a protobuf file named `common.proto`. When both are loaded in the same Python process, the second registration fails.
- **Workaround**: Skip the HA Tessie integration entirely. Use Tessie's REST API directly from HA via `rest_command`.
- **Energy site ID**: `2252120180790091`
- **Tessie token**: stored in `configuration.yaml`
- **API endpoints confirmed working**:
  - `POST /api/1/energy_sites/{id}/backup` → `{"backup_reserve_percent": N}`
  - `POST /api/1/energy_sites/{id}/operation` → `{"default_real_mode": "self_consumption"}`
  - `GET /api/1/energy_sites/{id}/site_info` → returns current reserve, mode, battery info
  - `GET /api/1/energy_sites/{id}/live_status` → real-time power flows

### Battery Automation — v1 live
- **Control mechanism**: `backup_reserve_percent` via Tessie API
  - 20% = floor only, Powerwall operates normally
  - 100% = forces Powerwall to charge from solar + grid to reach target
- **9 automations deployed** in `config/automations.yaml`:
  1. `battery_startup_set_reserve_floor` — sets 20% on HA restart (Rule 6)
  2. `battery_morning_charge_trigger` — at 9:30am, if SoC < 95%, set reserve to 100% (Rule 1)
  3. `battery_charge_complete_reset` — when SoC reaches 95%, reset reserve to 20% (Rule 1)
  4. `battery_pre_demand_window_reset` — at 2:55pm, always reset to 20% (Rule 2)
  5. `battery_overnight_safety_topup` — at 10pm, if SoC < 41% and price < 25¢, top to 41% (Rule 7 Step 1)
  6. `battery_morning_reserve_reset` — at 8am, clear any overnight top-up setting (Rule 7)
  7. `battery_negative_price_charge` — when price < $0, charge to 100% (Rule 8)
  8. `battery_negative_price_reset` — when price returns positive, reset to 20% (Rule 8)
  9. `battery_winter_overnight_precharge` — Jun-Aug, 1am, if SoC < 75% and price < 15¢, charge to 80% (Rule 7 Step 2)
- **REST sensor** added: `sensor.tessie_powerwall_info` polls site_info every 2 min
- **Template sensor** added: `sensor.powerwall_backup_reserve` reads from tessie_powerwall_info
- **Powerwall current state**: backup_reserve=20%, mode=self_consumption ✓

### Rule 1 price ceiling fix (same day)
- Grey day, little solar, price at 41¢ — morning charge trigger fired correctly at 9:30am but was paying too much for grid power
- Fixed: off-peak months (Apr, May, Sep, Oct) now only trigger forced charging if price < 20¢/kWh
- Peak months (Jun-Aug, Nov-Mar): no price ceiling — demand window risk overrides economics
- Reset reserve back to 20% manually; battery charging passively from solar + grid at natural rate
- Projected SoC at 3pm: ~49% (SmartShift left battery at 5% overnight, not fixable today)
- **SmartShift OFF at 11:30am 18 May 2026** — HA automation in sole control from this point ✅
- **Tessie subscription cost**: ~A$10/month — factor into savings calculations when reviewing ROI

### Monitoring period begins 2026-05-18
- 9 automations live, SmartShift off, Tessie REST API confirmed working
- Watching for:
  - Overnight top-up triggering correctly when SoC < 41% at 10pm
  - Morning charge trigger respecting 20¢ price ceiling in off-peak months
  - 2:55pm reset firing reliably before demand window (critical when June starts)
  - Negative price events (will charge to 100% automatically)
  - First peak month (June 1) — demand window logic becomes non-negotiable

### Next Steps (post monitoring review)
- Install Solcast integration via HACS for solar forecasting (Rule 1 refinement)
- Add Solcast-aware logic to morning charge trigger (cloudy day detection — Rule 9)
- Build EV charging automation (Rules 4 & 5)
- Investigate Daikin AC integration

## 2026-05-23

### Automation gaps found and fixed

**Issue 1**: Battery at 0–10% SoC at 8:35am with grid at 9¢ — not charging. Root cause: no automation runs before 9:30am in off-peak months. The 8am morning reset fires but only clears the overnight reserve, it does not trigger charging. Self-consumption mode + 5% reserve floor = Tesla firmware does nothing.

**Issue 2**: Battery only reached 74% yesterday despite cheap solar sponge prices. Root cause: (a) no trigger fires when `amber_in_cheap_window` flips to True — charging only kicks in at the next 30-min tick, missing up to 29 min of cheap window; (b) if Solcast forecast says solar will cover the deficit, the system stays in self_consumption even if the inverter is under-delivering.

**Fixes applied to `config/automations.yaml`**:

1. **`battery_solar_sponge_mode_check`** — added state-change trigger on `amber_in_cheap_window → True`, so charging starts immediately when the cheap window opens (mirrors the existing `battery_cheap_window_ended` which stops immediately when it closes).

2. **`battery_solar_sponge_mode_check`** — autonomous mode condition now also fires when Solcast `power_now > 500W` but inverter `solar_power_w < 200W` (inverter underperformance detected). Bypasses the "solar will cover it" assumption when the inverter clearly isn't delivering.

3. **`solar_inverter_underperformance_alert`** (new) — fires every 30 min and on solar dropping below 200W, between 9am–5pm. Alerts when Solcast expects >500W but inverter shows <200W. Uses `notification_id` so alerts replace rather than stack.

4. **`battery_low_soc_emergency_charge`** (new) — fills the pre-9:30am and post-2pm gap. Fires when SoC < 20% and price ≤ 10¢ (or cheap window open), 7am–10pm, outside demand window. Target is Solcast-aware: if solar will cover the deficit, charges to 20% safety floor only (self_consumption); if solar won't cover it, switches to autonomous mode and charges to 80%.

**Manual actions taken today**:
- Manually set `backup_reserve_percent: 80` via Developer Tools → Actions
- Manually set mode to `autonomous` via Developer Tools → Actions
- Battery confirmed charging at ~5kW after mode switch

**Autonomous mode — confirmed unsafe (2026-05-23)**:
- Switched to `time_based_control` (autonomous) for faster 5kW charging
- Immediately started exporting to grid at 4¢ feed-in while buying at 11¢ import — guaranteed loss
- Confirmed on two separate occasions: autonomous mode runs Tesla's own TOU optimisation regardless of `backup_reserve_percent` setting
- **Decision: autonomous mode banned from all automations permanently**
- All charging uses `self_consumption` only — slower grid draw but no unwanted exports
- Tessie API note: `default_real_mode` values are `self_consumption`, `time_based_control`, `backup`

**Dynamic grid charge target (2026-05-23)**:
- Added `sensor.battery_grid_charge_target` template sensor
- Formula: `clamp(95 − (net_solar_kWh / 13.5 × 100), 5, 95)`
- Morning trigger and solar sponge check now set reserve to this dynamic value instead of hardcoded 100%
- Target recalculates live; reserve updated every 30 min as Solcast revises

**True SoC sensor added (2026-05-23)**:
- Local Powerwall gateway (`sensor.tesla_powerwall_2_charge`) floors SoC at backup_reserve_percent — showed 20% when battery was actually at 16%
- Added Tessie `live_status` REST poll → `sensor.tessie_powerwall_charge` for true cloud SoC
- Emergency charge automations now use `sensor.tessie_powerwall_charge` for conditions
- Battery forecast card updated to use `sensor.tessie_powerwall_charge` with gateway as fallback

**30-min averaged home load (2026-05-23)**:
- Stove turning on spiked instantaneous `load_power` to 2.25kW, projecting 10.4 kWh home load to 3pm and wiping out the solar forecast — shortfall jumped to 73%, grid charge target hit 95%
- Added `sensor.home_load_30min_average` (HA statistics platform, mean over 30 min)
- `battery_grid_charge_target` template and forecast card now use 30-min average with instantaneous fallback
- Transient appliances (stove, kettle, dishwasher) no longer distort the forecast or trigger unnecessary grid charging

**Battery forecast card improvements (2026-05-23)**:
- Added **Grid charge target** and **Reserve set** lines — shows computed target alongside what's actually set on the Powerwall, immediately visible if they're out of sync
- Home load line now labelled `avg` to indicate it's the 30-min smoothed figure
- Card confirmed updating in real-time as SoC rises during grid charging

**Solcast config confirmed (2026-05-23)**:
- DC capacity: 6.12 kWp (SolarEdge reported peak)
- AC capacity: ~5 kW (inverter rated output — verify model number)
- Tilt: 0° (flat roof), Azimuth: 0° (irrelevant for flat), Loss factor: 0.9

**Final automation state as of 2026-05-23**:
- ~~All charging: `self_consumption` mode only — autonomous (`time_based_control`) banned permanently~~ *(correction below — 2026-05-24)*
- Emergency low SoC: fires on `sensor.tessie_powerwall_charge < 20%`, sets reserve to `battery_grid_charge_target`
- Morning trigger (9:30am) and solar sponge check (9:45am–2pm, +cheap window opens): set reserve to `battery_grid_charge_target`
- Grid charge target drives all reserve decisions — shortfall = 0 is the goal
- Charging confirmed working at ~1.7kW in self_consumption; sufficient if started early

## 2026-05-18 (continued)

### Solcast credentials
- **API Key**: `I6bgkuZyCcOuP4YeRmJWBaWkIgxoYCPW`
- **Resource ID**: `fd2e-343e-680f-b27e`
- **Rooftop site URL**: `https://api.solcast.com.au/rooftop_sites/fd2e-343e-680f-b27e/forecasts?format=json`
- Panels: flat (tilt ~0°), azimuth irrelevant
- Solcast integration installed via HACS: "HA Solcast PV Solar Forecast Integration" by BJReplay
- Key sensors confirmed working: `forecast_today`, `forecast_tomorrow`, `forecast_remaining_today`, `peak_forecast_today`, `peak_time_today`, `power_now`, `power_in_30_minutes`
- Units: forecast sensors in kWh, power sensors in W, peak_time in UTC (converted to local with `as_local`)
- HA markdown line break = blank line (not trailing spaces)
- Solar Forecast dashboard card added to Solar State view
- Rule 9 cloudy day automation added: fires 7am, charges to 80% if forecast_today < 10 kWh and price in cheap window
- Today (18 May): 7.2 kWh — correctly identified as cloudy day
- Tomorrow (19 May): 9.9 kWh forecast — also cloudy, overnight top-up likely needed
