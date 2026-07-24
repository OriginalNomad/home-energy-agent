# Project To-Do List

## Energy Agent — Active

### Immediate

- [ ] ▶️ **START HERE NEXT SESSION (2026-07-25): #3 — the 5-minute price problem (HIGH).**
  Full write-up is below under "HIGH — the agent decides on a 5-minute spot price". Needs a design
  decision first (which price source: the 30-min forecast slot `forecast[0]` the agent already
  fetches is the likely fix; whether to add threshold hysteresis), then implement. Also review how
  yesterday's four live changes (solar accuracy, Rule 30 survival floor, rebuilt model, priority-1
  path) behaved through the 2026-07-24 demand window before layering more on.

- [ ] ⭐ **NEW CAPABILITY — reserve-offset charge-rate controller** (found 2026-07-24 by live
  experiment; see energy_log "Charge-rate control RECOVERED"). Firmware 26.18.3 didn't remove the
  slow charge — it's still there in the taper as SoC approaches reserve. Measured dial (self_consumption):
  `reserve = SoC+5` → ~1.7 kW · `+10` → ~4 kW · `+20`→ 5 kW · `≤SoC` → idle. We only lost it because
  we always set reserve=85 (a 40-point gap = permanent 5 kW). **Proposal:** give the agent a target
  *rate* and translate it to `reserve = SoC + offset`, re-chased each cycle (it tapers to 0 as SoC
  reaches the reserve, so it's a chase not set-and-forget; 30-min cadence ~matches a 5-point chase;
  miss-a-cycle failure mode is safe). This directly fixes the summer concern (gentle grid top-ups
  that blend with solar instead of 5 kW slams) AND the over-import half of the 08:00 problem.
  **Before building:** characterise the taper at 2–3 more SoC levels (only tested at 63–65% so far) —
  the curve may shift with SoC/temperature. Then decide how the rule layer picks a target rate
  (e.g. gentle when time-rich + solar coming; fast near deadline). Interacts with the charge-rate
  *model* (priority 1): if we control the rate, the model's job shifts from "predict the rate" to
  "predict how long a chosen rate takes."

- [ ] 🔁 **REVIEW EACH MORNING UNTIL RESOLVED — HA threshold sliders drifting overnight**
  *(added 2026-07-23. `/morning` should check this every day and log the readings below until
  the cause is found. Delete this item once two clean weeks pass or the cause is fixed.)*

  **Symptom (user-reported, 2026-07-23):** the EV/battery threshold `input_number` sliders are
  repeatedly found at higher values than they were left at, and have to be reset by hand most
  mornings. Reported this morning: "minimum charge target" at **80%** (set to 30) and a
  "minimum price" at **70¢** (set to 40).

  **Established so far (2026-07-23) — nothing in this repo writes them:**
  - `energy_agent.py` only *reads* these entities (`ENTITIES` map, lines 147–153). Its sole
    `input_number` writes are the `battery_decision_*` dashboard helpers (lines 875–892).
  - No `input_number` writes in `config/automations.yaml`, and none in any other `agent/*.py`.
  - No `initial:` on any of these helpers in `configuration.yaml`, so an HA restart *restores*
    the last value rather than resetting to a default. **A restart is not the mechanism.**
  - So the writer is outside the repo: a UI-managed (storage-mode) script/automation, an
    integration, HA Assist/voice, a dashboard slider being nudged, or a phone-app mis-tap.

  **Audit trail — PARTLY SOLVED 2026-07-23 (Rule 28).** The agent now logs `settings_used` and
  `settings_violations` to `decisions.jsonl` every cycle, so drift is pinned to a 30-minute
  window without depending on HA's recorder. **From the next cycle on, the data to diagnose this
  collects itself** — check `settings_used` across cycles to see exactly when a value moves.
  Still unexplained: HA's recorder is *not* capturing these helpers (a 6-day history query
  returned one row per entity while live states carried same-day `last_changed`), and the
  `recorder:` block excludes only 5 Polestar sensors. Worth fixing separately — the logbook's
  `context_user_id` is what names the writer, and the agent's log cannot supply that.

  **Next steps, in order:**
  1. **Each morning, read `settings_used` in `decisions.jsonl`** for the overnight cycles — that
     now shows which helper moved and in which 30-minute window, automatically.
  2. Debug why HA's recorder isn't capturing these entities, then wait for one recurrence and
     read the logbook `context_user_id` — that names the writer directly, which the agent's own
     log cannot do.
  3. Each morning, record the readings before resetting anything (see table below).
  4. Pin the exact entity next time: `ev_min_charge_price_c` has **max 60**, so a reading of 70¢
     cannot be that helper — it is a different entity or a misread. `ev_min_soc_pct` has **max 80**,
     and 80 was exactly the reported value, so "pinned to max" is a live hypothesis.
  5. Check HA → Settings → Automations & Scenes for **UI-created** automations/scripts (the YAML
     file cannot show these), and any myenergi/Zappi or Polestar integration that writes helpers.

  **Morning readings log** (append one row per morning; note "as found", before resetting):

  | date | entity | as found | expected | notes |
  |------|--------|---------:|---------:|-------|
  | 2026-07-23 | `ev_min_soc_pct` | 80 | 30 | at entity max |
  | 2026-07-23 | (reported "min price") | 70¢ | 40¢ | exceeds `ev_min_charge_price_c` max of 60 — entity unconfirmed |
  | 2026-07-24 | all six EV helpers | stable | — | **EV sliders held overnight** (`ev_min_soc_pct`=30, rest steady) — this was flagged as "the real test" and it passed |
  | 2026-07-24 | `battery_max_insurance_floor_pct` | 0 | 30 | drifted back to 0 (set 30% on 07-23); 6 out-of-band violations, clamped to band floor 20. Operationally inert (dormant until April) but confirms drift isn't gone — it moved to a *different*, non-EV helper. Note: no `initial:`, so 0 = HA's default-to-min for an untouched helper → suggests the helper isn't persisting rather than being actively written |

