# Project To-Do List

## Energy Agent — Active

### Immediate — DO FIRST when back at home (next month)

> **📋 HOME UNPACK CHECKLIST (early next week)** — ordered; details for each are in the entries below.
>
> **A. Patches (code — review → `python agent/test_decision.py` → deploy):**
> 1. **[DRAFTED]** Cliff-guard patch — on branch `claude/morning-standup-arsr82`, **113 tests green**.
>    Review the diff to `energy_agent.py` + `test_decision.py` + `energy_rules.md`, run the tests,
>    merge to `main`, then `git pull` on the Pi. *This is the fix for the morning grid-charge you've
>    been seeing — it stops the 9am cliff. Highest value.*
> 2. **[NOT YET WRITTEN]** "hold reverts stale autonomous" patch — make a `hold` verdict switch a
>    stranded `autonomous` mode back to `self_consumption` when `soc >= target`, so a full battery
>    can't sit in reserve=100% starving the EV (today's second bug). Ask me to draft it.
> 3. Verify **`battery_autonomous_revert_target_reached`** is actually enabled in the HA UI — it
>    didn't fire on 2026-07-12.
>
> **B. Pi / data (need SSH or on-LAN access):**
> 4. **Run `build_models.py`** — populate `solar_correction` + autonomous charge rate.
>    ⚠️ NB: this only feeds the *shadow* LP (`optimizer.py`), **not** the control path — it does
>    *not* by itself fix the morning grid-charge (patch A1 does). It's the prerequisite for the
>    eventual LP-to-control cutover.
> 5. **Rotate the hardcoded HA bearer token** committed in `demand_window_summary.py:42` +
>    `log_daily_energy.py:55` → move to `.env`/`secrets.yaml`. (Secret in git — do early.)
> 6. **Deploy the Jul-3 demand re-banding**: `git pull` → run `log_daily_energy.py` →
>    `demand_window_summary.py --post` (crons pick it up otherwise).
> 7. **Pull recent `decisions.jsonl` + `daily_energy.jsonl`** back to a synced location (or paste me
>    a slice) so the **three-way decision analysis** and **daily-journal schema review** can finally
>    run — both have been blocked in every web session (files are gitignored, Pi-only).
> 8. Confirm the **09:00/09:30 rule_fired for 2026-07-12** to prove the cliff bit (see A1 entry).
> 9. Verify **HA slider values** (below).

