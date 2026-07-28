# Project To-Do List

## Energy Agent — Active

### Immediate

- [x] **#3 — the 5-minute price problem — DONE 2026-07-25 (Rule 32).** `compute_decision_context()`
  now anchors `price` on `price_forecast[0]` (the current 30-min slot, averaged) instead of the raw
  5-min `duration:5` sample, fixing every threshold at once (spread, forward_min, deferral/sliding,
  cost-target, all three EV thresholds) since `price` is the single anchor. Verified forecast[0] is
  the current interval (00:00 cycle: spot 11¢ vs slot 13.2¢). Falls back to spot when forecast empty;
  kill-switch `PRICE_USE_30MIN_SLOT`; logs `price_used_c`+`price_spot_c`. **Design choice:** slot only,
  hysteresis deferred (measure `price_used_c` for residual boundary flips first). 3 tests + 1
  pre-existing mismatch-reliant test fixed; 221 decision total. **Not yet deployed** — pending `git
  push`. HA automations still read the 5-min sensor (coarse 20¢/0¢ thresholds — separate, low value).

  ▶️ **START HERE NEXT SESSION:** the 2026-07-26 incident (see below) exposed robustness gaps that now
  outrank the ⭐ model-calibration follow-ups. Do the **agent-robustness hardening** item first (LLM
  try/except is small and highest-value), then revisit the **overnight strategy** that left the battery
  near-empty on a peak morning. After that: the ⭐ follow-ups (build_models learning the offset→rate
  curve from `charge_offset_pts`) and the architecture roadmap (LP divergence, analyst agent, savings
  dashboard).