- [x] **Confirm the `SETTINGS_SPEC` values and bands** — resolved 2026-07-23.
  The spec no longer holds *any* target values: it is `(alias, lo, hi)` bands only, because the HA
  console is the single source of truth and a duplicated target goes stale (that is how CONTEXT's
  "6¢" survived while the console said 10). All five EV helpers confirmed from the user's
  dashboard screenshot. `battery_charge_price_threshold_c` deleted (never wired to anything).
  `battery_max_insurance_floor_pct` and the deleted threshold were found to be on **no dashboard
  at all** — the floor's 0 was HA's default-to-`min` for an untouched helper, not a choice, so
  Rule 15's floor had been inert since 2026-05-31. Now carded and set to **30%**; live validation
  reports **zero violations**.
  Remaining thread, low priority: the Grid Price Forecast card annotates *"Charge threshold (12¢)"*
  — a hardcoded label for a helper that no longer exists. Remove or repoint it.

- [x] **Reconcile the overnight survival floor — RESOLVED 2026-07-24 (Rule 30).** User chose
  "trust the projection, ride lower." Lowered `battery_low_soc_emergency_charge` trigger + condition
  20% → 10% to align the safety net with the rule layer's designed 5%-floor ride (kept ~one
  agent-cycle margin above the physical reserve rather than going to exactly 5%). Deployed live via
  `deploy_ha_config.sh`, zero drift. Rule 30 documents it. **Watch:** confirm the oscillation is gone
  on the next genuine sub-10% morning. Original analysis retained below for context.

  ~~The rule layer and Layer 0 disagree by 15 points~~
  (found 2026-07-23 by replay; this is the *real* cause of the drain to 17%, not the solar
  forecast — see energy_log for the retraction.)
  - `compute_decision_context()` holds while `projected_soc_at_sponge > 5%`. At 00:00 on
    2026-07-23 it projected 12% and held; the projection was accurate.
  - `battery_low_soc_emergency_charge` (HA automation) triggers at **SoC < 20%**.
  - So the rule layer deliberately steers toward a trough that the safety automation treats as
    an emergency. Both are behaving as written. Last night the automation fired at 08:00, the
    08:30 HOLD immediately cleared reserve back to 5%, and SoC drifted down again — the two
    layers actively fighting.

  **Decide the intended overnight floor and make both layers use it.** This is a judgement call
  about how much demand-charge risk to carry overnight, so it needs your input rather than a
  default. Options: raise the rule layer's survival floor to ~20% to match Layer 0; lower the
  automation's trigger to match the 5% floor; or set an explicit intermediate floor (say 15%)
  and update both. Note the 5% floor was chosen deliberately in session 10 (the "5% survival
  floor replaces 20% threshold" change) — so raising it back is a reversal that should be
  reasoned about, not just applied.

- [ ] **HIGH — the agent decides on a 5-minute spot price, not a 30-minute one**
  (found 2026-07-23 chasing "why did the EV stay on Fast at 11¢ when the threshold is 10¢".)

  `sensor.1a_wigram_road_glebe_general_price` carries **`duration: 5`** — it is a 5-minute
  settlement price. The agent samples it once per 30-minute cycle and treats that single reading
  as *the* price for the whole interval. Real 5-minute prices swing hard, so every threshold
  comparison in the system is being made on what is effectively a sampled coin-flip.

  Measured on 2026-07-23, the sensor crossing the 10¢ EV threshold repeatedly within minutes:
  ```
  11:40:14 → 7¢    11:45:14 → 7¢     11:56:16 → 9¢
  11:41:14 → 11¢   11:46:14 → 10¢    12:00:17 → 11¢
  ```
  The 12:00 cycle sampled at 12:00:05 and saw **9¢** (set at 11:56:16) → `ev_ultra_cheap` → Fast.
  Twelve seconds later it was 11¢, which is what the dashboard showed and would have given Eco.
  **The agent was correct given what it sampled**; the input is the problem, not the logic.

  Affects every threshold in the system, not just EV: `ev_ultra_cheap_c`, `ev_standard_price_c`,
  `ev_min_charge_price_c`, the spread calculation, and `forward_min_c`.

  **Options** (needs a decision):
  1. Use the current **30-minute forecast slot** instead of the live 5-min sensor — the agent
     already reads a 30-min-granularity forecast for `price_forecast_6h`, so `forecast[0]` is
     probably the right value and costs nothing extra. Likely the correct fix.
  2. Average/median the last six 5-minute readings from HA history — more faithful to what you
     actually pay, but adds a history call per cycle.
  3. Add hysteresis to threshold comparisons so the mode doesn't flip on noise (worth doing
     regardless of 1 or 2, since Zappi mode changes have their own cost).

  Note this also explains why the dashboard and the agent can disagree about "the price" at any
  instant — same class of problem as the sliders: what is displayed is not what was acted on.

