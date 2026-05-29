# Energy System Control Log

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
