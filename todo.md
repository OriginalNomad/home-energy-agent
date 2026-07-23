# Project To-Do List

## Energy Agent — Active

### Immediate

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
  3. Pin the exact entity next time: `ev_min_charge_price_c` has **max 60**, so a reading of 70¢
     cannot be that helper — it is a different entity or a misread. `ev_min_soc_pct` has **max 80**,
     and 80 was exactly the reported value, so "pinned to max" is a live hypothesis.
  4. Check HA → Settings → Automations & Scenes for **UI-created** automations/scripts (the YAML
     file cannot show these), and any myenergi/Zappi or Polestar integration that writes helpers.

  **Morning readings log** (append one row per morning; note "as found", before resetting):

  | date | entity | as found | expected | notes |
  |------|--------|---------:|---------:|-------|
  | 2026-07-23 | `ev_min_soc_pct` | 80 | 30 | at entity max |
  | 2026-07-23 | (reported "min price") | 70¢ | 40¢ | exceeds `ev_min_charge_price_c` max of 60 — entity unconfirmed |

- [ ] **Confirm the `SETTINGS_SPEC` intended values and bands** (Rule 28, added 2026-07-23).
  The spec now lives in `agent/energy_agent.py` and is version-controlled, so drift is a diff.
  `intended` was **seeded, not chosen** — from the live helpers after the 2026-07-23 reset, except
  `max_insurance_floor_pct` (live 0, seeded 70 = `DEFAULT_MAX_INSURANCE_FLOOR`). Please confirm:

  | setting | UI label | intended | band | status |
  |---|---|---:|---|---|
  | `ev_ultra_cheap_c` | "Fast charge if price ≤" | 10 | 0–12 | ✅ confirmed by user screenshot 2026-07-23 (CONTEXT's "6" is stale) |
  | `ev_standard_price_c` | "Slow charge if price ≤" | 15 | 0–25 | ✅ confirmed |
  | `ev_min_charge_price_c` | "Price to reach minimum" | 40 | 5–45 | ✅ confirmed |
  | `ev_min_soc_pct` | "Minimum charge target" | 30 | 0–50 | ✅ confirmed |
  | `ev_charge_target_pct` | "Goal (Max)" | 80 | 50–100 | ✅ confirmed |
  | `battery_charge_threshold_c` | — | 10 | 5–30 | ⬜ still unconfirmed (CONTEXT says 12 — see card discrepancy below) |
  | `max_insurance_floor_pct` | — | 70 | 20–95 | ⬜ still unconfirmed — **live 0 is out of band → 70 substituted** |
  | `ev_departure_target_pct` | (Scheduled Charge) | 95 | 50–100 | ⬜ still unconfirmed |

  **Card/helper discrepancy spotted in the same screenshot**: the Grid Price Forecast chart is
  annotated **"Charge threshold (12¢)"** while `input_number.battery_charge_price_threshold_c` is
  live at **10¢**. The card appears to hardcode 12 rather than read the helper — so the dashboard
  is displaying a threshold the agent is not using. Exactly the class of "UI is not an accurate
  representation of the values" problem this work is about. Fix the card to read the helper (or
  confirm 12 is intended and set the helper), then decide which value is right.

  **The two unconfirmed non-EV helpers are not on any dashboard** (verified 2026-07-23 against
  all five `lovelace*` files in the Pi's `.storage` — zero occurrences of either entity). So they
  cannot be set or seen in the console at all, despite `energy_rules.md` describing them as
  "user-settable sliders".

  - `battery_max_insurance_floor_pct`: no `initial:` in `configuration.yaml`, never exposed, so it
    has sat at **0 — HA's default-to-`min` for an untouched `input_number`**, not a choice anyone
    made. **Rule 15's insurance floor has therefore been inert since the helper was introduced
    (2026-05-31)**, not merely recently.
  - `battery_charge_price_threshold_c`: reads **10**, and its `min` is 5, so 10 is a real restored
    value set at some point — but there is now no UI to see or change it.

  **This is the actual fix**: put both on the Energy Agent dashboard so the console genuinely is
  the source of truth, then set them deliberately. Card YAML supplied in the 2026-07-23 session
  (dashboards are `mode: storage`, so it must be pasted via the UI). Until then the agent's band
  substitution is *masking* a control with no interface — which is the opposite of the intent.

  Note the five EV helpers **are** on a dashboard and were confirmed from the user's screenshot;
  only these two are orphaned.

- [ ] **Reconcile the overnight survival floor — the rule layer and Layer 0 disagree by 15 points**
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
  `battery_charge_threshold_c`, the spread calculation, and `forward_min_c`.

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
- [ ] **Cron `build_models.py` nightly (~2am)** — Phase 2.5-B isn't finished until retraining is automatic; it is still run by hand. `ARCHITECTURE.md` calls for a model-accuracy section in the nightly summary too.
  ⚠️ **When cronning this, fix the pull hazard at the same time.** The Pi's agent cron is
  `git pull -q && … && python3 agent/energy_agent.py` — an `&&` chain. `build_models.py` writes
  `agent/model_params.json` *in the working tree*, so the Pi always has a locally-modified tracked
  file. The moment a commit touching `model_params.json` is pushed from the Mac, the Pi's
  `git pull` fails, the chain short-circuits, and **the agent silently stops running entirely**.
  Either commit+push `model_params.json` from the Pi (it has working SSH remote access), or
  gitignore it and treat it as machine-local state, or decouple the pull from the run so a failed
  pull can't stop the agent. As of 2026-07-23 the Pi has an uncommitted `model_params.json`, so
  this trap is currently armed.

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

- [ ] **Make the charge-rate window asymmetric (blocks acting on the 5 kW regime change)** —
  `build_charge_rate_model_from_power()` uses a symmetric 10-day rolling *median*, which is robust
  to outliers but by construction slow to a genuine step change. The risk is not symmetric though:
  believing 5 kW when it is really 1.67 means starting late and risking a ~$30 demand charge;
  believing 1.67 when it is really 5 costs cents. So the model should be allowed to fall
  **quickly** (slower charging = safe direction, react in ~1 day) and rise only on **sustained**
  evidence (keep roughly the current inertia). Note a low quantile does *not* substitute for this:
  new-regime p25 is 4.96–4.99, so quantiles hedge within-regime variance, not a regime change.
  Under such a scheme today's data still would not flip `self_consumption` to 5.0 — which is the
  correct outcome. Decide: implement this, or simply let the median flip naturally ~2026-07-27.

  **Remaining next steps**: (b) test the *approach-taper* hypothesis with fine-grained (2-point) reserve−SoC gap buckets — the 12:00 cycle pulled only 0.5 kW at a 6-point gap vs 3.7–5.0 kW at 11–48 points, and the earlier analysis bucketed 1–20 together and averaged that away; (c) poll the Tesla API every ~10 s through a ramp to confirm the mode field genuinely never moves.
- [ ] **Check `solar_unreliable` calibration** — the solar corrector shows Solcast runs at 0.14–0.16 of actual at 08:00–09:00 in winter, so "7% of forecast" mornings may be normal rather than faults. If the flag fires on ordinary winter mornings it is mislabelling them, and it gates real rule behaviour.


- [x] **Run `build_models.py` on Pi** — done 2026-07-22. Required fixing three bugs first (the script had never executed). Solar correction ratios came in at **0.14–0.74**, well below the 0.5–1.5 range anticipated here — Solcast's winter morning over-forecast is far larger than assumed. Re-run periodically to accumulate autonomous samples.

- [x] **Reload HA automations** — `battery_low_soc_emergency_charge` (20¢ ceiling + 85% peak target) and both demand window warning automations (1-min debounce) were updated 2026-06-23. Reloaded 2026-06-24.
- [x] **Verify HA slider values** — deleted 2026-07-23. This item *was* the antipattern: it
  hardcoded four target values (`ev_ultra_cheap_threshold_c=6`, `ev_eco_gap_c=1.5`,
  `battery_charge_price_threshold_c=12`, `battery_max_insurance_floor_pct=70`), three of which
  were wrong against the live console and one of which (`ev_eco_gap_c`) names an entity that has
  never existed in the code or HA config. Superseded by Rule 28: the console is the source of
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