- [ ] **Paste the two dashboard cards** — both HA dashboards are `mode: storage` (UI-managed), so card YAML cannot be committed. Supplied in the 2026-07-22 session: the Manual Agent Override card (override toggle, remaining-to-full, corrected solar, reserve buttons) and the rewritten Solar Forecast card (corrected today/tomorrow, recalibrated <5/5–7/≥7 kWh bands, live inverter-vs-Solcast accuracy line).
- [x] **Re-run `build_models.py`** — done 2026-07-23 10:39 (`built_at: 2026-07-23`, `obs_days: 46`).
  **Answer: the 5 kW regime persisted — it is not a one-day event.** Per-day split of the same
  filtered power samples: 07-13→07-21 `self_consumption` median **1.66–1.67** kW with 0–4% of
  samples above 3 kW; **07-22 median 5.00 (92% fast), 07-23 median 5.00 (96% fast)**. By SoC
  bucket the new regime is 4.99–5.01 kW across 10–60% with p25 within 0.04 of the median — a
  clean step change, not outlier contamination. Below 70% SoC `self_consumption` and
  `autonomous` are now **indistinguishable**.
  **But `model_params.json` still reports 1.67 kW**, because `POWER_DAYS = 10` and `kw` is the
  *median*: 9 old-regime days outvote 2 new ones. It cannot flip until ~**2026-07-27**.
  See the new asymmetric-window item below.
- [x] **Cron `build_models.py` nightly (~2am) + fix the pull hazard** — done 2026-07-24 (session 19).
  Nightly cron added on the Pi (`0 2 * * * … build_models.py`). Pull hazard fixed two ways:
  (1) `model_params.json` untracked + gitignored (commit cfe7cfc) so writing it no longer dirties a
  tracked file — it's derived machine-local state like the already-ignored jsonl/db files; the agent
  loads it with a graceful `{}` fallback; (2) the agent cron's pull decoupled to
  `{ git pull -q || true; } && …` so a failed/conflicted pull can never stop the agent. Validated by
  running `build_models.py` on the Pi — completed, wrote the file, tree stayed clean. Cron backup:
  `/tmp/cron.bak` on the Pi. **Read the live model via SSH now** (Mac copy is a stale snapshot):
  `ssh energypi.local "cat ~/home-energy-agent/agent/model_params.json"`.
  ⏳ Still open (separate): ARCHITECTURE.md's call for a **model-accuracy section in the nightly
  summary** — not built.