- [x] 🔐 **HIGH — rotate the three live keys + finish the config scrub — DONE 2026-07-28.**
  All three rotated by the user and verified live; the config `!secret` migration is deployed. Details:
  - **HA long-lived token** — regenerated, `.env` (Pi) updated, old revoked. Verified: `GET /api/` → 200.
  - **Solcast key** — regenerated + reconfigured in HA. Verified: `sensor.solcast_pv_forecast_*` all
    fresh (updated 1 min ago, `api_used` reset), same entity ids.
  - **Tessie token** — regenerated (tessie.com → Settings → Developer); updated in **both** Pi stores
    (`agent/.env` `TESSIE_TOKEN` raw, and `secrets.yaml` `tessie_bearer: "Bearer …"`). Config migrated:
    the 4 `configuration.yaml` headers now use `!secret tessie_bearer` (0 inline tokens), deployed via
    `./deploy_ha_config.sh`, then `rest.reload` + `rest_command.reload` (a plain deploy-reload does NOT
    reload those domains). Verified all three paths: agent `.env` token → Tessie live_status 200;
    HA read → `sensor.tessie_powerwall_charge` = 32; HA write → `rest_command.powerwall_set_backup_reserve`
    no-op → 200 (the exact 2:55pm safety call). Zero config drift after deploy.
  - **The historical copies in git are now worthless** (rotation done) — history rewrite stays optional.
  - **Secrets now single-source on the Pi** (see robustness #4): Mac `.env` bannered dev-only,
    `.env.example` + CONTEXT updated.
  - Still optional (low priority): regenerate the HA backup **encryption key** + Tesla Powerwall
    installer passcodes (were in the deleted PNGs); the git-history rewrite for the 8 stale `claude/*`
    branches. Neither blocks anything now that the live keys are rotated.

- [x] ~~🔐 **HIGH — rotate the secrets that were in git history, then finish the config scrub** (2026-07-26).~~
  *(superseded by the DONE entry above — original detail retained below for the record.)*
  Secret-bearing files (4 PNGs) and hardcoded tokens had been committed/pushed to GitHub, so per the
  global rule they are **compromised — rotation is the real fix**, not just removal.
  - **Working tree scrubbed 2026-07-26** (this session, no force-push, Pi untouched): removed the
    hardcoded HA token from `energy_agent.py` / `push_virtual_sensors.py` / `demand_window_summary.py` /
    `log_daily_energy.py` (all read `HA_TOKEN` from `agent/.env` now), the Solcast key from `CONTEXT.md` /
    `app/docs/homeassistant-context.md` / `energy_log.md`, and the dead Anthropic key from
    `PI_MIGRATION.md`; added `agent/.env.example`. Committed the user's deletion of the 4 secret PNGs.
    History was **not** rewritten (deferred by choice) — so the old values still exist in history +
    on GitHub until rotated.
  - **⚠️ ROTATE (user action, essential):**
    1. **Tessie token** (`BEVtCQ…`) — controls the Powerwall. Regenerate at tessie.com; update
       `agent/.env` (Pi) `TESSIE_TOKEN` **and** the Pi's `~/homeassistant/config/secrets.yaml`.
    2. **HA long-lived token** (`eyJhbGci…`) — regenerate in HA (Profile → Long-lived tokens); update
       `agent/.env` (Pi) `HA_TOKEN`.
    3. **Solcast API key** (`I6bgku…`) — regenerate at solcast.com; update the HA Solcast integration.
    4. Anthropic key `…xAAA` already dead (401'd 2026-07-26); `…dAAA` is live in `agent/.env` (Pi).
    5. Consider regenerating the HA backup **encryption key** and Tesla Powerwall installer passcodes
       (they were in the deleted PNGs).
  - **Remaining scrub — `config/configuration.yaml` Tessie token** (still inline in 4 `rest_command`
    Authorization headers; HA can't read `.env`). Do this **with the Tessie rotation**: add
    `tessie_bearer: "Bearer <new-token>"` to the Pi's `~/homeassistant/config/secrets.yaml`, change the
    4 headers to `Authorization: !secret tessie_bearer`, then `./deploy_ha_config.sh` and confirm
    rest_commands still load (`/api/services` has `rest_command`). Needs a deploy — excluded from this
    session's "Pi untouched" scope.
  - **Optional later — history rewrite** (git-filter-repo to purge the PNGs + secret strings from all
    170 commits and the 8 stale `claude/*` branches, force-push, reconcile the Pi). Only worthwhile if
    rotation is somehow delayed; rotation makes the historical copies worthless.

- [ ] 🧹 **LOW — delete the 8 stale `claude/*` remote branches** (abandoned morning-standup / automation
  runs, June–July 2026). They clutter `git branch -r` and carry the pre-scrub secrets in their history.
  Confirm none hold unmerged real work first (`git log origin/main..<branch>`), then
  `git push origin --delete <branch>`.

- [ ] 🛡️ **HIGH — agent robustness hardening** (surfaced by the 2026-07-26 incident: an expired
  `ANTHROPIC_API_KEY` crashed the agent at the LLM narrative call every cycle — control survived, but
  logging/notifications went dark and it looked frozen). In priority order:
  1. **Fault-isolate the LLM call — DONE 2026-07-28.** The LLM narrative loop is wrapped in try/except;
     on failure (expired key / outage / network) the cycle no longer crashes — it degrades to the same
     deterministic `_build_auto_summary` + `log_decision` the Phase-7 routine path uses, so
     `decisions.jsonl` / dashboard helpers / notifications still get written. Guards: a `_llm_logged`
     flag prevents a double-write/re-notify if the LLM fails on a *later* turn (after it already logged);
     the fallback reads `_cycle_context["decision_context"]` (not bare `_ctx`) so the safety-net can't
     itself NameError if the shadow block failed early. New JSONL field `llm_narrative_failed` marks
     degraded cycles (feeds the liveness/expiry items below). Verified: 222 decision tests still pass,
     module imports clean, `_build_auto_summary({})` tolerates a sparse ctx. **Not yet a dedicated unit
     test of the fallback path** — that needs mocking the whole cycle (Anthropic+HA+Tessie) or a small
     refactor to extract the loop; deferred (offered).
  2. **Liveness alerting — agent side DONE 2026-07-28 (Healthchecks.io chosen).** The agent pings a
     dead-man's-switch on every completed cycle via `_send_heartbeat()` (monitor-agnostic ping URL;
     no-op if `HEALTHCHECK_URL` unset; never raises). Entrypoint wraps `run_agent`: success ping on
     normal return (body `ok`, or `degraded: llm_narrative_failed` on an LLM-degraded cycle), `/fail`
     ping if `run_agent` raises. Skipped for `--dry-run`. **Alert cadence is set on the check, not in
     code** — recommended period 30 min, grace ~2 h (≈4 missed cycles) per the user's "don't hair-trigger"
     preference. **User setup DONE 2026-07-28:** Healthchecks.io check created; ping URL
     (a `hc-ping.com/…` capability URL, value in the Pi's `.env` only) added; test ping verified HTTP 200/"OK" (check
     is "up"). Code deployed (`3c40668`) — the Pi starts real per-cycle pings on its next cron pull.
     ⏳ Confirm the check's **period 30 min / grace ~2 h** so the gap before the first real agent ping
     doesn't false-alarm. **Deferred follow-on:** a
     tighter HA-side staleness check *during* the 3–9pm demand-window run-up (a relaxed grace could
     alert after 2:55pm) — the "loudly going into the demand window" piece; the user called it an edge
     case for now.
  3. **Silent key-expiry**: startup key-health ping that notifies on 401; consider a non-expiring key +
     billing alert.
  4. **Single source of truth for secrets** — **DECIDED + DOCUMENTED 2026-07-28: the Pi is canonical.**
     Production secrets live only in `energypi.local:~/home-energy-agent/agent/.env`; the Mac's `.env` is
     dev-only and now carries a DEV-ONLY banner; `agent/.env.example` + CONTEXT.md "Secrets" spell out the
     model. This closes the incident's root cause (edited Mac, agent runs on Pi). *Optional future nicety
     (not built):* a small Pi helper to derive HA's `secrets.yaml` `tessie_bearer` from `.env`'s
     `TESSIE_TOKEN`, so a Tessie rotation is one edit instead of two — deferred (secrets.yaml is
     root-owned; not worth injecting into the live rotation path today).
  5. **Pi single-point-of-failure**: a coarse always-on fallback in the Tesla app (Time-Based Control /
     a reserve schedule) so a dead Pi can't cost a demand charge; UPS / auto-restart.

- [ ] **MEDIUM — revisit the overnight charging strategy** (surfaced 2026-07-26). The battery rode
  11–18% overnight on `wait_for_cheap_go_hard` because prices never dropped below ~14¢, so it arrived at
  the peak morning near-empty and needed a big morning charge. Rule 30's 12% floor held (good) and Rule
  33 now softens the morning *response*, but the strategy of waiting all night for a cheap sponge window
  that doesn't come — on a **peak day** where 85% is required by 2:55pm — leaves no margin. Consider: on a
  peak-day eve, top up gently overnight when the cheapest available slot is still above threshold, rather
  than riding the floor. Check the `wait_for_cheap_go_hard` overnight cycles against the actual morning
  charge cost before changing anything.

- [ ] **MEDIUM — `survival_floor_defend` is price-blind; the LP handles the spike better**
  (surfaced 2026-07-28). On 07-28 the battery rode to ~10% and a morning price spike hit (34¢ realised
  at 07:30, forecast **36¢** at the 07:00 cycle). Rule 30's `survival_floor_defend` is *reactive and
  price-blind* — it topped up **at the 34¢ peak** because it only looks at SoC ≤ 12%, never the forward
  price. The LP shadow got it right: at the 07:00 cycle it fired `mpc_charge_grid` (target 24%, 2.95 kW)
  to **pre-charge across the spike** one slot before it landed. A human would do the same — buy the
  survival minimum at the cheapest pre-spike slot (~05:00/14¢) instead of at the peak.
  **Just collecting data points this week — no change yet.** Watch `survival_floor_defend` cycles vs the
  LP's `optimizer_verdict` on low-SoC mornings; note each time the rule buys at a local spike the LP
  avoided. Caveats to keep in mind: (i) the LP charged at 07:00/22¢, not 05:00/14¢, because its floor is
  5% and it cuts survival fine — a higher floor / the `risk` knob would make it buy earliest-and-cheapest;
  (ii) the LP is *not* uniformly better — on flat-price peak mornings (e.g. 07-26) it held too long and
  would have missed the deadline. **Two fix options when we act:** (a) *incremental* — make Rule 30
  forward-price-aware (defend the floor at the cheapest look-ahead slot before the projected breach; don't
  top up *at* a spike if the floor won't breach before a cheaper slot); (b) *structural* — hand
  survival/spike timing to the LP (the Phase-5 / LP-to-control direction). See energy_log 2026-07-28.

- [x] **#2 (from the incident) — a `hold` must revert autonomous mode — DONE 2026-07-26.** The hold
  branch of `_execute_deterministic_verdict` now commands `self_consumption` if the current mode isn't
  already that, and drops reserve to 5% unconditionally when it reverts (was gated on
  `sensor.powerwall_backup_reserve`, which read a stale 5% while the true setpoint was ~57%). This was the
  direct cause of the un-stoppable 5 kW charge on 2026-07-26. 3 tests. Deployed (`a7a8c38`).

- [x] **Rule 33 — receding-horizon deadline escalation — DONE 2026-07-26.** The peak deadline branch no
  longer slams 5 kW autonomous the instant self_consumption can't fill the whole gap; it gentle-leads
  (`peak_deadline_gentle_lead`) and escalates to autonomous only at the fast rate's point-of-no-return
  (`hours_to_2_55 ≤ fill_fast_85 + FAST_ESCALATE_BUFFER_H`, default 1.5h). Kill-switch
  `DEADLINE_GENTLE_LEAD`. 3 tests + `test_peak_deferral_trap_selfcons` updated. Deployed (`a7a8c38`).
  **Watch:** confirm gentle-lead on the next low-SoC peak morning; tune the buffer if it ever cuts 85%
  close.

- [x] ⭐ **NEW CAPABILITY — reserve-offset charge-rate controller** — **v1 built 2026-07-25 (session 20).**
  Rule 31 / `_gentle_charge_reserve()` in `energy_agent.py`. On a self_consumption charge the agent
  now chases `reserve = min(SoC + SELF_CONS_CHARGE_OFFSET_PTS, target)` (offset 6 → ~1.6 kW
  cycle-average) instead of a fixed reserve=85 that slammed 5 kW under firmware 26.18.3. autonomous
  unchanged. Kill-switch `GENTLE_CHARGE_CONTROL`; SoC-unreadable falls back to old behaviour; JSONL
  logs `charge_target_pct`/`reserve_cmd_pct`/`charge_offset_pts`/`charge_rate_intent`. 7 tests (203
  decision total). **Design decision (with user):** mode-as-selector (restore intent), build-now +
  log-to-calibrate-later — the `min(…,target)` clamp is structurally safe at any SoC, so we did not
  gate on characterising the taper at more SoC levels first.
  **Remaining follow-ups (not v1):** (a) characterise the taper at 2–3 more SoC levels and have
  `build_models.py` learn the offset→rate curve from the newly-logged commanded offsets vs
  `battery_power`; (b) intermediate rates (offset +10 ≈ 4 kW "medium") if the rule layer ever wants
  a mid-tier; (c) tune `SELF_CONS_CHARGE_OFFSET_PTS` if the realized cycle-average drifts from the
  1.67 kW the rules budget. **Watch:** model_params self_consumption must stay ~1.67 while the
  controller is active (see energy_log 2026-07-25) — if build_models ever reports it high, add a
  fill-time clamp so the deadline budget can't under-count charging time. **Not fixed by this:** the
  HA emergency automation still slams 5 kW when it fires → survival-floor reconciliation (#2).
  ⏳ **Not deployed to the Pi yet** — needs `git push` (agent code auto-pulls on the 30-min cron).

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
  | 2026-07-25 | all seven helpers | stable, in band | — | **Clean night.** `max_insurance_floor_pct`=30 (held, was 0 yesterday), `ev_min_soc_pct`=30, `ev_ultra_cheap_c`=10, `ev_standard_price_c`=15, `ev_min_charge_price_c`=40, `ev_charge_target_pct`=100, `ev_departure_target_pct`=95. Zero violations across all 20 overnight cycles. Nothing reset. |
  | 2026-07-26 | all seven helpers | stable, in band | — | **Clean night.** 0 `settings_violations` across the overnight cycles; all seven in-band. `ev_charge_target_pct`=90 (was 100 on 07-25 — in-band, presumed deliberate), `ev_min_soc_pct`=30, `max_insurance_floor_pct`=30, rest steady. Nothing reset. |
  | 2026-07-28 | all seven helpers | stable, in band | — | **Clean night (4th consecutive).** 0 `settings_violations` across 27 overnight cycles (20:00 07-27 → 10:00 07-28). All seven in-band and unchanged: `ev_charge_target_pct`=90, `ev_departure_target_pct`=95, `ev_min_charge_price_c`=40, `ev_min_soc_pct`=30, `ev_standard_price_c`=15, `ev_ultra_cheap_c`=10, `max_insurance_floor_pct`=30. (No 07-27 morning row was logged.) Nothing reset. |

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

- [x] **Reconcile the overnight survival floor — RE-RESOLVED 2026-07-25 (Rule 30 revised).**
  The 07-24 fix (lower `battery_low_soc_emergency_charge` 20% → 10%) was necessary but **not
  sufficient — the oscillation recurred on 2026-07-25**: battery rode to 5% overnight, the automation
  fired at 07:00 (SoC 5 < 10 trigger, its time gate opening), the 07:30 HOLD cleared reserve to 5%,
  churn. Root cause: the rule layer's floor (5%) is still below the automation trigger (10%), and
  lowering the trigger only relocates the sawtooth. **Fix (user chose "agent holds ~12% itself,
  gently"):** `SURVIVAL_FLOOR_DEFENSE` — a HOLD verdict at instantaneous SoC ≤ 12% is overridden to a
  gentle self_consumption top-up (`survival_floor_defend`, target 20%; Rule 31 makes it ~1.6 kW). The
  battery never reaches the floor, so the automation never fires in normal operation (stays a true
  "agent dead" backstop). HA automation unchanged (10% trigger kept as backstop). 5 tests; 216
  decision total. **Not yet deployed** — same `git push` as Rule 31. **Watch:** next low-SoC night,
  SoC should sit ~12–15% (not 5) and the emergency automation should not fire. Original 07-24
  analysis retained below for context.

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
