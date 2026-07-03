# Energy System Control Log

## 2026-06-27 (session 16b — remote access, overseas)

**Remote access attempt**: tried SSH to Pi (192.168.0.67) from iPad via Terminus while overseas on home VPN. VPN only routes 192.168.68.0/24 (TNAS/Mac subnet); Pi is on 192.168.0.x — unreachable directly. TNAS jump host attempt failed: Pi uses key-only SSH auth, key lives on Mac. Mac Studio (192.168.68.70) would work as jump host but Remote Login status unknown. Deferred to next month when back home.

**Outstanding action**: run `build_models.py` on Pi to complete Phase 2.5-B activation. Commands captured in todo.md under "Immediate — DO FIRST when back at home".

---

## 2026-06-27 (session 16 — Rule 26: physics-based overnight hold, 3× refinements)

**Root cause investigation**: Battery charged at 5am at 24¢ despite cheaper prices at 4am and 6am (realized: 19¢ → 24¢ → 19¢). Root cause: in peak months, the peak block intercepts all decisions before `overnight_hold` (Rule 20) can fire. Inside the peak block, `_cheapest_go_hard_slot()` found no cheaper feasible slot — Amber's ~6h forecast window likely showed the spike continuing at 5am. `peak_charge_now` fired.

**Rule 26 — three design iterations:**

**Version 1** (`is_night AND hours_to_2:55 ≥ 6h AND overnight_hold`): time-based, with 25% SoC floor inherited from overnight_hold. Catches the 5am case.

**Version 2** (`is_night AND hours_to_2:55 ≥ 6h AND price > 10¢`): removed the 25% SoC floor — battery should be allowed to drain toward 5% while waiting for Solar Sponge, not forced to charge just because SoC is below 25%.

**Version 3 — final** (`fill_fast_85 < hours_to_2:55 - 2h AND price > 10¢`): replaced `is_night` time-boundary with physics. Key insight: at 7am when `is_night` flips False, `peak_deadline_selfcons` fires if `fill_slow_85 ≥ hours_to_2:55 - 1h`. At 7am, SoC=5%: fill_slow_85≈7.4h ≥ 6.9h → fires and starts slow charging at 24¢. But fill_fast_85=2.16h: autonomous mode can reach 85% from 5% by 12:10pm. The agent should hold, wait for Solar Sponge (10am), then go hard cheaply. The condition `fill_fast_85 < hours_to_2:55 - 2h` means "autonomous has ≥2h of margin" — no point charging at elevated prices when Solar Sponge will be cheaper and we have ample time.

**`peak_charge_now` semantics after Rule 26**: now fires primarily when price ≤ 10¢ (already at Solar Sponge floor, no point waiting). For above-threshold prices, Rule 26 intervenes whenever autonomous has ≥2h of margin.

**Unit tests**: 3 Rule 26 tests added; 1 existing test (`test_peak_charge_now_when_no_cheaper_slot`) updated from 17¢/8:30am scenario (which Rule 26 now correctly intercepts) to 10¢/8:30am (at Solar Sponge floor — charge now). 109 tests, all pass.

**decisions.jsonl / daily_energy.jsonl not available** in this remote session (Pi-only, gitignored). Three-way shadow analysis and daily energy journal review could not be completed. Run `/morning` from Pi or Mac for those.

## 2026-06-10 (session 13 — LLM narrative prompt fixes: FIT/EV confusion, spread definition, Rule 10 deprioritised)

**Morning review**: three demand window passes Jun 7–9 confirmed on Pi (daily_energy.jsonl synced via SSH — Mac copy stale at Jun 5 due to Pi cron not pushing data back to git). Jun 6 recorded as borderline fail (0.56 kW peak, below billing threshold) during the Phase 5 cutover day. data_logger healthy: 178 rows Jun 6–10, 0 undecided rows. Phase 2.5-A (charge rate model) buildable ~2026-06-13.

**Three-way shadow analysis**: LLM↔det 82.3%, LLM↔opt 80.6%, opt↔det 88.5%, three-way consensus 76.6% (252 records). LP divergences predominantly cause (c) — trusts Solcast point forecast and holds while det correctly charges as insurance. Two LP bugs confirmed: (1) charges when target already met (`mpc_charge_grid` at soc=84–100% when det says `target_met`), (2) `solar_sponge_floor` rule not implemented in LP. LP not ready for control path.

**LLM narrative fix 1 — FIT/EV confusion**: at 7:30am LLM cited FIT price (feed-in tariff) as the reason for Zappi mode selection, which is wrong — FIT has no bearing on EV charging except Case 6 (negative-FIT solar dump). Fixed in `SYSTEM_PROMPT`: EV cases block now explicitly labels FIT as relevant *only* to Case 6, and instructs the LLM never to cite FIT for standard Eco/Fast/Eco+ selections.

**LLM narrative fix 2 — spread definition**: at 8:30am LLM defined `spread_c` as `import_price − FIT` (buy vs sell), which is wrong. `spread_c` is `current_import_price − forward_min_c` (buy now vs buy later). Fixed in `SYSTEM_PROMPT`: added explicit CRITICAL block defining spread as the "buy now vs buy later" saving, and explicitly prohibiting the import-minus-FIT definition.

Root cause of both errors: Phase 6 prompt slim removed all arithmetic context but left `fit_price_cents_kwh` visible in state. LLM had no definition of spread and latched onto FIT as the nearest available price variable.

**Rule 10 deprioritised**: decided not to build price spike arbitrage as a manual rule. In peak months, spikes almost always occur inside the 3–9pm demand window — discharging to sell risks a demand charge breach (~$100/month) that dwarfs any FIT revenue. Outside the demand window, spikes are rare on EA116. Revisit only if LP becomes authoritative (it can model the trade-off explicitly). `energy_rules.md` Rule 10 and `todo.md` updated to reflect this decision.

## 2026-06-09 (session 12 — race condition fix, peak_deadline_autonomous fix, sensor watchdog, data_logger dedup, Phase 6 prompt slim)

**Race condition: `battery_low_soc_emergency_charge` automation vs det layer HOLD.** HA automation fired seconds before agent cycle, setting reserve=20%. Det layer then ran HOLD verdict but only cleared reserve when `reserve > SoC` — sensor lag (slow Tessie poll) meant agent read stale reserve=5% and left 20% in place, causing grid charging. Two-part fix: (1) removed 20% minimum floor from automation action (now uses `grid_charge_target` directly, capped at 95%), (2) HOLD verdict now unconditionally sets reserve=5% when reserve > 5%, regardless of stale SoC sensor. Root cause: HA automation existed to protect against running down to 0% on a genuinely cloudy day — but the det layer already handles this via `overnight_hold` and `solar_sponge_floor` rules. 20% floor was vestigial and conflicting.