- [~] **🐛 BUG (FIX DRAFTED — on branch `claude/morning-standup-arsr82`, 113 decision tests green;
  needs review + deploy) — the `now_h >= 9` solar-unreliable guard is a cliff: a good solar day gets
  thrown away the moment the clock passes 9am if the panel is still ramping (control-path)**
  **Observed live 2026-07-12** (peak month, via `agent.sol.io` + the 08:30 agent narrative):
  20% SoC, 8:30am, `remaining_today = 17.1 kWh` (genuine big-solar-day), actual output **0.09 kW**
  vs Solcast this-hour **1.18 kW** → `_solar_accuracy()` returns **"unreliable"** (8% of forecast).
  **At 08:30 the agent HELD correctly** (rule `peak_solar_will_cover`, cleared reserve 20%→5%) —
  *only* because `now_h < 9` keeps `solar_unreliable = False` despite the "unreliable" string.
  (The 14¢ charging the user first saw was separate: reserve sitting at the 20% floor while SoC was
  19–20%, so the Powerwall trickled from grid to hold the floor — "hold ≠ arming". The 08:30 hold
  cleared it.)
  **The defect:** `solar_unreliable = (accuracy in ("poor","unreliable") and now_h >= 9) or
  zero_solar_day` (`energy_agent.py:1395`) flips as a **step function at exactly 9:00am**. With the
  accuracy string *already* "unreliable" at 08:30, the **09:00/09:30 cycle** — same 20% SoC, same
  17 kWh forecast, if the panel is still lagging — will set `solar_unreliable=True`, zero
  `expected_solar` (`:1427`), make `kwh_needed_85` the full ~8.8 kWh gap (`:1449`), and fire a grid
  charge (`peak_charge_now` / `peak_deadline_autonomous`), bypassing the wait-for-cheap branches
  (`wait_for_cheap_go_hard` `:1594`, `peak_early_morning_hold` `:1606`). Flat 6–13¢ prices made it
  low-harm *today*, but on a higher-priced slow-ramp morning it's a real, repeatable cost.
  **Why it's a bug, not just bad input:** `_detect_zero_solar()` (`:1163`) already guards this
  correctly — ignores near-zero output before 10am while Solcast `remaining > 2 kWh`, and needs 2+
  zero cycles. The accuracy path has neither guard: one ramp-lagged sample at/after 9am flips it.
  **Fix (DRAFTED 2026-07-15):** added a morning-ramp guard in `compute_decision_context()` — during
  `SOLAR_START_HOUR ≤ now < SOLAR_RAMP_SETTLE_HOUR (10.0)`, a `poor`/`unreliable` reading no longer
  flips `solar_unreliable` while `remaining_today > SOLAR_HEALTHY_REMAINING_KWH (2.0)`. Mirrors the
  pre-10am guard `_detect_zero_solar` already has, so the two solar-distrust paths agree. New consts
  by `SOLAR_START_HOUR`; logic replaces the old one-line `solar_unreliable` assignment (~`:1395`).
  Documented in `energy_rules.md` (Rule 11 "Morning-ramp guard"). **Tests added:**
  `test_solar_unreliable_ramp_guard_holds_good_solar_day` (the live 9:30am repro → holds),
  `_expires_after_settle` (10:30 still escalates), `_not_when_remaining_low` (≤2 kWh → still trusts
  the unreliable label). Full suite **113 passed, 0 failed**.
  **At home:** review the diff, run `python agent/test_decision.py`, then deploy. *Tuning:* if the
  flat roof still ramps past 10am in winter, nudge `SOLAR_RAMP_SETTLE_HOUR` → ~10.5 (and update
  `test_solar_unreliable_after_9am`, which pins the 10:00 boundary).
  **Confirm on Pi:** pull the 09:00 + 09:30 `rule_fired` + `forecast_accuracy` from `decisions.jsonl`
  to verify the cliff actually bit on 2026-07-12 (expected: `peak_deadline_autonomous` once past 9am).
  **Live manifestation later the same day (2026-07-12):** the cliff appears to have bitten — battery
  went 20% (08:30 hold) → **99% via `autonomous` grid-charge**, i.e. ~8 kWh pulled from grid on a
  17 kWh solar day the sun would have covered for free. Then it got **stranded in autonomous at 99%**:
  (a) a `hold` verdict carries `mode=None` so the agent never reverts autonomous→self_consumption, and
  (b) `battery_autonomous_revert_target_reached` did not fire. Because `autonomous` runs with
  `reserve=100%` (the export guard suppresses grid export below the Zappi's ~1.44 kW floor), a
  plugged-in EV in **Eco+ got no surplus** — solar was exporting/curtailing instead of charging the
  car. Manually switching the Powerwall to Self-Powered fixed the EV immediately. **Two follow-on
  items this exposed:** (1) make a `hold` verdict revert a stale `autonomous` mode to self_consumption
  when `soc >= target` (don't rely solely on the HA revert automation); (2) verify
  `battery_autonomous_revert_target_reached` is actually enabled in the HA UI.

- [ ] **Run `build_models.py` on Pi** — Phase 2.5-B is implemented and pushed but model_params.json needs to be rebuilt from live data to activate the solar corrector and autonomous charge rate model. SSH into Pi and run:
  ```bash
  cd ~/home-energy-agent
  git pull
  agent/venv/bin/python agent/build_models.py
  git add agent/model_params.json
  git commit -m "model_params: rebuild $(date +%Y-%m-%d)"
  git push
  ```
  Output will show solar correction ratios (expect 0.5–1.5 range) and autonomous charge rates. Check numbers look sensible before committing. Note: autonomous rates may still be sparse — check the n= counts.

- [x] **Reload HA automations** — `battery_low_soc_emergency_charge` (20¢ ceiling + 85% peak target) and both demand window warning automations (1-min debounce) were updated 2026-06-23. Reloaded 2026-06-24.
- [ ] **Verify HA slider values** — confirm after June 2 restart: `ev_ultra_cheap_threshold_c=6`, `ev_eco_gap_c=1.5`, `battery_charge_price_threshold_c=12`, `battery_max_insurance_floor_pct=70`.

### Architecture roadmap (in order)

- [x] **Phase 2.5-B — solar corrector (wired in, 2026-06-27)** — `optimizer.py` now applies per-hour Solcast correction from `model_params.json["solar_correction"]`; autonomous rate uses `_model_avg_rate_kw()` from `model_params.json["charge_rate_kw"]["autonomous"]`. `build_models.py` created. **Pending**: run `build_models.py` on Pi (see "DO FIRST" item above) to populate the model data — until then both models fall back to their priors (Solcast uncorrected, autonomous flat 5.0 kW).
- [ ] **LP to control path** — LP shadow has been running since Jun 1. Blocker: timing divergence (LP defers to cheapest slot; det charges at first acceptable slot). Once solar corrector is wired in and LP has calibrated solar, this gap should close. Plan: LP-authoritative with deterministic layer as hard-constraint backstop (Rule 2, survival floor, reserve guard). Kill-switch already in place.
- [ ] **Analyst agent** — weekly agent that reads `decisions.jsonl` + `daily_energy.jsonl` and surfaces systematic patterns: "cloudy mornings consistently start charging 1h late", "sponge threshold too tight 3 weeks in a row". Outputs proposed rule changes in plain English for human review. This is the feedback loop that makes the system self-improving rather than just self-executing.
- [ ] **Savings dashboard** — daily/weekly $ saved vs naive baseline (flat-rate charging, no demand management, no solar optimisation). Broken down by: demand charge avoided, cheap-window differential, solar self-consumption. Core product metric; also the first Sol feature users need to see.

### Tune from winter data (review at next session and monthly)

- [ ] **Tune `SOLAR_SPONGE_PRICE_THRESHOLD`** — currently 10¢. If winter Solar Sponge prices are regularly 12–18¢, overnight_hold fires too aggressively and battery arrives flat. Check the `rule_fired=overnight_hold_wait_for_sponge` cycles in `decisions.jsonl` against actual sponge prices.
- [ ] **Validate `peak_survival_wait_for_sponge` thresholds** — the 3h window and 5¢ price gap were set on one morning's data (Jun 23). Winter will give dozens of cycles. Check outcomes: did waiting pay off (sponge arrived and was cheaper), or did the battery hit the floor?
- [ ] **Tune historical price model** — `CHEAP_BAND_ALPHA`, `MAX_INSURANCE_FLOOR`, `PRICE_HISTORY_DAYS`. Set on May flat-price data; winter has larger swings. Review after 4 weeks of July data.
- [ ] **Tune `α` / `MIN_DAILY_SWING`** in `_hours_to_cheap_end` — same issue, set on May data.
- [ ] **Amber price forecast accuracy + risk premium** — needs ~4 weeks of JSONL data to compute signed forecast error per time-of-day bucket. 75th-percentile evening error → empirical spread threshold, replacing gut-feel 5¢. Due ~July.
- [ ] **Update charge rate model** — `model_params.json` has no autonomous mode data (17 days, no fast-charge cycles). Rebuild when autonomous charging accumulates (check `battery_mode='autonomous'` rows in `energy_log.db`).

### Infrastructure

- [ ] **Watch Tessie reliability** — `tessie_soc_failed` logged in JSONL each cycle. If recurring (>1/day), investigate rate throttle or token renewal.
- [ ] **Switch Pi boot to SSD** — Pi runs from SSD (`/dev/sda2`) but SD card may still be in boot order. Confirm in `raspi-config`.
- [ ] **Tessie cost review** — ~A$10/month. Review savings achieved once first full peak month (June) data is analysed. Replace with Tesla Fleet API (personal OAuth) if cost isn't justified.
- [ ] **Migrate HA to Pi** — Docker HA on Pi, restore from Mac backup. No urgency while Mac Studio stays on, but removes the single point of failure.
- [ ] **Daikin AC integration** — AC load during demand window is the biggest unmodelled variable. If Daikin has a HA integration, it would let the agent anticipate load spikes at 3pm.

### Nice-to-have / low priority

- [ ] **BOM solar forecast** — Solcast unreliable in winter. Investigate `api.weather.bom.gov.au` gridded solar radiation as an alternative or ensemble weight. Needs InfluxDB tracking of Solcast vs actual first.
- [ ] **InfluxDB dashboards** — SoC history, charging patterns, price vs SoC correlation, 3pm accuracy over time. Foundation for the analyst agent.
- [ ] **Dynamic demand-window target** — 85% is conservative. LP naturally computes a smarter target (cover just the 3–9pm load) when authoritative. Not worth building as a rule.
- [ ] **LP `three_way_review.py` live-only filter** — add `--live-only` flag to filter out back-filled records with synthetic solar. Low value now that solar_unreliable is wired in.

---

## Product — Sol

- [ ] **Savings dashboard** — see above; also the first Sol feature
- [ ] **Analyst agent** — see above; directly reusable as Sol's learning layer
- [ ] **Multi-tenant architecture** — define how Sol handles multiple sites with different tariffs, hardware, and grid operators
- [ ] **Tesla Fleet API** — personal OAuth path; also the API Sol would use at scale (no Tessie dependency)
- [ ] **Migrate to Anthropic Managed Agents** — hosted infra solves the Mac-sleep / cron reliability problem; MCP Tunnels for HA access; relevant as Sol's compute layer
- [ ] **Multi-battery support** — Sonnen, BYD, generic Modbus
- [ ] **Multi-tariff support** — Amber AU, Octopus UK, Tibber EU

---

## Done ✅ (key milestones)

- Deterministic rule layer in control (`DETERMINISTIC_AUTHORITATIVE=True`, 2026-06-06)
- LLM narrative-only + prompt slimmed 86% (Phase 6, 2026-06-09)
- Selective narrative — routine cycles skip LLM (Phase 7, 2026-06-23)
- Charge rate model from DB observations (Phase 2.5-A, 2026-06-23)
- LP solar_unreliable fix — stops mpc_solar_only on cloudy mornings (2026-06-23)
- peak_solar_cover_survival + peak_survival_wait_for_sponge rules (2026-06-23)
- Emergency automation hardened: 20¢ ceiling + 85% peak target (2026-06-23)
- Demand window warning debounced: 1-min sensor glitch guard (2026-06-23)
- Three-way shadow layer: LLM vs deterministic vs LP (2026-06-01)
- LP horizon extended to 22:00 with synthetic price model (2026-06-03)
- data_logger.py wired in — energy_log.db accumulating (2026-06-06)
- Tessie SoC=0 guard (2026-06-06)
- Demand-window reserve guard + HA health check (2026-06-02)
- Agent deployed to Pi on cron with auto git pull (2026-06-05)
- Cloudflare Tunnel — agent.sol.io → HA (2026-06-05)