- [x] **Phase 3 — retire the Mac HA instance** — done 2026-07-22. Stopped + `--restart=no`. No tunnel change was needed: cloudflared already pointed at `http://localhost:8123` (the Pi's own HA), so CONTEXT's old "→ 192.168.68.70:8123" was stale. `agent.sol.io` and `energypi.local:8123` both verified 200 after the stop.
- [ ] **Fix `shell_command.push_virtual_sensors`** — pre-existing, surfaced during consolidation. The command points at a *Mac* path, and the script isn't inside the HA container's mount (`~/homeassistant/config` → `/config`), so `restore_virtual_sensors_on_startup` cannot work on the Pi. Low urgency — `demand_window_summary.py --post` runs hourly via cron and re-pushes the sensors anyway. Fix by either copying the script into `config/` or moving the restore into the Pi's cron.
- [ ] **Explain 5 kW `self_consumption` charging (HIGH — decides the charge rate model)** — raising `backup_reserve_percent` above SoC pulled a sustained 5 kW three times on 2026-07-22, once triggered manually with the agent uninvolved, while `default_real_mode` stayed `self_consumption`. Ten days of 30-second data give a median of 1.67 kW for the same operation — a clean date boundary at 07-22. **Eliminated**: mode switch, HA automations, Storm Watch, Amber SmartShift, reserve−SoC gap (median 1.67 kW in every coarse bucket), SoC level, measurement artefacts. Leading hypothesis: overnight Powerwall firmware push (`26.18.3`), unverifiable — no version entity in HA.
  **UPDATE 2026-07-23 — (a) is answered: the 5 kW regime persisted a second day** (07-22 92% of
  samples >3 kW, 07-23 96%, median 5.00 both days, vs 0–4% on 07-13→07-21). Treat "anomaly" as
  "regime change" from here. The firmware-push hypothesis is stronger but still unverifiable.
  Consequence now live: the agent is planning against 1.67 kW while reality is 5.0 kW, a 3× error
  that makes it start charging far earlier than needed. Error is in the *cheap* direction (early
  arrival, wrong price) rather than the dangerous one, which is why this is not an emergency.
  This also retroactively explains the 2026-07-22 LP-vs-deterministic divergences: the LP held and
  its projected cost went negative while the rule layer charged 47%→80% at 12–13¢ — **the LP was
  right, and the rule layer was wrong because it was budgeting 3× the charging time it needed.**

- [x] **Make the charge-rate window asymmetric (blocks acting on the 5 kW regime change)** —
  done 2026-07-24 (session 19). Aggregation extracted into pure `_aggregate_charge_rates()`;
  headline `kw = min(median over POWER_DAYS=10, median over POWER_SHORT_DAYS=2)` → falls within
  ~1 day, rises only on sustained evidence (`min()` holds the pessimistic long value on a rise
  until the long window is majority new-regime; follows the short value down on a fall). Keeps
  `self_consumption` at 1.67 on 07-24 data (intended); safe flip to ~5 kW once the regime sustains
  (~07-27) with fall-fast protection. `kw_long`/`kw_short` recorded. New `test_build_models.py`
  (11 tests). Offline builder — no live effect until `build_models.py` is next run on the Pi
  (couple this with the nightly-cron item below, still open).

  **Remaining follow-ups** (separate from the window, still open): (b) test the *approach-taper* hypothesis with fine-grained (2-point) reserve−SoC gap buckets — the 12:00 cycle pulled only 0.5 kW at a 6-point gap vs 3.7–5.0 kW at 11–48 points, and the earlier analysis bucketed 1–20 together and averaged that away; (c) poll the Tesla API every ~10 s through a ramp to confirm the mode field genuinely never moves.
- [x] **Check `solar_unreliable` calibration** — done 2026-07-24 (session 19). `_solar_accuracy()`
  now measures actual against the *bias-corrected* this-hour forecast (raw × `_hour_solar_ratio()`
  from `model_params.json`), not raw Solcast. A normal winter morning flips from `unreliable — 13%`
  to `good — 95% of corrected`, so `expected_solar` is no longer zeroed on ordinary mornings.
  Genuine underperformance (below the calibrated expectation) still flags; near-zero corrected
  expectation → `not_applicable`. Falls back to raw when uncalibrated. 6 tests. Deployed live
  2026-07-24 mid-morning (control-path change — watch today's demand window). This was the real
  fix for the 2026-07-24 08:00 over-charge.


- [x] **Run `build_models.py` on Pi** — done 2026-07-22. Required fixing three bugs first (the script had never executed). Solar correction ratios came in at **0.14–0.74**, well below the 0.5–1.5 range anticipated here — Solcast's winter morning over-forecast is far larger than assumed. Re-run periodically to accumulate autonomous samples.

- [x] **Reload HA automations** — `battery_low_soc_emergency_charge` (20¢ ceiling + 85% peak target) and both demand window warning automations (1-min debounce) were updated 2026-06-23. Reloaded 2026-06-24.
- [x] **Verify HA slider values** — deleted 2026-07-23. This item *was* the antipattern: it
  hardcoded four target values (`ev_ultra_cheap_threshold_c=6`, `ev_eco_gap_c=1.5`,
  `battery_charge_price_threshold_c=12`, `battery_max_insurance_floor_pct=70`), three of which
  were wrong against the live console and one of which (`ev_eco_gap_c`) names an entity that has
  no longer exists in the code or HA config (it was real once — the retired Mac HA's
  `core.restore_state` has it at 1.0 on 2026-06-02 — but was dropped from `configuration.yaml`
  while the docs kept describing it). Superseded by Rule 28: the console is the source of
  truth and `settings_used` in `decisions.jsonl` records what the agent actually decided with.

### Architecture roadmap (in order)

- [x] **Phase 2.5-B — solar corrector — ACTIVATED 2026-07-22.** `optimizer.py` applies per-hour Solcast correction from `model_params.json["solar_correction"]`, now populated (ratios 0.14 at 08:00 → 0.74 at 13:00, n=24–90/hour). Autonomous rates populated too. Also surfaced on the dashboard via `sensor.solar_forecast_corrected`. **Remaining for this phase**: cron the rebuild nightly (see Immediate) — it is still run by hand.
- [ ] **LP to control path** — **divergence clock restarted 2026-07-22.** The LP ran on a hardcoded 50% SoC from its Jun 1 wire-in until Jul 22 (see energy_log), so *no* prior divergence analysis is valid — including the previously recorded blocker ("LP defers to cheapest slot; det charges at first acceptable slot"), which was never actually measured. Fixed and 4 regression tests added. **Next: collect a fresh week of clean three-way data (from 2026-07-22) before reassessing.** First clean signal to watch: under flat prices the LP defers charging to the last feasible slot with no error margin — likely needs the `risk` knob or a conservative solar quantile. Plan unchanged: LP-authoritative with deterministic layer as hard-constraint backstop (Rule 2, survival floor, reserve guard). Kill-switch already in place.
- [ ] **Analyst agent** — weekly agent that reads `decisions.jsonl` + `daily_energy.jsonl` and surfaces systematic patterns: "cloudy mornings consistently start charging 1h late", "sponge threshold too tight 3 weeks in a row". Outputs proposed rule changes in plain English for human review. This is the feedback loop that makes the system self-improving rather than just self-executing.
- [ ] **Savings dashboard** — daily/weekly $ saved vs naive baseline (flat-rate charging, no demand management, no solar optimisation). Broken down by: demand charge avoided, cheap-window differential, solar self-consumption. Core product metric; also the first Sol feature users need to see.

### Tune from winter data (review at next session and monthly)

- [ ] **Tune `SOLAR_SPONGE_PRICE_THRESHOLD`** — currently 10¢. If winter Solar Sponge prices are regularly 12–18¢, overnight_hold fires too aggressively and battery arrives flat. Check the `rule_fired=overnight_hold_wait_for_sponge` cycles in `decisions.jsonl` against actual sponge prices.
- [ ] **Validate `peak_survival_wait_for_sponge` thresholds** — the 3h window and 5¢ price gap were set on one morning's data (Jun 23). Winter will give dozens of cycles. Check outcomes: did waiting pay off (sponge arrived and was cheaper), or did the battery hit the floor?
- [ ] **Tune historical price model** — `CHEAP_BAND_ALPHA`, `MAX_INSURANCE_FLOOR`, `PRICE_HISTORY_DAYS`. Set on May flat-price data; winter has larger swings. Review after 4 weeks of July data.
- [ ] **Tune `α` / `MIN_DAILY_SWING`** in `_hours_to_cheap_end` — same issue, set on May data.
- [ ] **Amber price forecast accuracy + risk premium** — needs ~4 weeks of JSONL data to compute signed forecast error per time-of-day bucket. 75th-percentile evening error → empirical spread threshold, replacing gut-feel 5¢. Due ~July.
- [x] **Update charge rate model — rebuilt 2026-07-22 from instantaneous power.** Now measured from `battery_power` at ~30 s resolution (n=53–432/bucket) rather than 30-min SoC deltas, which conflated rate with duration and gated on a lagging reserve sensor. self_consumption 1.67 kW flat 0–70%; **autonomous 5.0 kW to 70%, 2.92 at 80%, 1.84 at 90%** — the taper was previously missing (n=2–5 → flat 5.0 kW), making the agent optimistic exactly where the 2:55pm deadline is decided. Two claims of mine that day (a "long right tail", then "self_consumption is really 5 kW") were both **retracted** — see energy_log. Open question tracked separately under the 5 kW anomaly item.

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