**`peak_deadline_autonomous` firing unnecessarily.** When current price was already the cheapest in the forecast horizon (Solar Sponge price), the deadline rule was still escalating to autonomous because `fill_slow_85 >= hours_to_2_55`. Fixed with price check: if `price <= forward_min` (cheapest upcoming price), use `self_consumption` (no need to rush to autonomous when we're already in the cheapest window).

**SolarEdge sensor stuck at 27W for 3 days.** Not a code bug. Router WiFi port was turned off (firmware update?). Inverter reconnected after WiFi restored. HA integration was silently returning stale data. Diagnosis: `sensor.solaredge_energy_today` enabled as daily validation metric.

**Sensor watchdog automation added** (`sensor_watchdog_morning`). Fires at 09:30 daily and on HA start. Checks 8 sensors (SolarEdge, Tessie, Solcast ×2, Amber ×2, Powerwall ×2) for `unavailable`/`unknown` state or >2h staleness. Sends persistent HA notification if any sensor is stale.

**data_logger double-insert bug fixed.** `get_current_state()` was called twice per cycle (once by `run_agent()`, once by LLM tool) — each call inserted a new row via `log_cycle_start()`, leaving the first row permanently undecided. Fix: `_cycle_context["db_cycle_id"]` guard — only inserts on first call per cycle. 141 orphaned rows cleaned from Pi DB via SQL (VACUUM had to run in separate connection outside transaction).

**Phase 6 — system prompt slimmed.** Prompt reduced from ~470 lines to ~65 lines (86% reduction). All deadline maths, spread tables, fill-time calculations, wait-and-go-hard strategy, deferral limits, Solar Sponge floor implementation, and escalation rules removed — the deterministic layer handles all of these. LLM prompt now states its role explicitly: narrative logger only, `set_*` calls are no-ops, summarise what the rule layer did from the shadow block.

## 2026-06-07 (session 11 — Tesla app reserve fix, hold+reserve bug fix, git cleanup)

**Root cause of reserve=80% drift during demand window — confirmed and fixed.**

June 6 JSONL review showed `reserve_before=80%` at every demand window cycle despite the pre-flight guard setting 5% each cycle. Investigation found: the **Tesla app had backup reserve set to 80%**. When Tessie's cloud command to set 5% didn't fully persist to Powerwall hardware (e.g. Tessie session lag or brief disconnection), the Tesla firmware fell back to its app-stored value of 80%.

Fix: Tesla app backup reserve manually set to **5%**. Now the firmware fallback is safe — agent/guard commands always override upward as needed, but any missed command defaults to 5% (harmless) rather than 80% (causes demand window breach).

This explains the full chain:
- Session 9 (Jun 5): reserve_before=80% throughout demand window — Tesla app at 80%.
- Session 10 (Jun 6): same pattern, corrected manually at 18:48 by LLM narrative.
- Session 11 (Jun 7): Tesla app reset to 5%. Pre-flight guard + `_guarded_set_reserve()` should now hold clean.

Also identified: 12 automations listed as "disabled" in CONTEXT.md have no `enabled: false` in automations.yaml — HA UI enable/disable state is not reflected in the YAML. Confirmed via HA UI: `battery_winter_overnight_precharge` and `battery_cloudy_day_topup` are disabled. Remaining 10 need verification. Most critical to confirm disabled: `battery_cheap_window_autonomous_charge` (sets reserve=100% when cheap window opens — directly conflicts with agent).

**Hold + reserve > SoC bug fixed.** `_execute_deterministic_verdict()` was doing nothing on hold verdicts. If reserve was above SoC (e.g. 80% reserve, 28% SoC), the Powerwall would grid-charge at whatever price was current even while the agent was holding for a cheaper slot. Fix: any hold verdict now actively sets reserve=5% if reserve > SoC. 94 tests pass.

**EV price thresholds changed to `<=`** — slider value is now inclusive. "EV Standard Price = 12¢" now charges at exactly 12¢, not only below it.

**Operational data files untracked from git** — `decisions.jsonl`, `daily_energy.jsonl`, `agent_decisions.log` now gitignored and Pi-only. Standard Pi command: `git pull && agent/venv/bin/python agent/energy_agent.py`

**5% battery floor vs 0%**: 5% is conservative choice. Tesla firmware has its own cell-protection floor regardless. 0% would give ~0.67 kWh more discharge during demand window. Not changed — low priority.

**Solar forecast inaccuracy**: Solcast overestimates by ~2.5× in June (actual/forecast ratio 0.39–0.45 all week). `solar_unreliable` flag fires correctly and det layer ignores solar forecast when set. Simple scalar correction (~0.4×) could be applied to `remaining_today` now using existing decisions.jsonl data. Proper Model 1 (OLS per-hour correction) buildable ~2026-06-20.

**Phase 5 cutover verified clean.** All June 6 charging decisions driven by deterministic layer. LLM correctly narrative-only throughout. Pre-charge: autonomous from 09:31 (SoC 21%→82%), reverted to self_consumption at 12:00, target reached by 13:30. Demand window entered at 100% SoC.

---

## 2026-06-06 (session 10 continued — demand window reserve bug, Phase 5 cutover, data logger)

**Demand window reserve stuck at 80% — 7 consecutive cycles (18:30–21:00).**
Agent was confusing the pre-charge target (reach 85% SoC by 2:55pm) with a floor to maintain during the demand window. Pre-flight guard dropped reserve to 5% at cycle start, but LLM then overrode it back to 85% reasoning "SoC < 85%, I should charge." Battery couldn't discharge; all home load came from grid.

Two fixes deployed:
1. `_guarded_set_reserve()` in TOOL_MAP — blocks any set_reserve(N > 10) during 3–9pm peak months. Returns error string without API call.
2. Explicit system prompt instruction: 85% target is a pre-charge goal, NOT a floor during the window. Reserve must stay at 5% during demand window.

**Phase 5 cutover — deterministic layer now authoritative.**
Root cause of the demand window bug: LLM reasons from first principles and can construct valid-sounding wrong answers. Rule tree cannot. `DETERMINISTIC_AUTHORITATIVE = True` added. `compute_decision_context()` now executes all set_* actions before the LLM runs. LLM runs narrative-only; its set_* calls are no-op'd. `log_decision()` unchanged — JSONL written per cycle. Kill-switch: `DETERMINISTIC_AUTHORITATIVE = False`.

**data_logger.py wired into energy_agent.py.**
`energy_log.db` now created on startup. Three call sites added (all guarded by `_HAVE_DATA_LOGGER`): `log_cycle_start` after state read, `log_price_forecast` after forecast read, `log_agent_decision` inside `log_decision`. Phase 2.5-A clock started — charge rate model buildable after ~1 week (target ~2026-06-13).

## 2026-06-06 (session 10 — premature charging bug, Tessie SoC guard, hold ≠ arming)

**Bug identified: agent charged battery at 8:30am while intending to wait for Solar Sponge.**

At ~07:45am battery was at 18% SoC with Solar Sponge (7–11¢) starting at 10am, ~2.25h away. The agent correctly reasoned "wait for Solar Sponge" but executed `set_reserve(85%)` as a "preparation" step — not understanding that `backup_reserve_percent > current_soc` is the Powerwall's charging trigger. The moment reserve was set to 85%, the Powerwall started pulling from grid at 14¢ — exactly the price we were waiting to avoid.

Root cause: the 20% "emergency charge" threshold was incorrectly being used as a floor to top up before a cheap window. The right behaviour: if the battery can survive to the cheap window above the Powerwall's 5% absolute floor, take no action at all.

**Fix 1 — Hold ≠ arming (system prompt CRITICAL block).**

Added explicit guidance: *"Do NOT raise reserve while waiting for a cheaper window."* Reserve should only be raised above 5% if `projected_soc ≤ 5%` — i.e., the battery will hit the absolute floor before the window opens.

Formula: `projected_soc = current_soc − (hours_to_window × home_load_kw / 13.5 × 100)`

- `projected_soc > 5%` → leave reserve at 5%. Battery survives; charge cheap.
- `projected_soc ≤ 5%` → set reserve to `drain_to_window + 8%` — survival minimum only.

Example from the bug: SoC=18%, load=0.6kW, 2.25h to Solar Sponge → projected = 18 − 10 = 8% → above floor → **no action**.

Updated Rule 1 (Low SoC survival check) and Rule 7 Step 1 in `energy_rules.md` to match.

**Fix 2 — Tessie SoC=0 sanity guard (`_build_battery_state()`).**

Overnight log showed Tessie returning SoC=0% at 06:30am while gateway correctly read 27%. Old code likely panicked and set reserve=80%, causing unnecessary 14¢ grid charging for the 07:00 and 07:30am cycles (logs missing — confirms old code ran). The 08:07am manual run found reserve=80% with SoC=26%, diagnosed the false alarm, and correctly dropped reserve to 5%.

Fix: new `_build_battery_state()` function. If Tessie returns 0%, or gateway reads more than 15% above Tessie when gateway is reliable (`gateway > reserve`), substitute gateway and set `tessie_soc_failed=True`. The `gateway > reserve` guard correctly excludes cases where reserve is high and gateway is floor-clipped — it only fires when gateway is genuinely above the reserve floor and therefore trustworthy.

Three new fields in battery state dict: `soc_tessie_pct`, `soc_gateway_pct`, `tessie_soc_failed`. System prompt updated to explain these fields and when `soc_pct` may differ from Tessie.

**Overnight observation (Jun 5/6 midnight–06:30am):** 12 consecutive correct overnight holds (Rule 20 — overnight hold). Agent correctly deferred charging to Solar Sponge each cycle. SoC drained from ~75% to 27% overnight without grid charging. All 12 cycles showed `deferral_detected=True` but correctly overridden by confirmed-cheap-window logic — correct behaviour.

**Manual Pi run (08:07am):** venv path issue surfaced — `python3 agent/energy_agent.py` fails with ModuleNotFoundError; must use `agent/venv/bin/python agent/energy_agent.py`. Agent correctly found reserve stuck at 80% (from Tessie=0 panic), dropped it to 5% via Tessie API. Solar Sponge then charged correctly.

**08:10am escalation to autonomous — discussed:** the agent escalated to autonomous at ~08:10am (manual run). User flagged this was too early — autonomous should only be warranted when we're 3h from the demand window and short of target. Noted as expected behaviour: graduated solar underdelivery response logic (self_consumption first, autonomous only near the deadline) is already implemented in Rules 22/23 but may need threshold tuning as June data accumulates.

**Discussed: LLM commentary is the dominant Anthropic cost (~$97/month).** Re-architecture Phase 7 (selective narrative — skip LLM on routine hold/overnight cycles, call LLM only on high-stakes decisions) would reduce this to near-zero. Phase 5 cutover is the prerequisite.

**Committed: 5 commits (8c01c1f, 045a074, 2eaf092, 04cc062, 854893c + 88cc39a gitignore).** All 86 tests pass. Pi picks up changes on next cron cycle.

**Second bug cluster discovered — premature charging again at 09:25am.**

Despite the hold≠arming CRITICAL block, agent charged at 11¢ at 09:25am. Root cause: **decision tree ordering bug** in `compute_decision_context()`. `peak_deadline_selfcons` was evaluated before `wait_for_cheap_go_hard`. At 09:25am `fill_slow_85=5.24h ≥ deadline-1h=4.5h` → `peak_deadline_selfcons` fired and the LLM followed it, setting reserve=85% at 11¢ when Solar Sponge (7¢) was 35 minutes away.

**Fix 3 — Decision tree reorder (3957e8c):** moved `wait_for_cheap_go_hard` and Solar Sponge cases (`peak_sponge_go_hard`, `peak_sponge_selfcons`, `solar_sponge_floor`) before `peak_deadline_selfcons`. `peak_deadline_selfcons` now only fires if no cheaper go-hard slot exists AND there's no Solar Sponge window available.

**Fix 4 — `_detect_zero_solar` false positive suppression (00fac85):** On the same morning (clear sky), agent declared `zero_solar_day=True` before 10am because the lagged SolarEdge sensor showed ~27W throughout the morning ramp. Fix: if `solcast_remaining > 2.0 kWh AND now_h < 10.0`, suppress zero-solar. At 10am+ actual zero readings become valid evidence.

**Fix 5 — Solar sensor switch (9a67c27):** Root cause of the 27W reading: agent was reading `sensor.solaredge_current_power` (SolarEdge cloud API, ~15-min lag). Real-time sensor is `sensor.solar_power_w` (template wrapping Powerwall gateway `sensor.tesla_powerwall_2_solar_power`, real-time). Changed ENTITIES dict to use `sensor.solar_power_w`. At the time of detection: lagged sensor = 27W, real-time sensor = 354–412W.

**Fix 6 — `_detect_zero_solar` stale history override (073a6e2):** After the sensor switch, stale JSONL records from the old sensor still showed ≤0.1 kW, keeping `zero_solar_day=True` for several cycles. Fix: if current reading > 0.1 kW and sensor is available, return False immediately regardless of history.

**Dashboard card fix — Solar Forecast vs Actual (apexcharts):** Card second series was using `sensor.tesla_powerwall_2_solar_power` (kW = 0.412) with `transform: return x / 1000` → displayed 0.000412, rounded to "0 W". Fixed by switching to `sensor.tesla_powerwall_2_solar_power` without transform (native kW scale) and removing `* 1000` from the Solcast `data_generator` — both series now in kW. Using the Powerwall sensor rather than `sensor.solar_power_w` because the Powerwall sensor has recorder history; `solar_power_w` is a template sensor that may not be recorded.

## 2026-06-05 (session 9 continued — Pi deployment, Cloudflare Tunnel, repo consolidation)

**Raspberry Pi 5 deployment — agent now running on Pi.**

Pi details: `simonmonk@energypi.local` / `192.168.0.67`, running Raspberry Pi OS Lite 64-bit on a 1TB SSD (confirmed `/dev/sda2`, not SD card). Python 3.13.5 installed.

Steps completed:
- SSH key auth set up (Mac → Pi)
- Pi SSH key added to GitHub (`energypi` key)
- Cloned `home-energy-agent` repo via SSH to `~/home-energy-agent`
- Python venv at `~/home-energy-agent/agent/venv` with all deps (anthropic, pytz, requests, scipy, numpy)
- `agent/.env` created on Pi with `HA_URL=http://192.168.68.70:8123` + all API keys
- `HA_URL`, `HA_TOKEN`, `TESSIE_TOKEN`, `TESSIE_SITE_ID` made env-var-configurable in `energy_agent.py` (defaults unchanged for Mac; Pi overrides via `.env`)
- Dry run confirmed: Pi correctly reads HA sensors on Mac Studio at LAN IP
- **Mac cron removed** — Pi now owns all three cron jobs
- Pi cron: every 30 min with `git pull -q` before each run (deploy = `git push` from Mac)
- Pi cron: daily energy log at 21:05, demand window summary hourly

**Cloudflare Tunnel — HA accessible at `https://agent.sol.io`.**

- `cloudflared` installed on Pi from Cloudflare's official APT repo
- Tunnel `home-energy-agent` created (ID: `1f5203ff-866e-4c28-ab13-c55009ccc2b9`)
- `cloudflared` config at `/etc/cloudflared/config.yml` pointing to `http://192.168.68.70:8123`
- DNS CNAME added manually in Cloudflare dashboard: `agent.sol.io → <tunnel-id>.cfargotunnel.com` (Proxied)
- `cloudflared` running as systemd service, connected via Sydney edge (`syd06`, `syd01`)
- HA `configuration.yaml` updated: `http: use_x_forwarded_for: true` + `trusted_proxies` includes Pi subnet `192.168.0.0/24`, Mac subnet `192.168.68.0/24`, Docker bridge `172.16.0.0/12`
- HA external URL set to `https://agent.sol.io` in HA Settings → Network
- **Confirmed working**: Chrome ✅, HA Mac app ✅, iOS HA app on WiFi ✅, iOS HA app on mobile data ✅
- Safari on Mac fails (iCloud Private Relay interference) — not a concern, HA app preferred

**Repo consolidation.**

- `home-energy-automation` (phone repo, older state) archived and deleted
- `home-energy-console` renamed to `home-energy-agent` on GitHub
- Local git remote updated to `https://github.com/OriginalNomad/home-energy-agent.git`
- `ARCHITECTURE.md` and `agent/data_logger.py` pulled from phone branch and merged in
- `/morning` skill updated to read ARCHITECTURE.md (step 6) and added "Architecture progress" section (6th standup item)

**Dev workflow going forward:**
- Develop on Mac Studio → `git push` → Pi pulls automatically before each cron run
- HA remains on Mac Studio for now; future session: migrate HA to Pi via Docker

## 2026-06-05 (session 9 — receding horizon charging, NameError bug fix, battery_grid_charge_target fix)

**Morning standup:** Jun 4 demand window passed (97% SoC at 3pm, 0.048kW peak import). Three-way divergence: LLM↔det 74%, LLM↔opt 76%, opt↔det 94%, three-way consensus 75%. LP still diverges on cloudy mornings (cause-c: trusts solar point forecast). Phase 5 cutover blocked until LP consumes `solar_unreliable`.

**Bug 1 fixed: `_demand_reserve_guard_fired` NameError broke JSONL + HA notifications since June 2.**

Root cause: variable was set inside `run_agent()` but never initialised at module level. Every cycle since session 6 (Jun 2) raised `NameError` in `log_decision()`, silently killing: JSONL writes (no records after 2026-06-04T23:30 AEST), `persistent_notification` pushes, HA logbook entries, and dashboard helper updates (`input_text.battery_decision_action` stuck on Jun 4 13:30). Plain-text log continued because it's the first line of `log_decision()`, before the crash. Fix: added `_demand_reserve_guard_fired: bool = False` at module level.

**Bug 2 fixed: `battery_grid_charge_target` incorrectly reverted autonomous mode immediately on unreliable-solar days.**

Root cause: template sensor computed `95 − (solcast_remaining / 13.5 × 100)`. On Jun 5 morning with Solcast claiming 11kWh remaining, the sensor returned 13%. Battery at 26% triggered `battery_autonomous_revert_target_reached` immediately (`26 >= 13`), reverting autonomous mode within 30s and blocking the fast charge. Fix: added peak-month floor — in peak months before 3pm, `battery_grid_charge_target` is clamped to a minimum of 85%. Sensor now shows 85% on cloudy peak days instead of an over-optimistic Solcast-derived low value. Live reload confirmed: sensor updated to 85%.

**New strategy: wait-and-go-hard + receding horizon Solar Sponge charging.**

User insight: on peak days with grid charge needed, the correct strategy is not "charge at 1.7kW now" but "find the cheapest upcoming slot where fast-fill still fits the deadline, wait for it, then go hard at 5kW." Each cycle should also reassess the charging rate as solar arrives — drop from 5kW to 1.7kW if fill_slow now fits; hold if solar is covering the remaining gap.

Implemented:

- `_cheapest_go_hard_slot()`: scans price forecast for cheapest slot where `hours_until + fill_fast_85h + 0.5h ≤ deadline`. Conservative SoC projection (home load drain, no solar credit). Returns (price, hours_until) or None.
- `wait_for_cheap_go_hard`: peak month, grid charge needed, cheaper feasible slot ≥1¢ ahead → hold and wait.
- `peak_charge_now`: peak month, grid charge needed, no cheaper slot → charge at self_consumption now.
- `peak_sponge_go_hard`: in Solar Sponge, fill_slow ≥ deadline − 1h → autonomous (tight, must go hard).
- `peak_sponge_selfcons`: in Solar Sponge, fill_slow fits comfortably → self_consumption. Next cycle reassesses as solar data improves.
- Receding horizon principle added to system prompt: every cycle is an independent optimization. Mode can change as solar improves or prices shift. `battery_autonomous_revert_target_reached` is a safety net, not the primary rate controller.
- `go_hard_slot` field exposed in decision context, REFERENCE block, and JSONL `computed_context`.

86 unit tests, all pass.

**Live validation (Jun 5, 9:21am):** agent correctly held at 8:30am (17¢, cheaper window ahead at ~14¢), then escalated to autonomous + reserve=100% at 9:21am when `fill_slow=4.69h, deadline=5.58h, margin=0.89h < 1h buffer, no cheaper slot ahead`. After Bug 2 fix, autonomous mode held correctly until battery reached 85%.

**Discussed: 85% fixed target is too blunt on flat-price days with variable solar.** On days with flat prices (no cheaper window) and unreliable solar, the optimal target should be derived from expected 3-9pm load, not always 85%. The LP optimizer naturally computes partial-fill targets. This is a future improvement — for today, 85% is the correct conservative choice given solar at 17% of forecast.

**Architecture note:** `battery_autonomous_revert_target_reached` automation uses `sensor.battery_grid_charge_target` as the stop condition. With the peak-month 85% floor now in place, this correctly stops autonomous charging at the demand-window target on all solar conditions.

## 2026-06-04 (session 8 — EV charging logic rework)

**Morning standup:** Jun 3 demand window passed (98% SoC at 3pm, 0.008kW peak import). Three-way divergence analysis: LLM↔det 83%, LLM↔opt 77%, opt↔det 93%. LP optimiser not yet Phase 5 ready — it doesn't consume the `solar_unreliable` flag, so it systematically holds on cloudy mornings while LLM+det correctly charge. LP cutover blocked until this is fixed.

**EV charging logic rewritten (cause: EV was charging at 13¢ despite user expecting 10¢ threshold).**

Root cause: the old Cases 4/5 gated on `amber_in_cheap_window` (Amber's binary sensor), which is True throughout the Solar Sponge window regardless of actual price. `ev_ultra_cheap_threshold_c` only controlled the "go Fast" case (Case 2), not whether EV charged at all.

Fix: replaced `amber_in_cheap_window` + 3-phase Eco/Fast sub-logic with two explicit price sliders:
- `ev_standard_price_c` (new entity, default 10¢) — Eco (slow charge) when price below this
- `ev_ultra_cheap_threshold_c` (existing, default 5¢) — Fast when price below this
- `ev_min_charge_price_c` (new entity, default 20¢) — ceiling for below-minimum emergency Fast; replaces hardcoded 20¢

`ev_eco_gap_c` entity removed (no longer used). Demand window always forces Eco+ regardless of price.

New priority order: demand window → below-min → FIT negative → target met → ultra cheap → standard → Eco+ default.

All three new sliders added to `configuration.yaml` and wired into agent. 75 tests pass. Verified live: 13:30 cycle at 11¢ with standard threshold 10¢ correctly logged `ev_price_too_high → Eco+`.

## 2026-06-03 (session 7 — solar-sufficiency hold, LP horizon extension, schema fix)

**Items 1–4 from morning standup — all implemented and tested.**

**Item 1 + 3 — Deterministic layer: home load deduction + solar-sufficiency hold**

Root cause identified for the `peak_target_met` at SoC=25% divergence seen in the morning standup: `kwh_needed_85` was computed against raw Solcast `remaining` kWh, not net of home consumption. On a sunny day with 10+ kWh remaining, the battery looked "covered" even at 25% SoC because home load consuming 7-10 kWh of that solar was not being deducted.

Fix: `net_expected_solar = max(expected_solar - home_load_kw * solar_window_h, 0.0)` is now used in both the peak and non-peak branches. For peak: window = hours_to_2:55pm. For non-peak: capped at 7h.

`peak_target_met` renamed to `peak_solar_will_cover` when SoC < 85% — the two cases are semantically different and were causing confusion in divergence analysis.

New `solar_will_cover` rule in the non-peak path: if reliable solar forecast (net of home load) can cover the gap to cost_target before 1pm, hold. This encodes the human insight from the morning standup: on a sunny forecast day, hold-until-you-must is correct — not trickle-charging from grid.

68 tests, all pass (was 60).

**Item 2 — LP optimiser horizon extension**

The core divergence cause: Amber's ~6h forecast window ends before the 15:00–21:00 demand window on peak mornings, so the LP never sees the `demand_penalty_c = 1000 ¢/kWh` on those slots and holds/solar-only instead of pre-charging.

Fix: `_build_hourly_price_model()` builds a per-hour-of-day median price table from the last 7 days of decisions.jsonl. `_extend_forecast_to_demand_window()` appends synthetic 30-min slots from the end of the Amber forecast to 22:00 using this model. The LP optimizer now receives the extended forecast each cycle.

New optimizer test 7: cloudy peak morning + zero solar + short horizon → LP may hold; same scenario + extended horizon showing demand window → LP pre-charges. All 12 optimizer tests pass.

**Item 4 — daily_energy.jsonl schema**

- `solar.accuracy` renamed to `solar.forecast_vs_actual_ratio` (explicit semantics; 0.17 on Jun 2 = solar delivered only 17% of forecast)
- `agent.forecast_accuracy_category` added: "good"/"poor"/"unreliable" based on fraction of daytime cycles with unreliable solar. Jun 2 would score "unreliable"; Jun 1 would score "good". This is the #1 predictor of demand-window breach days for a future learning agent.

**Demand window today (Jun 3):**
SoC was 30% at 08:30, reserve set to 85% via Solar Sponge (7¢). Watch whether solar delivers today and whether the agent needs to escalate to autonomous. The new `solar_will_cover` rule should prevent unnecessary trickle-charging if the solar forecast is accurate.

---

## 2026-06-02 (session 6 continued — demand window incident + HA rest_command fix)

**Demand window breach incident — 7pm grid import during cooking**

At ~7pm user noticed grid was powering cooking load instead of the battery, during the demand window (3–9pm peak month). Root cause chain:

1. Agent charged battery to 81% today (target was 85%) — SoC stalled at 81%, reserve stayed at 80% (the charging floor)
2. At 2:55pm, `battery_pre_demand_window_reset` automation fired but **immediately errored**: `Action rest_command.powerwall_set_mode not found`
3. Reserve stayed at 80% through the entire demand window — battery pinned at its own floor, unable to discharge
4. When cooking spike hit at 7pm, Powerwall had nowhere to go → grid import

**Root cause: `rest_command` integration had been broken since HA restart on June 1 06:23.** The `powerwall_set_mode` payload was truncated in the container config at startup (`{{ mode }` instead of `{{ mode }}"}`). The session 5 fix corrected the file at 17:06 that day, but HA does not retry integrations that failed at startup — `reload_all` doesn't help. The integration was silently dead for ~36 hours.

**Immediate fix:** called Tessie API directly to drop reserve to 5% (`{"backup_reserve_percent": 5}` → 200 OK). Battery resumed discharging; grid import dropped to ~0W. SoC was 62% with reserve now 5%.

**Permanent fix:** restarted HA via `POST /api/services/homeassistant/restart`. After restart both `rest_command.powerwall_set_backup_reserve` and `rest_command.powerwall_set_mode` loaded cleanly. `battery_pre_demand_window_reset` will work correctly from tomorrow.

**Verified post-restart state:** SoC=62%, reserve=5%, mode=self_consumption, grid=0.001kW — battery discharging normally, demand window safe for remainder of evening.

**Agent pre-flight guard implemented** (`agent/energy_agent.py`, `run_agent()` — added same session):
- **Demand-window reserve guard**: at cycle start, before LLM runs — if peak month AND 15:00–21:00 AND `reserve > 10%`, calls `set_powerwall_reserve(5)` via Tessie directly. Bypasses HA rest_commands entirely. Logs warning to stderr. Would have caught tonight's breach at the 15:00 cycle instead of 19:00.
- **HA rest_command health check**: each cycle checks `/api/services` for `rest_command` domain; warns loudly if missing. Would have surfaced the June 1 failure immediately rather than 36h later.
- Both additions are in a `try/except` so failures are non-fatal to the main cycle.

## 2026-06-02 (session 6 — demand-window outcome logging + HA monitor cards)

**Built comprehensive daily energy journal** — a per-day record of everything a learning agent needs to reconstruct what happened and why, persisted to `daily_energy.jsonl` (survives HA recorder's ~10-day rolloff) and surfaced in HA via two dashboard cards.

- **`agent/log_daily_energy.py`** — comprehensive daily energy journal, cron at 21:05. Queries HA history API for the full day + reads `decisions.jsonl`. One JSON record per day to `agent/daily_energy.jsonl`, capturing:
  - **Solar**: Solcast forecast vs actual inverter output, accuracy ratio
  - **Battery**: SoC at midnight / 9am / 3pm / 9pm, min SoC during demand window
  - **Load**: total home consumption, demand-window load
  - **Grid**: total import/export, import during Solar Sponge and demand window separately
  - **Price**: min/max/mean for overnight, Solar Sponge, demand window, full day, and FIT
  - **Demand window**: billing-accurate pass/fail (EA116 peak 30-min avg kW, threshold 0.10 kW)
  - **Agent**: total/charge cycles, modes used, rules fired (deterministic + optimiser), shadow match rates, forecast unreliable count, 3pm SoC goal vs projected, daily API cost
- **Supersedes `log_demand_window.py`** — the old file only tracked demand window pass/fail. The new journal captures the full context so a future analyst/learning agent can answer "why did this day pass or fail?" without any other data source.
- **Metric correction (important):** first cut measured kWh imported and flagged June 1 as FAIL on 0.058 kWh. But per `ea116_tariff.md` §1 / Rule 2, the bill is set by the **single highest 30-minute *average* import (kW)** in the month — not kWh, not instantaneous peak. Reworked to compute the peak clock-aligned 30-min-average import. June 1 → **PASS at 0.038 kW** (38 W avg, worst block 20:30). Pass threshold = 0.10 kW.
- **`agent/demand_window_summary.py`** — reads `daily_energy.jsonl` and **pushes `sensor.demand_window_monitor` into HA via the REST API** (state = this month's peak 30-min import kW = the billed number; attributes = rolling 30-day per-day history). Crons: daily 21:05 (after recompute) + hourly (keeps the sensor alive across HA restarts, since `/api/states` is non-persistent).
- **Why REST-push, not a template/utility_meter sensor:** the agent runs on the host; HA runs isolated in Docker and its `/config` has diverged from both the repo and the docker-compose host-mount path. Pushing state from the host sidesteps the divergence entirely and keeps the JSON as the single source of truth.
- **Two Markdown dashboard cards** (native, no custom-card dependency) driven by that sensor's attributes: (1) peak-30min-import bars per day + month headline; (2) pass/fail timeline with worst 30-min block and min SoC. Both Jinja templates validated against live HA via `/api/template`.
- **Backfill patterns visible in first 7 days**: May 29 hit SoC 5% at 9am with 17 kWh grid import and Solcast accuracy 0.45; June 1 had Solcast accuracy only 0.41 (worse forecast) but reached 99% by 3pm — the difference: June is peak month so the agent prioritised the deadline.

**Config-divergence finding (flagged for follow-up):** the running HA is the Docker container (up 7 days, serving :8123); its `/config/configuration.yaml` (Jun 1 17:06) matches the repo copy but is ~50 min ahead of the docker-compose bind-mount path `/Users/simonmonk/homeassistant/config` (Jun 1 16:17). So edits to the repo `config/` do not reliably reach the running HA. Also seen in the live log: `rest_command.powerwall_set_backup_reserve not found` at 2026-06-01 11:00 — the agent's reserve command had been failing to load that morning (the Content-Type/payload bug); container config mtime is after the fix, so likely resolved, but worth confirming the rest_commands now load cleanly.

## 2026-06-01 (session 5 — overnight hold rule, card fixes, June 1 peak month starts)

**Overnight hold rule added (Rule 20):**

Observed 2026-05-31 20:30: agent charged battery to 95% overnight at 14-15¢ despite Solar Sponge at 6-8¢ being 8-12h away. Root cause: the Amber 12h forecast didn't reach tomorrow's Solar Sponge prices, so `forward_min` showed 13¢ (not 6¢), and the deferral_limit fired — "no cheaper window visible, charge now."

Fix: added `overnight_hold` flag — when nighttime (20:00–07:00) AND price > `SOLAR_SPONGE_PRICE_THRESHOLD` (10¢) AND SoC > 25%, hold and wait for Solar Sponge. Solar Sponge is a structural tariff feature, not a forecast — it's always there. The demand window only applies 3–9pm; Rule 13 morning deadline maths handles peak months from 9am. No pre-charge needed.

`overnight_hold` inserted into decision tree BEFORE `deferral_limit` so it can't be overridden by repeated holds. Peak month exception: if tomorrow solar outlook is overcast AND price < 15¢, pre-charge is justified — but Rule 13 handles this during daylight cycles, not overnight. 60 unit tests pass.

**Battery Forecast card fixes:**
1. Evening mode (after 3pm): now shows charging status when active ("🔌 charging 1.65 kW → 95% in ~2h") rather than always showing "solar done · battery discharging"
2. Goal/projected section hidden after 3pm (non-peak) — not relevant when solar is done
3. Reserve display: removed stale `input_number.battery_decision_reserve_set` helper value; now reads `sensor.powerwall_backup_reserve` (Tessie) only — eliminates "If Consumption · Reserve: 80% (Powerwall: 5%)" confusion
4. Goal SoC: reads agent's actual reserve target (when > 20%) rather than hardcoded 80/85%

**Overnight behaviour observed (May 31 evening):**
- 16:30: agent correctly dropped reserve to 5%, letting battery discharge
- 17:00–19:30: correctly held, waiting for 11¢ overnight window
- 20:30: deferral_limit incorrectly fired, charged to 95% at 14-15¢
- This is the exact scenario overnight_hold prevents from June 1 onwards

**June 1 peak month — first live demand window test:**
System enters first peak month. Agent must now:
- Apply `is_peak_month = True` from first morning cycle
- Run 85% SoC by 2:55pm deadline maths every cycle from 9am
- `battery_pre_demand_window_reset` fires at 2:55pm as backstop

**HA configuration.yaml — two bugs fixed (session 5 continuation):**

1. **`Content-Type` key was crashing HA's annotated YAML loader** — HA's custom `annotatedyaml` parser was failing to parse `Content-Type: "application/json"` as a YAML key (error: "while scanning a simple key… could not find expected ':'"). Fix: quote the key as `"Content-Type": "application/json"`. Applied to both `rest_command` blocks. The error had been silently present since at least 06:15 on June 1 (pre-dating this session); HA was loading but warning.

2. **`rest_command` payload truncated** — the `configuration.yaml` payload line `{"default_real_mode": "{{ mode }}"}` was truncated in the file, ending at `{{ mode }` (missing `}}"`). Cause unclear (likely a previous partial save). Fixed by rewriting the `rest_command` section and switching both payloads from `>-` block scalar to single-quoted strings (`'{"default_real_mode": "{{ mode }}"}'`), which is the recommended format for Jinja2 templates in rest_command payloads.

3. **`card-mod` installed as a frontend module** — added `extra_module_url: /hacsfiles/lovelace-card-mod/card-mod.js?hacstag=190927524421` to `frontend:` in `configuration.yaml`. card-mod confirmed loading (card background colour test passed). Attempted to use it for icon colour customisation on binary sensor entity rows; abandoned as low-priority (shadow DOM piercing complexity not worth it for cosmetic change).

**Note on HA config sync:** The Docker container at `/config/` diverges from the repo copy at `config/`. The container runs HA's actual config; the repo is a separate copy maintained manually. Changes made to the container via `docker exec` (not via editing the repo file) include the `Content-Type` fix and payload rewrite.

**June 1 demand window — PASSED. First live peak-month test successful.** ✅

Complete cycle trace:
- 00:00–07:30: agent held overnight (overnight_hold Rule 20 working), SoC drained 72%→50% through home load. No overnight charging at 11–16¢. Rule 20 validation: ✅
- 09:30: Solar Sponge begins, agent raised reserve to 85%, charging started at 11¢
- 09:30–14:30: Solar + grid charged 39%→96% at 7–11¢ (Solar Sponge window). Agent correctly used `set_reserve(85%)` and held mode at `self_consumption`.
- 13:30: demand target (85%) reached. Agent held from 14:30 onwards.
- 15:00: SoC = **99%** entering the demand window. No grid import needed.
- 15:30: SoC = **100%**. Battery fully charged.
- 15:00–17:30: agent held throughout the demand window, battery discharging naturally (100%→91%) to power home load. Zero grid imports. Rule 2 (no demand window import) maintained. ✅
- `battery_pre_demand_window_reset` automation (2:55pm backstop): did not need to fire — agent covered it.

This is the first confirmation that the full system (overnight_hold + Solar Sponge + peak-month deadline maths + demand window hold) works end-to-end on a live peak day.

**LP optimiser during demand window:** correctly said `hold/mpc_solar_only` through the charging phase (solar filling battery) and `hold/mpc_hold` during the demand window (no charging needed). Agrees with LLM+rules on this well-defined scenario — promising signal for the live three-way review.

**Architecture assessment → PRODUCT.md "Optimisation Engine — Depth".** Core finding: the system approximates, with an ever-growing hand-tuned rule stack, the solution to a finite-horizon optimal control problem. The deterministic shadow layer is a good *bridge* (testable) but still a rule tree needing endless edge-case patches; the right endpoint is to **compute** the optimum (LP/MPC), which is also the Sol direction. Wrote up the full target architecture: what MPC is + why receding-horizon re-planning is robust to forecast error; the forecast-uncertainty toolkit (per-site calibration → ensemble → nowcasting → distributions/robust MPC against a risk-tuned quantile → two-tier safety/opportunity); the "separate what varies from what's universal" principle (three per-user models: intent→objective, devices→dynamics, tariff→prices into one universal core); the three self-learning loops (per-site calibration / fleet-cohort priors / human-gated meta-analyst); two non-negotiables (learning never touches safety; validate fleet-wide via shadow mode); LLM repositioned to elicitation/explanation/degradation/analyst; synthesis + 5-step migration path. Roadmap gained **Phase 5 — Self-learning**. Two deterministic-layer weaknesses noted for later: `spread_too_small` ignores absolute cheapness at low SoC; no next-day peak-demand lookahead (overnight_hold/Rule 20 is the rule-side answer to the same scenario).

**LP optimiser shadow prototype (`agent/optimizer.py`) — migration-path step 2.** Pure receding-horizon LP (scipy HiGHS; scipy present on the agent's `/usr/bin/python3`). Reads the *same* state + price + solar forecasts as the LLM and `compute_decision_context()` and returns a verdict in the *same* `{action, target_pct, mode, rule_fired}` shape for direct A/B.
- Per 30-min slot vars = charge/discharge/import/export/soc; minimise `Σ(price·gi − feedin·ge + wear·(c+d))·Δt` minus terminal value on stored energy; constraints = power balance, SoC recursion w/ efficiency, bounds.
- **Demand window = heavy import penalty 3–9 pm (peak months), not a fixed 85%/2:55pm rule** — the LP pre-charges exactly enough to cover the evening load from battery (derived from forecasts). Encodes the asymmetric loss; always feasible. `risk` knob scales solar down / load up (the elicited risk-aversion → quantile hook).
- Verdict map: grid-sourced charge slot 0 > 0.3 kW ⇒ charge (autonomous > 2.5 kW); solar-only/none ⇒ hold.
- **`agent/test_optimizer.py` — 9 assertions, all pass** (cheap-now charges; flat+full holds; negative→autonomous; peak-month pre-charges before window; sunny→hold; risk knob never reduces protective charging). `test_decision.py` still 60/60.
- Demo (`python3 agent/optimizer.py`) on the 10:00 June-1 cycle: optimiser said **hold/mpc_solar_only** (projected solar fills the battery) vs LLM+deterministic **charge to 85%** (`solar_sponge_floor`) — a real divergence; the optimiser trusts the solar forecast, the rules charge as cheap insurance. Exactly what robust-MPC (conservative solar quantile) resolves and what the three-way shadow exists to surface.
- **Shadow wiring (zero control-path risk):** `run_agent()` computes the optimiser verdict in a *separate* try/except (cannot affect the deterministic shadow or control); `log_decision()` writes `optimizer_verdict`, `optimizer_context`, `optimizer_action_match` (vs LLM), `optimizer_vs_deterministic` to `decisions.jsonl`. Guarded by `_HAVE_OPTIMIZER`. `energy_agent.py` compiles + imports clean, `_HAVE_OPTIMIZER = True`. From the next cron cycle every record carries a three-way A/B.

**Three-way divergence watch — tool + first findings (`agent/three_way_review.py`).** New analysis tool: reads `decisions.jsonl` and reports pairwise agreement (LLM↔rules, LLM↔optimiser, optimiser↔rules) + three-way consensus + a divergence list, highlighting cycles where the optimiser disagrees with *both* others. Live records use their logged `optimizer_verdict`; older records are **back-filled** by re-running `optimize_battery()`. **Important back-fill caveat:** the back-fill reconstructs solar from each record's `solar_this_hour_kwh`/`solar_next_hour_kwh` via `_synth_solar_from_record()` — a crude, likely *over*-stated proxy — so back-filled optimiser verdicts are biased toward "solar will cover it / hold". Live `optimizer_verdict` records (from the 11:00 cron onward) use the real solar forecast and are the trustworthy ones. `/morning` rewritten to a three-way analysis (was 2-way shadow) with the new field names and the (a) bug / (b) LLM-caution / (c) optimiser-trusts-forecast taxonomy. (Tooling note: an initial `dict | None` annotation crashed silently on the agent's Python 3.9 — fixed to `Optional[dict]`; numbers below are from the corrected run.)

First run — 77 cycles, 31 May 00:00 → 1 Jun 10:30, **all back-filled** (so read with the caveat above):
- three-way consensus 56%; **LLM↔deterministic 82%**; LLM↔optimiser 69%; optimiser↔rules 61%.
- **Headline: the back-filled optimiser chose `hold` on all 77 cycles — it never once charged.** It under-charges relative to both LLM and rules across the board. Two co-primary causes, both expected:
  1. **Synthetic-solar back-fill bias** — the reconstructed solar overstates generation, so the LP concludes solar covers the load and returns `mpc_solar_only`. This is a measurement artifact of back-fill, *not* the live optimiser; only live records settle it.
  2. **Horizon length** — Amber's forecast is ~6h, so a 10–11am cycle sees only to ~4pm. On a Solar Sponge day those 3–4pm prices are still cheap (6–9¢); the expensive evening demand-window prices (5–9pm) that justify pre-charging are *beyond the horizon*, so the LP sees flat-cheap with no arbitrage incentive and holds. The rule layer compensates with structural knowledge the LP lacks (Solar Sponge always < evening; 85% by 2:55pm).
- Risk-knob sensitivity (solar quantile): risk=0.6 flipped only ~3 good-solar afternoon cycles to charge; it did **not** materially change the picture. The solar quantile is a *second-order* fix.
- Honest read of who was right on the divergences: mixed and not yet conclusive from back-fill. Notably at 20:30–21:00 on 31 May the optimiser held at 14–15¢ where the LLM *and* rules charged — and that charge is exactly the overnight-at-high-price mistake that motivated Rule 20 the same day, so the optimiser's hold looks *correct* there. Conversely on the midday Solar Sponge cycles the rules' "charge cheap as insurance" is the safer call on a peak day. This is precisely the (b)-vs-(c) tension the watch exists to adjudicate — needs live data.
- LLM↔deterministic at 82% is consistent with prior reviews (the rule layer tracks the LLM well); the optimiser is the outlier and is **not yet trustworthy from back-fill alone**.
- **Two fixes before the optimiser means anything: (1) longer price horizon** — synthesise evening/overnight prices from the historical p25/p75-by-time-of-day model, OR apply the 3–9pm import penalty to the whole block regardless of forecast length; **(2) collect live `optimizer_verdict` records** (real solar) and re-run the watch on those only, ignoring back-fill. Both added to todo.

**LP horizon bug diagnosed.** The LP's `demand_penalty_c=1000` on grid import during 3–9pm is correctly applied to whichever slots in the Amber forecast fall within that window — and the 6h forecast does reach 15:00–16:00 on a morning cycle, so the penalty IS firing for those slots. The bug is more subtle: with good solar, the LP correctly sees "solar fills the battery → no grid charge needed → hold". With zero/poor solar and battery draining to 5% by 15:00, the LP should pre-charge but holds instead — likely because the load-coverage cost at the penalty rate for 1–2 demand-window slots is still smaller than the pre-charge cost in the LP's maths (since only 1–2 slots are in the demand window, not all 6). The fix (horizon extension to 24h) exposes all 4–6h of the demand window, making the penalty unavoidable and much more expensive than pre-charging. This is the confirmed priority-1 fix before any cutover. Horizon extension starts next.

**Migration plan — LP-authoritative (biased for sooner):**

| Date | Work |
|------|------|
| **Today (June 1)** | Watch 2:55pm demand window live — first validation of the whole system. LP shadow logs via cron. |
| **June 2** | Build synthetic horizon extension in `optimizer.py` (p25/p75-by-hour beyond Amber ~6h); add test; confirm LP charges on simulated cloudy peak morning |
| **June 3** | Monitor live `optimizer_verdict` records (real solar, not back-fill); run `three_way_review.py --live-only`; watch 2:55pm again |
| **June 4** | If 48h of live LP data is clean on peak-day charging → flip `OPTIMIZER_AUTHORITATIVE = True`. LLM still runs but only generates narrative; deterministic layer becomes safety override |
| **June 7–14** | Slim system prompt (remove LP-replicated arithmetic); implement selective LLM — only called on divergent cycles or emergency rules |

**Architecture of the cutover:** `OPTIMIZER_AUTHORITATIVE` flag at the top of `energy_agent.py`. When True: LP verdict drives control commands; deterministic layer overrides if it fires higher urgency (`peak_deadline_autonomous`, `demand_window_active`); LLM runs only to produce the narrative summary and its `set_*` tool calls are no-op'd. Kill-switch: flip flag back to False. No git reset needed.

**hours_to_cheap_end / demand window question clarified.** Discussed why the agent reported "6+ hours to cheap window end" on a peak month day. Answer: the `hours_to_cheap_end` figure is a *price-shape* metric that scans for when prices exit the cheap band — it's not the operative deadline on peak days. In peak months, `hours_to_deadline = min(hours_to_2:55pm, hours_to_cheap_end)` ([energy_agent.py:1161](agent/energy_agent.py:1161)), so the 2:55pm constraint is already capping the decision arithmetic correctly. The 6h figure is also partly a flat-day fallback artifact (MIN_DAILY_SWING guard) and is misleading-but-harmless — the decision tree uses `hours_to_2_55` directly on peak branches. Presentation fix deferred as low priority.

---

## 2026-05-31 (session 4 — deferral_limit false-positive fix + morning analysis field names)

**EV Eco+/Fast progression + solar zero-detection threshold fixes:**

**FIT price integration + EV Case 6 (negative FIT solar dump):**

Added `sensor.1a_wigram_road_glebe_feed_in_price` and `_descriptor` to state read and JSONL log (`fit_price_c`). New EV Case 6: when FIT < 0¢ AND battery SoC ≥ 85% AND EV < 100%, switch EV to **Eco+** (not Fast) — Eco+ absorbs surplus solar that would otherwise export at negative price, without pulling from grid. Fast was initially used but corrected: the goal is to avoid paying to export, not to buy grid power. Battery threshold 85% ensures this only activates when battery is genuinely near full. 55 unit tests pass.

**`battery_autonomous_revert_target_reached` automation bug fixed:**

Automation was reverting reserve to 5% prematurely because it used Tessie OR gateway sensor. When reserve=100%, the gateway immediately reads 100% (it floors at reserve level), matching the grid_target_pct and triggering the revert within 30s — long before the battery actually reached the target. Fixed: changed trigger to Tessie sensor only. The 2-minute Tessie poll lag is acceptable; the gateway was unusable for this check.

**New HA input_number sliders added** (require HA restart to activate):
- `input_number.ev_ultra_cheap_threshold_c` (default 6¢)
- `input_number.ev_eco_gap_c` (default 1.5¢)
- `input_number.battery_charge_price_threshold_c` (default 12¢)
- `input_number.battery_max_insurance_floor_pct` (default 70%)

**Live observation 2026-05-31 14:38–15:00:**
Battery was at 73% with price at 6¢ and cheap window until 16:30. Historical model correctly targeted 95% (price at p25 floor). Agent set reserve=95% then escalated to autonomous+reserve=100% at 14:40 when automation prematurely reverted. After automation fix, battery charged cleanly to ~82% by 15:00 with agent maintaining reserve. FIT was -1¢ throughout — EV correctly held on Eco+ (already at 80% target; Case 6 didn't fire until battery ≥ 85%).

**Historical price model for grid target (non-peak, `HISTORICAL_PRICE_MODEL = True`):**

Replaces the static `cost_target` logic with a rolling price-percentile model that makes the grid charge target continuous and self-calibrating:

- `load_price_history(7)` reads the last 7 days of `price_c` from `decisions.jsonl` each cycle
- `_price_stats()` computes `p25` (cheap anchor) and `p75` (reference anchor) from those prices
- `price_position = (P_now − p25) / (p75 − p25)` — where current price sits in recent history (0=cheapest, 1=normal/expensive)
- **Solar trust**: `solar_trusted = forecast × confidence × price_position` — when prices are cheap (position→0), discount solar forecast (cost of over-charging is low, be aggressive from grid). At normal prices (position→1), full trust.
- **Insurance floor**: `floor = max_floor × (1 − price_position)` — maintain a minimum SoC proportional to how cheap prices are, guarding against the cheap window closing early.
- `cost_target = max(solar_adjusted_target, insurance_floor)`
- Falls back to legacy logic when: `HISTORICAL_PRICE_MODEL = False`, insufficient history (< 48 records), price history is flat (swing < 2¢), or peak month (demand deadline overrides anyway).
- `cost_target_method` field in JSONL (`historical` vs `legacy`) tracks which path fired.
- Rollback: set `HISTORICAL_PRICE_MODEL = False` at top of `energy_agent.py`.
- New HA slider: `input_number.battery_max_insurance_floor_pct` (default 70%).
- 52 unit tests pass.

**Solar-unreliable autonomous escalation (non-peak):** When `solar_unreliable=True`, self_consumption at 1.7 kW cannot be supplemented by uncertain solar, so the autonomous escalation threshold is tightened from `fill_slow >= deadline - 0.5h` to `fill_slow >= deadline - 1.5h`. If there's less than 1.5h of buffer at the slow rate and solar is unreliable, the agent escalates to autonomous (~5 kW) to fill the battery from grid while prices are still cheap rather than risking arriving at the evening spike short. New rule_fired: `nonpeak_solar_unreliable_autonomous`. System prompt updated with matching guidance. 47 unit tests pass.

**EV charging refinement (Cases 4 & 5):** When in a cheap window but a genuinely cheaper price is still coming (forward_min > 1.5¢ below current) AND EV is above the minimum SoC (no urgency), use Eco+ now and save Fast for the actual cheapest moment. New rule_fired values: `ev_case4_cheaper_upcoming` / `ev_case5_cheaper_upcoming`. When forward_min ≥ current − 1.5¢ (this IS the cheapest window), use Fast as before. System prompt updated to document the Eco+/Fast progression. This covers the user's scenario: EV at 50%, 8¢ now but 6¢ in 2h → Eco+ now → Fast when 6¢ arrives.

**Solar zero-detection threshold raised from 8am → 9am:** Two separate paths were firing too early on flat-roof panels in Sydney in late May/June:
1. `_detect_zero_solar`: guard raised from `now_h < 8` to `now_h < SOLAR_START_HOUR (9)`, and the recent-records check now only counts records from ts_hour ≥ 9. Prevents 8:30am false positives when the 8:00 record shows solar=0 (expected — sun angle too low on a flat roof).
2. `solar_unreliable` (accuracy-based): added `and now_h >= SOLAR_START_HOUR` gate. Before 9am, Solcast may forecast 1.2kWh but actual is 0 — this is expected dawn behaviour, not a forecast failure. Prevents the agent from declaring "unreliable forecast, treating as zero-solar day" all morning on sunny days. System prompt updated to say "after 9am" instead of "after 8am".

39 unit tests, all pass.

---

**Two shadow-layer data-quality bugs fixed:**

**Bug 1 — morning analysis wrong field names (cosmetic):** The `/morning` analysis script was reading `action`, `soc_pct`, `price_now_c` — fields that don't exist in the JSONL schema. Actual fields are `actions` (list), `soc`, `price_c`. This made every record look like it had null LLM decisions. The underlying data was fine; only the analysis display was wrong. Fixed: added a field-name reference table to `.claude/commands/morning.md`.

**Bug 2 — deferral_limit false-positive on overnight hold (logic fix):** The deterministic layer was firing `deferral_limit/charge` at 22:30–23:30 and 07:00–07:30 when the LLM correctly held, waiting for a 6¢ Solar Sponge window 7+ hours away. Root cause: `deferral_limit` only checked `deferral_detected` (3 holds with flat price) but not whether a genuinely cheaper window was incoming. Overnight, prices are flat at 13¢ but the forecast clearly shows 6¢ from 10am — `forward_min = 6¢`, well below current 13¢.

Fix: compute `forward_min = min(price_forecast)` (cheapest price in the full horizon, not just 6h), and gate `deferral_limit` on `forward_min >= price - 2.0`. If there's meaningfully cheaper power coming (>2¢ below current), holding is correct and deferral_limit is suppressed. Existing `nonpeak_deferral` test (flat 16¢ forecast, no cheaper window) still passes — deferral_limit still fires correctly when it's a genuine stuck-wait. New test `test_overnight_hold_for_cheap_window` added and passes. 30 unit tests total.

**`forward_min_c` added to JSONL `computed_context`** so future `/morning` reviews can see it in divergence records.

---

## 2026-05-30 (session 3 — deferral_limit ordering bug fixed)

**Shadow-layer `/morning` analysis — deterministic layer bug found and fixed:**

34 shadow records accumulated (15:30 yesterday → 07:30 today). Agreement rate was 41% (14/34), but all 20 divergences were the same single bug: `deferral_limit` branch in `compute_decision_context()` was ordered **before** the `target_met` (cost_target ≤ soc) check. Result: any time 3+ consecutive holds occurred with SoC already above the cost floor (normal overnight behaviour), the deterministic layer fired `charge/deferral_limit` erroneously. The LLM correctly held all 20 times.

Fix: swapped the two branches so `target_met` is checked first. 28 unit tests pass. Trivial reorder, no logic change — the deferral_limit rule is still correct; it now only fires when a genuine charge gap exists.

Current state: SoC=24% at 07:30, holding for Solar Sponge (forecast: 7¢ at 09:30, 6¢ from 11:30). LLM is correct to hold.

June 1 demand window activates Monday — backtest covered it, but not yet run live.

---

## 2026-05-29 (session 2 — forecast fix, backtest harness, SoC-sensor bug, shadow mode, cheap-end rewrite)

**`/morning` review extended — shadow-layer analysis section added (`.claude/commands/morning.md`):**

The morning summary is now 4 parts (was 3); the new part compares the LLM's actual decisions against the deterministic shadow layer from `decisions.jsonl` and reports: agreement rate (`shadow_action_match`/`shadow_mode_match`), each divergence with a right/wrong read, and a recommendation on advancing the re-architecture (Phase 4 collect → Phase 5 cutover with kill-switch → Phase 6 slim prompt). Handles the "too few records yet" case. This makes divergence review a standing daily habit through the June peak week.


**Price forecast bug fixed — mixed interval granularity:**

`get_price_forecast()` was not empty (todo was stale) but badly malformed. The Amber sensor returns *mixed* interval sizes: the first ~10 entries are 5-minute intervals, the rest are 30-minute. The code did `forecasts[:24]` assuming uniform 30-min ("12h"), so it actually saw ~7.5h, and the agent's `price_forecast_6h` (`[:12]`) was really ~1.8h. Worse, the `hours_to_cheap_end` sustained-rise scan compared intervals that were sometimes 5 min apart, sometimes 30 — making the deadline maths meaningless. Fix: resample all sub-intervals into uniform 30-min buckets (average price per bucket), so index × 0.5h is an accurate "hours from now" and the existing prose-based scan logic becomes valid. Added a fail-loud stderr warning when the forecast is empty.

**Peak-month backtest harness added (`agent/backtest.py`):**

Feeds the real agent (real system prompt + model) synthetic peak-month scenarios, stubbing every data-read and intercepting every write — nothing touches Tessie/HA. Purpose: validate the 2:55pm/85% demand-window logic before June 1, when it first matters live. 5 scenarios: cloudy-10am, cloudy-1:30pm (tight), sunny-11am, deferral-trap, 2:55pm-boundary.

**Backtest results — 4/5 correct, 1 real bug found:**

The agent correctly held on the sunny day, charged self_consumption when on-time, escalated to autonomous when tight, and fired the deferral limit. The 2:55pm-boundary scenario exposed a genuine bug: the agent trusted `soc_gateway_pct` (85%, floor-clipped to the reserve level) instead of the true `soc_pct` (Tessie, 50%), declared the demand-window target "achieved," and dropped reserve to 5% — which on a real June day would enter the 3-9pm window at 50% and risk a Rule 2 grid-import violation.

**Re-architecture Phase 1 + 2 — deterministic decision layer (shadow only, not yet wired in):**

Began moving the arithmetic the system prompt asks the LLM to do in its head into a pure Python function. Rationale: every edge-case fix over the last fortnight (deferral limit, hours_to_cheap_end, zero-solar, Solar Sponge floor) is a deterministic computation bolted on as prose — the LLM is bad at exactly this (mental arithmetic, array scanning, counting) and there's no verification when it errs.

- `compute_decision_context(state, price_forecast, recent_records, now)` — pure function (no HTTP, no clock reads, no globals). Computes hours_to_cheap_end, kwh_needed/fill times (both cost-target and 85% demand), zero_solar_day, deferral_detected, effective cost target (grid_target vs time-based substitute), spread, and a recommended verdict `{action, target_pct, mode, rule_fired}` via an ordered decision tree (demand window → peak deadline → Solar Sponge floor → deferral → spread table).
- `get_recent_records(n)` — parsed-dict version of the recent decisions (the existing function returns a formatted string; the pure function needs structured data).
- `agent/test_decision.py` — 26 assert-based unit tests, no API calls, run in ms. Cover hours_to_cheap_end (sustained rise vs blip), deferral/zero-solar detectors, and the verdict for every backtest scenario incl. the SoC-gateway divergence.

NOTE — this is NOT in the control path yet. Plan: shadow mode through early June (log computed verdict alongside the LLM's actual decision, measure divergence), cut over only after the first peak-month week. One divergence already visible: on the deferral-trap scenario the deterministic layer picks self_consumption (3.18h fill fits the 3.42h window) where the LLM over-escalated to autonomous — the kind of finding shadow mode is meant to surface.

**Phase 4 finding (first real divergence) — `_hours_to_cheap_end` under-reports urgency on a gradual price ramp:**

Live at 15:30 the LLM was charging in autonomous; the deterministic layer recommended `nonpeak_deadline_selfcons` (charge to 90% but at self_consumption). They agreed on action+target, diverged on mode. Investigation (prompted by Simon noticing the cheap window was visibly closing in ~30 min, not 1.5h):

- Forecast: 13.4 (now) → 15.7 (16:00) → 17.0 (16:30) → 19.0 (17:00, sustained).
- `_hours_to_cheap_end` anchors to *current* price and only flags "cheap end" at the first sustained **+4¢** step. 15.7 is +2.3, 17.0 is +3.6 — both under +4 — so it only triggers at the 19¢ step = **1.5h**.
- But the window is effectively closing at 16:00–16:30 (price already +2–3.6¢, Amber cheap flag/descriptor flipping). Real remaining cheap time ≈ **30 min**.
- Consequence: with only ~30 min left, self_consumption (`fill_slow_h`=1.59h) cannot reach 90%; only autonomous (`fill_fast_h`=0.54h ≈32min) makes it. **The LLM's autonomous call was correct; the deterministic recommendation was wrong** — because its urgency metric over-stated remaining cheap time on a gradual ramp.

Root cause: `+4¢-relative-to-current-price` threshold. On a stepwise jump it's fine; on a smooth climb off a low base it never accumulates +4¢ between adjacent intervals until the very end.

Proposed fix (HOLD until a few more June examples confirm the pattern): redefine cheap-end as hours until price crosses an **absolute** cheapness band (or until the Amber `in_cheap_window`/descriptor clears), rather than a relative jump off the current price. Candidate: cheap-end = first interval where `cents_kwh > min(current_price, today_cheap_band) + N` measured against an absolute reference, or simply consume Amber's own descriptor transition (very_low/low → neutral+). Keep the relative-jump test as a secondary trigger for genuine step events. `agent/energy_agent.py:784`.

This is exactly the divergence shadow mode was built to surface — captured for the Phase 4 review.

**Fix implemented — `_hours_to_cheap_end` rewritten as a scale-free daily-shape model (Option 1):**

Replaced the +4¢-relative-to-current-price test with a range-normalised cheap band, so the metric tracks the *structural shape* of the day (morning bump → noon trough → evening peak) regardless of absolute price level:

- `p_min` = cheapest interval ahead; `p_peak` = max price in the **evening window (15:00–21:00)** (anchored there so the morning bump doesn't inflate the range; falls back to forward max if no evening intervals in horizon).
- `rng = p_peak − p_min`; cheap band `T = p_min + α·rng`, `α = 0.30` (`CHEAP_BAND_ALPHA`).
- `hours_to_cheap_end` = hours to the right edge of the cheap region ahead (first sustained interval where price > T). Returns 0.0 if already above the band, 6.0 if it never ends in the horizon.
- **Flat-day guard:** if `rng < 5¢` (`MIN_DAILY_SWING`), return 6.0 — a flat day has no real trough/ramp, so 1¢ jitter must not register as a closing window.

Backtested against every forecast in `decisions.jsonl`: on flat May days (15–17¢, ~1–2¢ swing) the new metric returns 6.0 just like the old one (no false urgency); on the morning of a big-swing day (08:30, 41→16¢ descending) it returns 6.0 (trough still ahead — correct); and on the 15:30 evening ramp it returns **0.5h**, matching the eye and fixing the bug. Re-running the live 15:30 verdict now yields `charge/90%/autonomous` — **agreeing with the LLM**; the earlier false divergence is resolved. Added unit tests for the gradual-ramp and sub-5¢-jitter cases (28 pass). α/swing values are first-pass; revisit once June peak-month forecasts (larger swings) accumulate. `agent/energy_agent.py:784`.

**Re-architecture Phase 3 — shadow mode wired in (logs both decisions):**

The deterministic layer is now computed every live cycle and both decisions are logged side-by-side for divergence measurement. This is the data-collection step before any cutover.

- `run_agent()` precomputes `compute_decision_context()` before the LLM loop and injects `_format_decision_context()` output into the initial message as a "REFERENCE ONLY (you are still the decision-maker)" block — the LLM still decides; the helper is advisory.
- `_format_decision_context(ctx)` renders all derived figures (hours_to_cheap_end, kwh_needed, fill times, spread, flags) plus `>>> RECOMMENDED:` verdict.
- `log_decision()` now records `computed_verdict`, `computed_context`, `shadow_action_match` (did the LLM charge/not-charge match the recommendation), and `shadow_mode_match` (self_consumption vs autonomous) into `decisions.jsonl`.
- Dry-run verified: the shadow block renders and the agent references it in its reasoning ("aligns with the deterministic helper recommendation of 'target_met, hold'"). Cycle completes clean (exit 0).

NOT in the control path — the LLM's decision is still authoritative. Plan: collect divergence data through the first peak-month week (June), then decide on cutover. This directly enables the "use next week's data to validate" goal.

**Fix — SoC-sensor guidance added to system prompt:**

The system prompt had no battery-sensor section and never told the agent which SoC reading to trust (Rule 6 in energy_rules.md knew, but the prompt didn't). Added a CRITICAL block: always use `soc_pct` (Tessie true SoC); `soc_gateway_pct` is floor-clipped at the reserve level and lies upward whenever reserve > true SoC; never judge target-met off the gateway. Re-ran the boundary scenario: agent now correctly identifies true SoC=50%, ignores the gateway, and keeps charging instead of dropping reserve.



**Observed failure mode — agent deferring indefinitely on a forecast that never arrives:**

Battery sat at 5% SoC from midnight through ~10am while the agent held each cycle waiting for a 12–13¢ cheap window that never materialised (price stayed at 16–19¢ all morning). Each 30-min cycle the Amber forecast showed a cheap window "arriving in 2–3 hours" — agent rationally held each time, but the window kept moving. Agent is stateless between cycles so had no way to detect the pattern.

Root cause: no memory of prior deferral decisions + no time-based override when the forecast is repeatedly wrong.

**Rule 14 added — Solar Sponge minimum floor:**

EA116's Solar Sponge (10am–3pm) is structurally cheaper than evening prices on every day — not a forecast, a tariff fact. The agent was treating it as a variable "spread check" decision and deferring indefinitely waiting for an even cheaper window. Rule: during 10am–1pm, if SoC < 50%, always charge to at least 50% regardless of spread. This is a floor only — demand window or grid charge targets above 50% override it. Self-consumption is sufficient (no autonomous needed). Added to system prompt and energy_rules.md as Rule 14.

**Fix — short-term memory + escalation rules (Rule 13):**

1. **Short-term memory**: `get_recent_decisions(n=3)` reads the last 3 records from `decisions.jsonl` and injects them as a "Recent decisions" context block in the initial message of each cycle. The agent sees: `[09:00] hold, [09:30] hold, [10:00] hold` and can recognise the deferral pattern.

2. **Deferral limit**: if 2+ consecutive holds waiting for the same cheap window AND current price is within 2¢ of then — forecast is wrong, charge now at current price. Added to system prompt and Rule 13 in energy_rules.md.

3. **Time-based escalation (Rule 13)**:
   - Peak months: every cycle from 9am, calculate `kWh_needed / rate` vs `hours_to_2:55pm`. If self_consumption can't make the deadline, switch to autonomous immediately. Price irrelevant — demand charge is $100/month.
   - Non-peak months: if `hours_to_fill_slow ≥ hours_to_spike − 0.5h`, start self_consumption regardless of spread. After noon with battery < 30% and flat prices for 2+ cycles: charge now.
   - Quick-check heuristics added: past 12:30pm + <40% SoC + peak month = autonomous immediately.

4. **Task each cycle** updated: step 1 is now "review recent decisions before anything else."

Rule 13 added to energy_rules.md. System prompt updated to match.

**Zero-solar override added to system prompt:**

Agent kept citing "solar will arrive from 11am" as a reason to defer charging even when solar had been 0 kW for 2+ consecutive cycles during daylight. Root cause: the Solcast/Open-Meteo forecasts predicted solar clearing — the agent treated model prediction as more credible than actual evidence. Fix: explicit CRITICAL rule — if `solar=0.0kW` appears in 2+ of the last 3 cycles during daylight hours (after 8am), that is a zero-solar day. Do NOT cite weather model radiation forecasts as a reason to hold. Evidence beats predictions. The `solar_current_kw` field added to recent-decisions context format so agent can see the pattern.

**EV Case 5 bug fix:**

Agent left Zappi on Eco+ when battery was at 29% below reserve=70% and EV was at 34%. Agent said "Eco+ correctly" because it applied Solar Sponge caution ("don't use Fast during Solar Sponge if discharging") — but battery was BELOW reserve, so it cannot discharge. Case 5 (battery below reserve, charging from grid) explicitly allows Fast. Fix: rewrote EV section of system prompt with explicit numbered priority list (Cases 2/3/4/5) and an explicit note "Do NOT apply Solar Sponge caution to prevent Fast when Case 5 is active." CONTEXT.md EV policy updated to match.

**Battery forecast card redesign:**

Rebuilt the HA markdown card to show the decision context clearly:
- Goal by deadline (85% peak / 80% non-peak)
- Projected SoC with solar-accuracy scaling (0%/50%/100% of Solcast remaining)
- Grid charging contribution included in projection (was previously missing — caused "10% projected" when battery was actively charging)
- Gap in % and kWh with plain-English "Why" reasons
- "To close the gap" section with self-consumption vs autonomous ETAs, and flag when the deadline is too tight

**JSONL record additions and daily accuracy tracking:**

Three new fields added to every `decisions.jsonl` record:
- `goal_3pm_soc`: target SoC at 3pm (85% peak, 80% non-peak)
- `projected_3pm_soc`: solar-accuracy-adjusted projected SoC at 3pm (mirrors card logic)
- `price_forecast_6h_times`: ISO timestamps for each price forecast slot (joins with InfluxDB actuals later)

New function `_maybe_write_daily_accuracy(now)`: fires once after 3pm, checks if today's accuracy record exists, finds the actual 3pm SoC from the first post-3pm cycle, compares against morning projections (6am, 8am, 10am, 12pm cycle timestamps) and writes a `record_type: "daily_accuracy"` record with `projections`, `actual_3pm_soc`, and signed `projection_errors`. Foundation for the InfluxDB accuracy dashboard.

**Price risk asymmetry section added to system prompt:**

Added explicit framing: the spread table is symmetric but evening prices are not. If you charge at 15¢ and evening is 18¢, you overpaid 3¢. If you wait and evening hits 30¢+, you underpaid by 15¢ on what you missed. Spot prices have a fat right tail. Solar Sponge charging is insurance against that tail, not just an arbitrage calculation. Practical implication: the spread floor itself should always be met during Solar Sponge regardless of spread — which leads directly to Rule 14.

**Adaptive deadline calculation (hours_to_cheap_end) — replaces hours_to_spike:**

The old deadline logic used `hours_to_spike` = first forecast price > 30¢. This was broken in two ways:
1. On mild-spike days (prices going 15¢ → 19¢) it found nothing and defaulted to 6h, giving false "plenty of time" readings
2. In non-peak months where 3pm doesn't matter, 3pm was used as the deadline — wrong if cheap prices actually run until 4pm+

Replaced with `hours_to_cheap_end`: scan forward through 30-min forecast intervals for the first *sustained* price rise — two consecutive intervals both ≥ current_price + 4¢. That's the real deadline regardless of absolute price level.

- Non-peak months: `hours_to_cheap_end` is the deadline
- Peak months: `min(hours_to_2:55pm, hours_to_cheap_end)` — demand window remains the hard constraint
- No sustained rise found: use 6h (end of forecast)

Example: price 15¢ all day until 4pm then 20¢. Old logic: 30¢ not reached → 6h deadline (too loose). New logic: +4¢ sustained rise found at 4pm → hours_to_cheap_end = 5.5h from 10:30am. Self-consumption decision made correctly against real deadline.

Updated: system prompt SYSTEM_PROMPT (deadline-aware charging step 4, non-peak section), energy_rules.md (Rule 1 deadline table, Rule 13 non-peak section).

**HA battery forecast card updated for dynamic deadline:**

Card now scans the Amber forecast attribute directly in Jinja2 to compute `hours_to_cheap_end` (same algorithm as agent): first sustained +4¢ rise over two consecutive intervals. Non-peak months show "Goal by 4pm" (or actual dynamic time); peak months show "Goal by 3pm". All projection, gap, and "to close" calculations use `hours_to_deadline` rather than a hardcoded `hours_to_3pm`. Footer "kWh to 3pm" also updated to the real deadline time.

---

## 2026-05-28

**Agent bug fix — grid charging trigger mechanism:**

Observed at 14:00: agent said "self_consumption at reserve 5% is correct — grid will cover 1.3 kWh" but no grid draw occurred. Root cause: agent misunderstood the control mechanism. Grid charging in self_consumption mode ONLY happens when `backup_reserve_percent > current_soc`. Reserve at 5% with battery at 62% gives Tesla no reason to touch the grid — battery charges from solar surplus only.

Correct action would have been `set_reserve(80%)` to trigger the grid draw. System prompt updated with a prominent CRITICAL note and a lookup table making the trigger explicit. Tool description for `set_powerwall_mode` also corrected. Failure mode documented in energy_rules.md with the 14:00 observation as the example.

**Agent model switched to `claude-sonnet-4-6` with prompt caching:**

- Model: `claude-opus-4-5` → `claude-sonnet-4-6`
- Prompt caching added: system prompt and tool definitions marked with `cache_control: ephemeral` — within-cycle turns 2+ served from cache at 10% of input token cost
- Cost formula corrected: Anthropic returns `input_tokens` as non-cached only; cache tokens are billed separately
- Pricing constants updated: $3/$15 per 1M tokens (input/output) for Sonnet; verify at console.anthropic.com
- Result: ~25¢/cycle → ~4¢/cycle, ~$12/day → ~$2/day (6x reduction)
- `sensor.agent_daily_cost` in HA now reflects Sonnet pricing

**Claude API cost logging added (`decisions.jsonl`):**

- `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `est_cost_usd` recorded per cycle
- `sensor.agent_daily_cost` pushed to HA each cycle with running daily total and token breakdown in attributes

**Open-Meteo weather forecast added to agent (`get_weather_forecast()` tool):**

Adds hourly cloud cover, solar radiation (W/m²), and rain probability from Open-Meteo alongside Solcast. Free, no API key needed, covers Glebe precisely (lat=-33.88, lon=151.19). Returns solar-relevant hours (6am–7pm) for today and tomorrow, plus a `tomorrow_solar_outlook` summary (good/poor/overcast) derived from average radiation during the 8am–3pm core window.

Two use cases:
1. **Overnight pre-charging** (peak months): agent now sees tomorrow's solar quality before deciding overnight charging level — if tomorrow is overcast, pre-charge tonight rather than relying on solar that won't arrive.
2. **Daytime cross-check**: distinguishes "temporary cloud" (Solcast unreliable but radiation > 250 W/m² — wait 30 min) from "all-day overcast" (radiation also < 150 W/m² — act immediately).

First run showed agent correctly using it: cloud cover 100% but radiation improving (201→430 W/m²) → correctly diagnosed as temporary/passing cloud, not all-day overcast. Tomorrow outlook: poor (293 W/m² avg) — would trigger overnight pre-charging in June peak months.

New JSONL fields: `tomorrow_solar_outlook`, `tomorrow_avg_radiation`.
Rule 12 added to energy_rules.md.

---

**Structured JSON decision log added (`agent/decisions.jsonl`):**

`log_decision()` now writes a newline-delimited JSON record alongside the existing plain-text log on every cycle. Each record captures the full context the agent had at decision time:
- Battery: SoC, reserve before/after, mode before/after, grid target
- Grid: price, cheap window flag, 6-hour price forecast array
- Solar: current kW, forecast accuracy, remaining/this-hour/next-hour kWh
- Time context: is_peak_month, in_demand_window, in_solar_sponge
- EV: plugged, SoC, zappi mode before/after
- Decision: actions list (null fields for holds), summary text

`_cycle_context` dict populated by `get_current_state()` and `get_price_forecast()` as they run; read by `log_decision()` to build the record without re-querying HA. Reset at start of each `run_agent()` call.

First record confirmed valid at 09:12 AEST — hold decision, battery 33%, waiting for 17¢ Solar Sponge window vs 23¢ current price.

This is the foundation for Stage 2 (outcome annotation from InfluxDB) and Stage 3 (analyst agent).

---

## 2026-05-27

**Crontab fixes:**
- Fixed broken crontab path (old: `/Users/simonmonk/homeassistant/agent/energy_agent.py`, after monorepo move)
- Fixed unquoted path with spaces causing cron to fail silently — agent was missing cycles from ~9am until fixed (~11:30am)
- Crontab now correctly: `ANTHROPIC_API_KEY="..." /usr/bin/python3 "/Users/simonmonk/Simon Projects/Home Energy Console/agent/energy_agent.py"`

**Agent system prompt improvements (cloudy/flat-price day observations):**

Three gaps identified and patched from watching the agent on a cloudy day (solar forecast unreliable, ~20¢ all day, 62¢ spike at 7pm):

1. **Forecast accuracy → discard grid_target_pct**: When `forecast_accuracy` is `poor` or `unreliable`, the `battery_grid_charge_target` sensor is Solcast-derived and optimistically low. Agent now ignores it and substitutes a time-based target (85% before noon, 70% midday, 50% after 2pm).

2. **Flat-then-spike rule**: If min(forecast prices before spike) ≥ current price − 3¢, treat current price as the charge window — no cheaper window is coming, don't hold.

3. **Deadline-aware charging with adaptive escalation**: Agent now calculates `kWh_needed / charge_rate = hours_to_fill` and compares against `hours_to_spike` every cycle. Starts `self_consumption` when deadline demands it, escalates to `autonomous` if falling behind. Formula: must start self_consumption if `hours_to_spike ≤ hours_to_fill_slow + 1.5h`; escalate to autonomous if `hours_to_spike ≤ hours_to_fill_slow + 0.5h`.

**Observed agent decision at 10:44:** Correctly identified 40¢ spread (23¢ now vs 62¢ at 7pm), poor solar, set reserve to 80%, started self_consumption charging, noted it would reassess if falling behind. First correct application of new rules.

**`/morning` command updated** to include standing session instructions: update `energy_rules.md` and `energy_log.md` as changes happen, not just at session end.

**`energy_rules.md` updated** to reflect all three new agent rules above.

---

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

## 2026-06-23 (session 14 — morning rule fixes + Phase 2.5-A/B + Phase 7)

**Session start context**: Mac desktop session, 13 days since last session (last session 2026-06-10).

**Morning rule fixes (commits 773e3ce, a7d01bb, 5f64f0a, 33a5f89):**

- `peak_solar_cover_survival`: charges now when battery can't survive overnight to Solar Sponge AND sponge is either >3h away OR price gap <5¢. Addresses Jun 23 case where battery drained to 8% at 7am and emergency-charged at 42¢.
- `peak_survival_wait_for_sponge`: holds when battery can barely survive AND sponge ≤3h away AND sponge is ≥5¢ cheaper. Addresses 7:30am notification charging at 20¢ when 11¢ sponge was 2.5h away.
- `battery_low_soc_emergency_charge` automation: added 20¢ absolute price ceiling + hardcoded 85% reserve target in peak months before 3pm (bypasses `sensor.battery_grid_charge_target` which was returning 42% — suspected template cache issue).
- Demand window warning automations: added `for: "0:01:00"` to both triggers to prevent sensor glitch false positives (Jun 14 incident: "Battery at 0% during demand window. Grid importing 0W" — contradictory, clearly a sensor momentary drop).

**Phase 2.5-A — charge rate model (commit 403b48f):**

Built `agent/model_params.json` from 17 days of `energy_log.db` observations (179 self_consumption charging pairs across SoC buckets 0–90). Key findings:
- Peak rate 1.66 kW at 60% SoC (near rated self_consumption ~1.7 kW)
- Significant taper above 80%: 0.876 kW at 80%, 0.625 kW at 90%
- Below-rated at 40% (1.30 kW) — possibly firmware throttling at low SoC
- No autonomous data yet (Powerwall hasn't fast-charged during the 17-day window)

`_avg_charge_rate_kw(soc_from, soc_to, mode)` computes weighted-average rate by summing fill time across 10%-point SoC buckets. `fill_slow_85`, `fill_fast_85`, `fill_slow`, `fill_fast` now use model rates. Falls back to SLOW_KW=1.7/FAST_KW=5.0 for missing or low-sample (n<5) buckets.

Impact: fill time estimates are now accurate above 80% SoC. Previously underestimated time to charge from 75% to 85% (3.24 kW assumed → actual tapers to 0.876 kW). This is where the `fill_slow_85 >= hours_to_deadline` deadine check was most wrong.

**LP solar_unreliable fix (commit 403b48f):**

`optimizer.py`: zeros solar series when `state['solar_unreliable']=True`. Previously the LP always saw the raw Solcast forecast — on cloudy mornings it planned `mpc_solar_only` (hold, rely on solar) while the deterministic layer correctly charged from grid. This was the source of ALL 19 LP divergences in the previous 2-day analysis window.

With fix: LP now fires `mpc_hold` instead of `mpc_solar_only` on cloudy mornings (it still defers to cheap-grid slots rather than charging at the exact current slot, which is a different philosophical approach from the deterministic layer). The divergence label changes from a wrong positive claim ("solar will handle it") to an honest deferral ("no solar, charging later").

`run_agent()`: injects `solar_unreliable` flag from `_cycle_context['decision_context']` into `_opt_state` before calling `optimize_battery`.

**Phase 7 — selective narrative (commit 403b48f):**

`_is_interesting_cycle()`: skips LLM when:
- No action was taken (no set_reserve / set_mode calls)
- Rule is in `_ROUTINE_HOLD_RULES` (overnight_hold_wait_for_sponge, peak_target_met, peak_on_track, peak_solar_will_cover, demand_window_active, target_met, nonpeak_on_track)
- Rule hasn't changed since last cycle
- Demand guard didn't fire

LLM runs on: any action taken, unusual rule, rule change, demand_guard_fired, or dry_run.

`_build_auto_summary()`: generates one-line `[auto] rule | SoC% | price¢ | solarKW | hold` entry directly. JSONL written by `log_decision()` call from Python — no Anthropic API call.

Estimated API cost reduction: ~60-70% of cycles are routine holds overnight (14 cycles) + during demand window (12 cycles). These all now skip LLM. Remaining ~20% of cycles (Solar Sponge charges, peak deadline decisions, rule changes) still get LLM narrative.

**Tests**: 56 passed (test_decision.py) + 12 passed (test_optimizer.py). No regressions.

**Pending**: HA automations from this session still need manual reload. Pi will pull commit on next cron cycle (max 30 min).

## 2026-06-23 (session 14 continued — EV notification fixes, Supabase RLS, architecture discussion)

**EV notification fixes (commits d558cad, 1bf90eb, dd5c59d):**

Three iterations to get the EV notification right:
1. First fix: `_build_auto_summary()` changed from returning a string to a `(battery_summary, ev_summary)` tuple; `battery SoC` label corrected to `battery {soc}%` to avoid ambiguity.
2. Second fix: EV notification suppression when EV not plugged in — but realised the EV notification should always fire showing the Polestar SoC regardless.
3. Final fix: `ev_soc_pct` now always read from `sensor.polestar_7853_battery_charge_level` (was conditional on `ev_plugged`); EV notification always shows `[auto] EV 31% (not plugged in) | mode n/a | hold`.

**Architecture discussion:**
Discussed the refinement path for the next 6 months of winter operation:
- Phase 2.5-B solar corrector is the highest-value near-term item (OLS regression, Solcast vs SolarEdge actuals)
- LP to control path is the medium-term goal once solar corrector calibrated
- Analyst agent (weekly review of decisions.jsonl + daily_energy.jsonl) is the long-term self-improvement loop
- Winter will stress-test: overnight survival thresholds, sponge price threshold (10¢), peak_survival_wait_for_sponge 3h/5¢ thresholds, fill time accuracy above 80% SoC

**Tessie discussion:**
Tessie (~A$10/month) is a proxy for Tesla Fleet API. Replacing it requires: register at developer.tesla.com, one-time OAuth browser flow for refresh_token, ~20 lines of token-refresh logic in agent. Worth doing only if savings analysis from June data doesn't justify the cost. Decision deferred to after June data reviewed.

**todo.md rewrite:**
Full restructure — pruned ~40 stale completed items, reorganised into Immediate / Architecture roadmap / Tune from winter data / Infrastructure / Product sections. 18 active items, clearly prioritised.

**Supabase RLS fix (Sol project):**
Received security advisory email: `public.profiles` and `public.conversations` tables had RLS disabled, exposing them to unauthenticated access via PostgREST. Fixed:
- `public.profiles`: `ALTER TABLE ENABLE ROW LEVEL SECURITY` + SELECT/INSERT/UPDATE policies on `auth.uid() = id`
- `public.conversations`: same + DELETE policy, using `profile_id` column (not `user_id` — confirmed via `information_schema.columns` query first)
Both tables now locked down. Security Advisor warning should clear.

**energy_rules.md updates (this close-out):**
- Added Rule 24 (`peak_solar_cover_survival`) and Rule 25 (`peak_survival_wait_for_sponge`) — were missing entirely
- Updated fill time formula in Rule 13 to reference model-based rates (not flat 1.7 kW)
- Updated Phase 7 note in implementation section
- Added charge rate model note explaining `model_params.json` data

## 2026-06-24 (session 15 — Amber price update, 3 bug fixes)

**Amber price update (July 1):**
Reviewed Amber annual pricing update email. Key changes from July 1:
- Network component: +0.42¢/kWh (EA116 TOU pass-through — peak and off-peak are identical rates, so no behavioural TOU effect)
- Environmental: −0.23¢/kWh (carbon neutral offset removed)
- Market charges: +0.14¢/kWh
- **Net kWh change: +~0.22¢/kWh** — baked into Amber's real-time price, no agent changes needed
- Daily supply: 61.04¢ → 73.71¢ (+$3.80/month fixed)
- Metering: 44.47¢ → 47.25¢ (+$0.84/month fixed)
- **Demand charge: 42.34 → 43.43¢/kW/day (+2.6%)** — demand avoidance strategy unchanged
- Amber estimate: +$5.31/month for average 3,900 kWh/year residential customer
- "Time-of-use" reference in email is the Ausgrid network layer (not the wholesale spot). EA116 peak and off-peak network rates are identical, so no TOU behaviour from user's perspective.

**Bug observed — 23:00 autonomous charge at 19¢ with SoC=25%:**
Battery drained to 25% at 23:00 and agent fired `nonpeak_deadline_autonomous`, setting reserve=50% in autonomous mode (~5 kW) at 19¢. Should have waited for Solar Sponge (~11¢ at 10am). Root cause: two bugs intersecting.

**Three bugs fixed (commit 307e8c9):**

1. **`hours_to_2_55` deadline rollover (lines 1428-1432):** `max(DEMAND_DEADLINE − now_h, 0)` goes to 0 after 2:55pm. At 23:00 this was 0.0h, making the deadline escalation logic think we were *at* the deadline → autonomous urgency fired. Fix: if `now_h > DEMAND_DEADLINE`, add 24h (e.g. 23:00 → 15.9h until tomorrow 2:55pm).

2. **`overnight_hold` SoC boundary (line 1389):** condition was `soc > 25`, so at exactly 25% the hold didn't fire and fell through to the bugged deadline path. Fix: `soc >= 25`. At 25% SoC, 11h overnight drain at 0.5 kW still drains ~41% → hits the floor anyway, but Rule 7 handles the survival top-up correctly via self_consumption.

3. **`peak_deadline_autonomous` wrong-mode-on-urgency (line 1565-1572):** when `fill_slow_85 >= hours_to_2_55` (self_consumption too slow to reach 85% by deadline), the code checked `price <= forward_min` and used self_consumption if prices were flat. Self_consumption physically can't make the deadline regardless of price. Fixed: removed price branch — always go autonomous when in deadline urgency. This also fixed two pre-existing test failures. One stale test expectation updated (Phase 2.5-A model raised avg charge rate 45%→85% fill time from 3.18h to 3.97h, pushing it past the 3.42h deadline at 11:30 — autonomous is now correct).

**Tests:** 103/103 passed (up from 56 — new tests added during session 14 for Rules 24/25 plus the pre-existing failures now fixed).

**energy_rules.md updated:**
- Rule 20: `>= 25` boundary documented with rationale; deadline rollover bug explained
- Rule 13: `peak_deadline_autonomous` always-autonomous-when-tight note added

## 2026-07-03 (session 17 — Jul 3 "demand breach" investigation + monitor re-banding)

**Investigation (from a web session — no Pi access; worked entirely from user-supplied
downloads: `site_power` history CSV + June bill PDF):**

The 2026-07-03 dashboard flagged a demand-window **breach: 0.208 kW @ 18:00, min SoC 43%**.
Investigated whether this was a control failure. Conclusion: **it was not** — the agent,
reserve (5%), mode (self_consumption) and battery discharge all behaved correctly.

- Reproduced the billed figure from the raw `site_power` CSV: clock-aligned 18:00 half-hour
  = **0.198 kW net** (matches dashboard 0.208); worst *sliding* 30-min all day = 0.243 kW.
- Shape of the 18:00 half-hour: one ~6-min sustained ~1.3 kW grid draw (17:59–18:05) + a
  single ~30-s 2.0 kW blip at 18:15. Only 15% of the half-hour imported >0.5 kW.
- **Key user question — "won't the 2 kW spike cost ~$30?"** No. The June bill states demand
  is billed on the **highest 30-*minute average*** import in the month. June's actual demand
  charge: **Network – Peak Demand 2.69 kW × $11.5479/kW = $31.11** (that 2.69 kW was the Jun 2
  stranded-reserve breach — a *sustained* half-hour). A 30-s 2 kW blip adds only 0.034 kW to a
  30-min average → Jul 3's worst is ~$2.5–2.8/mo, not $30.
- **Conclusion on "can NDC ever be $0?"** No, and it shouldn't be chased. Demand = the month's
  single worst 30-min avg over ~360 window-intervals; the Powerwall's sub-second regulation lag
  means any dinner half-hour nets slightly positive. Jul 3 was already ≤0 in 11 of 12 intervals.
  Practical floor ≈ the spikiest dinner's regulation noise (~$1–3/mo). The money is in never
  repeating a Jun-2-class *sustained* breach (~$28 above floor), which the reserve guards now
  prevent.

**Change made — demand-window monitor re-banded by $ materiality (not near-perfection):**
- `agent/log_daily_energy.py`: replaced single `PASS_KW_THRESHOLD = 0.10` with three-way bands
  `classify_demand()` + `demand_cost_estimate()`. Rate from the June bill: `$11.5479/kW`.
  Bands: **pass <0.5 kW** (~<$6/mo, regulation floor) · **marginal 0.5–1.5** · **breach ≥1.5 kW**
  (~≥$17/mo, sustained/preventable). Record now carries `status` + `cost_est_dollars`; `passed`
  kept for back-compat.
- `agent/demand_window_summary.py`: imports the same bands (single source of truth), **re-scores
  all historical days from `peak_kw`** so old records adopt the new bands, and exposes
  `days_passed`/`days_marginal`/`days_failed` (breaches only) + `this_month_cost_est_dollars`.
- Dashboard markdown card updated to a 3-way icon (✅/🟡/⚠️) + est. $/mo column + month cost.
- Effect: Jul 3 (0.208 kW) now reads **✅ pass ~$2.40/mo** instead of ⚠️ breach; a 2.69 kW day
  still reads **⚠️ breach ~$31/mo**. 121 tests unchanged (109 decision + 12 optimizer, all pass).

**To deploy on Pi:** `git pull` → run `log_daily_energy.py` (rewrites JSONL with status/cost)
→ `demand_window_summary.py --post` (reposts sensor). Crons pick it up otherwise.

**Flagged (not actioned):** both cron scripts have a long-lived HA bearer token hardcoded as a
default (`demand_window_summary.py:42`, `log_daily_energy.py:55`) and committed to the repo —
worth rotating + moving to `.env`/`secrets.yaml`. Also still pending: `build_models.py` run on
Pi (Phase 2.5-B activation), unchanged from session 16.

**Dashboard card — bug then fix:** first card version referenced `d.status` and errored
(`UndefinedError: 'dict object' has no attribute 'status'`) because the live sensor is still the
*old* `demand_window_summary.py` (branch not yet merged → Pi hasn't pulled), so its `days` list
lacks the new attributes. Rewrote the card to be **self-contained**: it computes status, $, and
the pass/marginal/breach counts from `peak_kw` (present on the old sensor), so it renders
correctly *before* and *after* deploy and can never disagree with the sensor. Also switched the
YAML scalar from folded `>` to literal `|` (folded scalars mangle newlines and break markdown
tables). Rendered/verified with a jinja2 harness before handing over.

**PR opened:** `originalnomad/home-energy-agent` **PR #1** (`claude/morning-standup-q0ylqm` →
`main`) so the user can merge from an iPad (overseas, no Pi/SSH access). Once merged, the Pi's
30-min `git pull` + hourly summary cron deploy the re-banding automatically — no manual run needed.
`energy_rules.md` Rule 2 verification bullet and `CONTEXT.md` (key files + What to watch for)
updated to reflect the 3-way bands and pending-deploy state.
