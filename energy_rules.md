# Energy Automation Rules

## Implementation note (as of 2026-06-09)

These rules are implemented by three Python layers in `agent/energy_agent.py`, running every 30 minutes on the Pi:

**Layer A — Deterministic rule layer (IN CONTROL)**: `compute_decision_context()` + `_execute_deterministic_verdict()`. A pure Python `if/elif` rule tree that reads current state and executes all set_* API calls. This is the control path. Kill-switch: `DETERMINISTIC_AUTHORITATIVE = False`.

**Layer B — LLM narrative logger (selective, cosmetic only)**: Claude runs after the deterministic layer and writes a plain-English log entry. Its `set_*` calls are no-ops. System prompt is ~65 lines (slimmed 2026-06-09). **Phase 7 (2026-06-23):** on routine hold cycles (`overnight_hold_wait_for_sponge`, `peak_early_morning_hold`, `demand_window_active`, `peak_target_met`, `peak_solar_will_cover`, `target_met`, `peak_on_track`, `nonpeak_on_track`) where no action was taken and the rule hasn't changed from the previous cycle, the LLM call is skipped entirely — `_build_auto_summary()` writes a one-line `[auto]` entry directly to JSONL and HA notifications. LLM runs on any action, unusual rule, or rule change.

**Layer C — LP optimiser / MPC (shadow, not in control)**: `agent/optimizer.py`, a receding-horizon linear program. Runs every cycle, logs its verdict to `decisions.jsonl` alongside the deterministic verdict for comparison. Not yet in the control path.

Hard constraints (Rule 2 demand window, export guard) remain as HA automations that fire independently of all layers above. See `CONTEXT.md` for current automation status.

---

## Terminology

| Term | Definition |
|------|------------|
| **Minimum Battery Threshold** | 20% — the floor for intentional discharge decisions (arbitrage, normal operation). Distinct from the Powerwall's 5% absolute reserve, which is the floor for *survive-to-cheap-window* decisions. |
| **Target SoC** | The desired battery state of charge at a given time (typically 100% by 3 pm) |
| **Solar Sponge Window** | 10:00 am – 3:00 pm — super off-peak rate, primary charging window |
| **Demand Window** | 3:00 pm – 9:00 pm — zero grid import enforced in peak months |
| **Peak Months** | Nov, Dec, Jan, Feb, Mar, Jun, Jul, Aug — demand charge applies |
| **Off-Peak Months** | Apr, May, Sep, Oct — no demand charge, price-only decisions |
| **Cheap Window** | Current price is below the average of today's 4pm–8pm forecast prices — answers "is now cheaper than tonight's peak?" (to be replaced with Solcast-aware MPC) |
| **Solar Forecast** | Solcast prediction of tomorrow's solar generation in kWh |
| **Cheapest Window** | Lowest-price forecast period identified from Amber forecast data |

---

## System
- **Powerwall 2**: 13.5 kWh usable, ~5 kW charge/discharge rate
- **Solar**: SolarEdge, peak ~5 kW
- **EV**: Polestar 4 (~100 kWh battery), charged via Zappi
- **Tariff**: Amber Electric dynamic pricing, Ausgrid EA116
- **Demand window**: 3–9 pm daily (active months: Nov, Dec, Jan, Feb, Mar, Jun, Jul, Aug)
- **Off-peak months**: Apr, May, Sep, Oct (no demand charge)

---

## Core Rules

### Rule 1 — Battery Full Every Day
- Target: Powerwall at **95%** by **3:00 pm** every day
- Primary source: solar charging during the **Solar Sponge window (10:00 am–3:00 pm)**
- Grid top-up: only what solar can't cover — the system charges exactly enough from grid so that the remaining solar forecast fills the rest

**Grid charge target (`sensor.battery_grid_charge_target`):**

The system doesn't charge to a fixed level (100%, 80% etc). Instead it computes the minimum SoC the battery needs to reach via grid charging, so that Solcast's remaining solar forecast covers the deficit to 95%:

```
target% = clamp(95 − (net_solar_kWh / 13.5 × 100), 5, 95)

where net_solar = max(0, solcast_remaining_today − (home_load_kW × hours_to_3pm))
```

This recalculates live as Solcast updates and the battery charges. If solar will cover everything, target stays at 5% (no grid charging needed). If it's a cloudy day with no solar, target approaches 95%.

| Example | Solcast remaining | Net solar | Grid charge target |
|---------|------------------|-----------|--------------------|
| Sunny day, battery at 30% | 9 kWh | 7 kWh | 43% |
| Cloudy day, battery at 20% | 3 kWh | 2 kWh | 81% |
| Battery at 80%, good solar | 6 kWh | 5 kWh | 58% → already above target, no grid charge |

**Charging phase summary:**

| Phase | Mode | Reserve | Rate | Why |
|-------|------|---------|------|-----|
| Cheap window open + shortfall > 5% | `autonomous` | 100% | ~5 kW | Fast grid charge — reserve=100% prevents export |
| Grid charge target reached | `self_consumption` | 5% | Solar only | Solar covers the rest to 95% |
| Cheap window closes mid-charge | `self_consumption` | 5% | — | Stop paying peak prices |
| Solar sponge ticks (9:45am–2pm, no cheap window) | `self_consumption` | `battery_grid_charge_target`% | ~1.7 kW | Slower but free during solar hours |
| SoC < 20% + cheap price (emergency) | `self_consumption` | `battery_grid_charge_target`% | ~1.7 kW | Safety floor charge |
| SoC hits 95% | `self_consumption` | 5% | — | Done — no further grid draw |

**Two-mode charging strategy:**

- **`autonomous` + `reserve=100%`** — fast ~5 kW grid charge. `reserve=100%` is the critical export guard: it tells the Powerwall to treat all stored energy as backup reserve, preventing it from discharging or exporting while in autonomous mode. Without `reserve=100%`, autonomous mode runs Tesla's TOU optimisation and exports at feed-in prices (4¢) while buying at import prices (11¢+) — a guaranteed loss. Confirmed unsafe on 2026-05-23.
- **`self_consumption`** — used for all other phases: solar sponge ticks, emergency floor charges, and after the cheap window closes. Charges at ~1.7 kW from grid, but sufficient when started early enough.

**Price spread governs mode selection:**

The value of charging hard (autonomous) depends on the spread between the cheap window price and the next expensive period the battery would otherwise have to cover.

| Spread (cheap_now vs next_peak) | Mode decision |
|---------------------------------|---------------|
| < 5¢ | Don't charge from grid — hold and wait for a better window |
| 5–8¢ | `self_consumption` only; only if window ≥ 3h and SoC gap is meaningful |
| 8–15¢ | `self_consumption` for long windows; `autonomous` if window < 2h AND need > 15% SoC |
| > 15¢ | `autonomous` justified — real arbitrage, charge at full 5 kW rate |

**Peak month demand window overrides spread logic:** if the battery won't reach 85% SoC by 2:55pm and solar + self_consumption won't get there in time, use `autonomous` regardless of spread. The demand charge (~$100/month) outweighs any charging cost differential.

**Price protection:**
The cheap window condition (`amber_in_cheap_window`) gates all grid charging. At high spot prices (e.g. $2/kWh) the condition is False — autonomous mode reverts to `self_consumption` immediately via `battery_autonomous_revert_cheap_ended`. The grid charge target sensor continues calculating but nothing acts on it until price returns to a cheap window.

**Cheap Window definition:** `current price < average of today's 4pm–8pm forecast prices`

| Scenario | Current | Avg 4–8pm | Charge? |
|----------|---------|-----------|---------|
| Good solar day | 8¢ | 12¢ | ✅ Yes — cheaper than tonight |
| Expensive morning, 70¢ peak coming | 34¢ | 52¢ | ✅ Yes — still cheaper than tonight |
| Midday dip after cheap morning | 10¢ | 8¢ | ❌ No — evening is cheaper |
| Flat pricing all day | 20¢ | 20¢ | ❌ No — no advantage to charging now |

**Charge trigger logic:**

| Month type | Months | Charge condition |
|------------|--------|-----------------|
| Peak | Nov, Dec, Jan, Feb, Mar, Jun, Jul, Aug | Always charge if SoC < 95% — demand window risk overrides price |
| Off-peak | Apr, May, Sep, Oct | Cheap window open AND price < 15¢ |

**Price gate — off-peak months:**
`amber_in_cheap_window` alone is insufficient — it only asks "is now cheaper than tonight's 4–8pm average?" and is True at 18¢ even when 13¢ is available at 10am. Off-peak charging requires both:
- `amber_in_cheap_window = True` (cheaper than tonight's peak)
- `price < 15¢` before 10am (absolute threshold — wait for genuinely cheap slot)
- After 10am: `amber_in_cheap_window` alone (deadline pressure — must charge by 3pm)

Peak months always charge regardless of price — demand window risk overrides economics.

**Flat-then-spike days:**
If the price forecast shows no window more than 3¢ cheaper than the current price before an evening spike, treat current price as the charge window — don't hold for a cheaper window that isn't coming. Check: if `min(forecast prices before spike) ≥ current price − 3¢`, charge now rather than waiting. This commonly occurs on overcast days where Solar Sponge prices are similar to morning prices (~20¢ all day) before a 3pm+ spike.

**Deadline-aware charging with adaptive escalation:**
The agent re-runs this calculation every 30-min cycle. It starts slow (cheap) and escalates to fast only when the maths demands it.

1. `kWh_needed = (target_soc − current_soc) / 100 × 13.5`
2. `hours_to_fill_slow = kWh_needed / 1.7` (self_consumption)
   `hours_to_fill_fast = kWh_needed / 5.0` (autonomous)
3. `hours_to_cheap_end` — scan the price forecast forward for the first interval where:
   `price ≥ current_price + 4¢` AND `the following interval is also ≥ current_price + 4¢` (sustained rise, not a blip).
   `hours_to_cheap_end` = hours from now until that first elevated interval.
   - Non-peak months: use `hours_to_cheap_end` as the deadline
   - Peak months: use `min(hours_to_2:55pm, hours_to_cheap_end)` — demand window is hard
   - If no sustained rise found in forecast: use 6h (end of forecast window)
4. Mode decisions:
   - `hours_to_cheap_end > hours_to_fill_slow + 1.5h` → spread logic applies, window still viable
   - `hours_to_cheap_end ≤ hours_to_fill_slow + 1.5h` → start self_consumption now, no time to wait
   - `hours_to_cheap_end ≤ hours_to_fill_fast + 0.5h` → escalate to autonomous immediately

**Escalation:** once self_consumption charging has started, the agent rechecks each cycle. If `hours_to_cheap_end ≤ hours_to_fill_slow + 0.5h`, it switches to autonomous. Start slow and cheap; escalate automatically when the deadline demands it.

*Example (non-peak): 36% SoC, 80% target, now 10:30am, price 15¢. Forecast: 15¢ until 4pm then rises to 19¢+. hours_to_cheap_end = 5.5h. kWh needed = 5.9, slow needs 3.5h, fast needs 1.2h. 5.5h > 3.5 + 1.5 = 5.0h → spread logic applies; 5¢ spread → self_consumption, monitor. At 2:30pm, battery at 55%: kWh_needed = 3.4, slow needs 2.0h, hours_to_cheap_end = 1.5h → 1.5 ≤ 2.0 + 0.5 → escalate to autonomous.*

**Charge rate model (rebuilt from instantaneous power, 2026-07-22):** `_avg_charge_rate_kw(soc_from, soc_to, mode)` segments the SoC range into 10% buckets and computes a weighted average from `agent/model_params.json`. Rates are now measured from `sensor.tesla_powerwall_2_battery_power` at ~30 s resolution over 10 days (n=53–432 per bucket), not from 30-minute SoC deltas.

| mode | rate |
|------|------|
| `self_consumption` | **1.67 kW**, flat across 0–70% SoC (p10≈p25≈median — a very tight distribution). 80%/90% retain the older, slower 0.96/0.71 kW as a conservative fallback |
| `autonomous` | **5.0 kW to 70%**, then tapering: **2.92 kW at 80%, 1.84 kW at 90%** |

**Why the method changed:** the previous model measured *SoC gained per 30-minute cycle*, which conflates the charge rate with how long charging actually ran, and it gated on `sensor.powerwall_backup_reserve` — a Tessie-polled sensor with ~2 minutes of lag (observed reporting 5% while the true value was 80%). Both flaws biased the result.

**Why the autonomous taper matters:** it previously had n=2–5, below `MIN_SAMPLES`, so the agent assumed a flat 5.0 kW across the whole range. It was therefore *optimistic* about fill time in exactly the 80–100% band where the 2:55pm deadline is decided. Fill time for 80→95% went from 0.41 h to 0.83 h. Being optimistic there risks a demand charge (~$100/month); being pessimistic costs cents — so the model deliberately errs slow, and buckets with fewer than 20 samples keep their older, slower values.

> **Regime change (2026-07-22, confirmed persistent 07-23).** Raising `backup_reserve_percent`
> above SoC now pulls a sustained **5 kW** in `self_consumption` — 92%/96% of samples > 3 kW on
> 07-22/07-23 vs 0–4% on 07-13→07-21, a clean step change (leading hypothesis: a firmware push,
> unverifiable). Below 70% SoC `self_consumption` and `autonomous` are now indistinguishable.
> **While the model still reports 1.67 kW, every fill-time projection is ~3× too pessimistic and
> the agent starts charging earlier/dearer than it needs to** — the direct cause of the 2026-07-24
> 08:00 over-charge. Error is in the *safe* (cheap) direction, not the demand-charge direction.

**Asymmetric charge-rate window (2026-07-24).** The headline rate a bucket exposes as `kw` is now
`min(median over 10 days, median over the last 2 days)` — computed by the pure
`_aggregate_charge_rates()` in `build_models.py` (unit-tested in `test_build_models.py`). This makes
the model **fall to a slower rate within ~1 day** (safe: budget more charge time) but **rise to a
faster rate only on sustained evidence** (a spurious fast day must not make the deadline maths
optimistic). Mechanism: on a *rise* the 2-day median jumps first while the 10-day median lags, so
`min()` holds the pessimistic long value until the long window is itself majority new-regime
(≈ the same timing as a plain 10-day median — no upside is delayed); on a *fall* the 2-day median
drops first, so `min()` follows it down. The rationale is the asymmetric cost: believing 5 kW when
it is really 1.67 risks a ~$30 demand charge; believing 1.67 when it is really 5 costs cents. A low
quantile does **not** substitute — within a regime p25≈p50, so quantiles hedge spread, not a regime
change. Run against the 07-24 data this keeps `self_consumption` at 1.67 (2 new days can't outvote
8), which is the intended output; the value is a *safe* flip to 5 kW once the regime is sustained
(~2026-07-27), with fall-fast protection if it ever reverts. `kw_long`/`kw_short` are recorded
alongside `kw` for transparency.

**Why `hours_to_cheap_end` instead of `hours_to_spike`:**
The old logic found the first interval exceeding 30¢ — which means it found nothing on mild-spike days (e.g. prices going 15¢ → 19¢) and gave incorrect 6h default deadlines. `hours_to_cheap_end` finds the first *sustained* price rise of ≥ 4¢, which correctly identifies when "cheap now" ends regardless of the absolute price level.

> **Note (2026-05-29) — two definitions in play during shadow mode.** The prose above is the LLM-facing definition (the +4¢ sustained-rise test, still in the system prompt). The *deterministic shadow layer* (`compute_decision_context`) now uses an improved **scale-free daily-shape** definition: cheap-end = the right edge of the bottom-`α` band of the day's own swing (`α=0.30` of trough→evening-peak range), with a 5¢ minimum-swing guard so flat days register no closing window. The +4¢ test under-reported urgency on a gradual ramp off a low base (caught live 15:30 2026-05-29). When the deterministic layer is cut over (Phase 5), this prose and the system prompt should be unified onto the scale-free model. See `agent/energy_agent.py:_hours_to_cheap_end` and the 2026-05-29 log entry.

**Timing:**
- Checked at **9:30 am** (initial trigger)
- Re-checked **every 30 minutes from 9:45am to 2pm**, AND **immediately when `amber_in_cheap_window` flips to True** — charging starts the moment the cheap window opens
- Stops charging the moment `amber_in_cheap_window` flips to False
- Target reserve updates each 30-min tick as Solcast revises and SoC rises — naturally lowers the grid charge target as solar arrives
- Hard reset at **2:55 pm** — clears any active charge target before demand window

**Inverter underperformance override:**
- If Solcast `power_now > 500W` but inverter shows `< 200W`, treat as a solar deficit regardless of remaining-today forecast — charge from grid rather than waiting on solar that isn't arriving

**Low SoC survival check (before cheap window opens):**
- Before deciding to top up, compute: `projected_soc = current_soc − (hours_to_next_cheap_window × home_load_kw / 13.5 × 100)`
- If `projected_soc > 5%`: no action — battery will survive to the cheap window above the Powerwall floor; charge cheap then
- If `projected_soc ≤ 5%`: set reserve to just enough to survive — `drain_to_window + 8%` — not a top-up to 20%
- Example: SoC=18%, 2.25h to Solar Sponge, 0.6kW load → projected = 18 − 10 = 8% → above 5%, no action
- Example: SoC=6%, 2.25h to Solar Sponge, 0.6kW load → projected = 6 − 10 = −4% → set reserve=18% (10+8)
- Respects demand window: never fires during 3–9pm in peak months

- On cloudy days (see Rule 9): Solcast forecast triggers grid top-up early rather than waiting for the price check

### Rule 2 — Never Trigger Network Demand Charge
- During **peak months** (Nov–Mar, Jun–Aug): **zero grid import** between 3–9 pm, every single day
- This is not a time-of-use price issue — it is a **peak demand ratchet**: Ausgrid measures your single highest **30-minute** average import interval during 3–9 pm across the entire month and charges a $/kW network fee on that peak for the whole month's bill. One bad 30-minute window can effectively **double the entire monthly bill**.
- The demand baseline resets on the **1st of each month** — be especially cautious at the start of each peak month
- The battery must cover 100% of home load during this window with zero grid import
- If battery SoC is dangerously low during the demand window, shed non-essential loads rather than import from grid
- Rule 2 overrides everything — no exception, no matter the price or circumstance
- During **off-peak months** (Apr, May, Sep, Oct): demand charge does not apply; revert to price-only decisions
- **Agent pre-flight guard (added 2026-06-02):** at the start of every `run_agent()` cycle, before the LLM runs, the agent checks: if peak month AND 15:00–21:00 AND `reserve > 10%`, it immediately calls Tessie API directly to set reserve=5%. This is a hard override that bypasses HA rest_commands entirely — it fires even if HA has failed to load the rest_command integration (the June 2 failure mode). Logged to stderr as a warning when it fires.
- **LLM reserve block during demand window (added 2026-06-06):** `_guarded_set_reserve()` in `TOOL_MAP` intercepts any `set_powerwall_reserve(N > 10)` call made during 3–9pm peak months and returns an error string without calling the Tessie API. Prevents the failure mode where the LLM confuses "battery needs to reach 85% SoC by 2:55pm" (a pre-charge target) with "battery must stay above 85% during the demand window" (wrong — during the window, reserve must be 5% so battery can discharge freely). The pre-flight guard drops reserve to 5% at cycle start; this block prevents the LLM overriding it back up within the same cycle.
- **HA rest_command health check (added 2026-06-02):** each cycle also checks `/api/services` for the `rest_command` domain and warns loudly if it's missing — surfaces HA config failures immediately rather than silently for 36h.
- **Verification (added 2026-06-02):** `agent/log_daily_energy.py` records each peak day's outcome to `agent/daily_energy.jsonl`. The pass/fail metric matches the bill exactly — the **peak clock-aligned 30-minute average import (kW)** over 3–9pm, *not* kWh or instantaneous peak. A day passes if that stays below 0.10 kW (sub-30-min transients the battery covers don't count). The monthly bill is driven by the single worst day's peak, so `sensor.demand_window_monitor` (pushed via REST API, no HA config change) surfaces this month's running max + a rolling per-day history for the dashboard cards.

### Rule 3 — Avoid Negative Feed-in Tariff (FIT) and Export Penalty
- If Amber export price goes **below $0/kWh**: stop exporting to grid
- Divert excess solar to battery first, then EV (if plugged in), then curtail
- Check: `sensor.1a_wigram_road_glebe_feed_in_forecast` for upcoming negative FIT windows
- Ideally charge battery/EV *before* a negative FIT window begins to maximise absorption capacity
- **Export penalty**: EA116 applies an infrastructure export charge if solar export during 10am–3pm consistently exceeds an approved threshold — absorb excess solar on-site (battery, EV) rather than exporting during this window

### Rule 4 — EV Charging by Price Thresholds

**Core policy: EV never charges from the Powerwall battery.**
Use `Eco+` as the default (charges only from actual solar export past the meter). Override to `Eco` or `Fast` only when the grid price is below user-set thresholds and we are outside the demand window.

**Three user-set price thresholds (HA sliders):**
- `ev_ultra_cheap_threshold_c` — price at or below this → **Fast** (charge at full speed)
- `ev_standard_price_c` — price at or below this → **Eco** (charge slowly from grid+solar)
- `ev_min_charge_price_c` — ceiling for the below-minimum emergency charge; above this price even an EV below min SoC stays on Eco+

> **The values live in the HA console, not here.** They are deliberately not restated in this
> document — a duplicated number goes stale silently. To read what is actually in force, see
> `settings_used` in the latest `agent/decisions.jsonl` record, which logs the values the agent
> decided with, every cycle. Rule 28 covers how they are validated.

**Priority order (first matching condition wins):**

| Priority | Condition | Zappi mode | Notes |
|----------|-----------|-----------|-------|
| 1 | Demand window (3–9pm peak months) | `Eco+` | No grid draw ever during demand window — Rule 2 absolute |
| 2 | EV SoC < min AND price < ev_min_charge_price_c | `Fast` | Critical EV level — override price gate (ceiling is a user-set slider; read the live value from the console) |
| 3 | FIT < 0¢ AND battery ≥ 85% AND EV < 100% | `Eco+` | Absorb solar surplus into EV rather than paying to export |
| 4 | EV SoC ≥ target | `Eco+` | Target met — solar surplus only |
| 5 | Price < ultra_cheap_c | `Fast` | Exceptional price — charge hard |
| 6 | Price < standard_price_c | `Eco` | Acceptable price — charge slowly |
| 7 | Default | `Eco+` | Price too high — solar surplus only |

**Zappi modes:**
- **Fast**: full rated speed from grid; solar reduces grid draw but charge is grid-led
- **Eco**: charges from grid+solar up to in-home surplus rate; slower than Fast
- **Eco+**: charges only from actual grid export (power physically exporting past the meter — genuine solar surplus). Battery discharge does not count as export, so EV never draws battery power.

- Respect Rule 2: no EV grid charging during demand window (3–9 pm) in peak months
- Re-evaluates every 30 min (agent cycle); slider changes take effect at next cycle
- *Future: allow user to set departure time for deadline-based charging*

### Rule 5 — Dump Excess Solar into EV
- When solar generation exceeds home consumption + battery charge rate: divert surplus to Zappi
- Zappi **Eco+ mode** handles this — charges only from actual grid export (power flowing to the street), so it captures genuine solar surplus without drawing from the battery
- Priority order for excess solar: **Battery → EV → Grid export**
- Do not export to grid if EV has capacity and is plugged in
- **Eco+ is the default** whenever the EV is plugged in — ensures solar surplus is absorbed without battery being discharged for EV

**Zappi mode reference:**

| Mode | Source | Behaviour |
|------|--------|-----------|
| **Fast** | Grid (+ solar reduces import) | Charges at full rated speed; solar reduces grid draw but charge is grid-led |
| **Eco+** | Grid export (genuine solar surplus) | Charges only when house is exporting to grid. Battery discharge does not trigger EV charging. |
| **Eco** | In-home surplus | Throttles to match any in-home surplus including battery discharge — **not used** (battery-to-EV risk) |
| **Stop** | None | No charging |

---

### Rule 6 — Minimum Battery Reserve
- **Normal floor: 5%** — the agent sets reserve to 5% as the standard operating floor, allowing full discharge in off-peak months
- **Demand window floor (peak months): 5%** — reserve dropped to 5% at 2:55pm so battery discharges as deeply as possible to cover home load 3–9pm
- **Reserve is restored at 9pm** after the demand window ends (via `battery_post_demand_window_restore`)
- The reserve level is the only writable control parameter available via the Tessie API — it is used as a charge/discharge signal, not purely as a backup reserve
- Note: the Powerwall gateway sensor (`sensor.tesla_powerwall_2_charge`) floors its reading at the reserve level. Always use `sensor.tessie_powerwall_charge` for true SoC.
- **Critical — never judge "target met" off the gateway.** Whenever reserve > true SoC (i.e. actively grid-charging), the gateway reads *upward* to the reserve level and lies. Declaring a charge target — especially the 85% demand-window target — "achieved" off the gateway and then dropping reserve is a **Rule 2 violation trap**: the battery enters the 3–9pm window under-filled and imports from grid. Always confirm target attainment against the Tessie reading. *(Found 2026-05-29 by the peak-month backtest; SoC-sensor guidance added to the agent system prompt the same day.)*

### Rule 7 — Opportunistic Overnight Grid Charging (Seasonal)
- Do not assume overnight charging is needed or useful — evaluate based on season, solar forecast, and price comparison

**Step 1 — Safety net: survive to Solar Sponge**
- Compute: `projected_soc_at_sponge = current_soc − (hours_to_sponge × home_load_kw / 13.5 × 100)`
- If `projected_soc > 5%`: no action — battery will survive to Solar Sponge above the Powerwall floor; charge cheaply then
- If `projected_soc ≤ 5%`: top up to just enough to survive — set reserve to `drain_to_sponge + 8%` with a **Price Ceiling of 25¢**
- This is a minimal survival top-up only — not a top-up to 20%, not a full charge
- Reserve cleared back to 5% at **6am** (not 8am) — allows 3.5 extra hours of free discharge before the cheap window opens, arriving at the Solar Sponge window with less stored energy and therefore more capacity to absorb cheap grid or solar charging

**Step 2 — Additional charging: only if it beats the morning rate**
- Before charging beyond 20% overnight, compare overnight price with forecast Solar Sponge price
- If Solar Sponge forecast (10am–3pm) is cheaper than overnight: **wait** — charge during Solar Sponge instead
- Example: do not charge at 12¢ overnight if Solar Sponge is forecast at 6¢
- Only charge overnight if overnight price is genuinely the cheapest upcoming window

**Step 3 — Seasonal logic**

| Season | Solar expectation | Overnight charging beyond safety net |
|--------|------------------|--------------------------------------|
| Summer (Nov–Mar) | High — Solar Sponge will likely fill battery | Skip — Solar Sponge rate will be cheaper |
| Winter (Jun–Aug) | Low — solar may not fill battery alone | Charge to 80% if overnight price < Solar Sponge forecast |
| Autumn/Spring (Apr, May, Sep, Oct) | Moderate — weather dependent | Charge only if overnight rate beats Solar Sponge forecast |

- Cap overnight grid charging at **80%** — leave the final 20% for solar during Solar Sponge
- Decision inputs required: Amber overnight forecast, Amber Solar Sponge forecast, Solcast solar forecast, weather forecast
- *Depends on: Solcast integration and weather forecasting integration (both on to-do list)*

### Rule 8 — Negative Spot Price: Maximise Consumption
- If Amber spot price goes **negative** (grid paying you to consume): charge everything possible
- Charge Powerwall to 100%
- Charge EV to 100% if plugged in
- This is free or subsidised electricity — overrides all other rules except Rule 2 (demand window)
- *Future enhancement: trigger high-draw appliances (AC, heat pump, dishwasher) when spot goes negative — requires smart appliance integration (e.g. Sensibo for AC, smart plugs for others)*

### Rule 9 — Cloudy Day Detection
- If solar forecast for the day is below **10 kWh**: activate "cloudy day" mode
- Cloudy day: grid top-up to 80% during cheapest morning window (before 9 am), then rely on whatever solar arrives
- Prevents arriving at 3 pm with a half-full battery because solar underdelivered


### Rule 10 — Price Spike Arbitrage (deprioritised — not implemented)

**Decision (2026-06-10)**: Not worth building as a manual rule. The economics are unfavourable in practice:

- **Demand window conflict**: in peak months (Jun–Aug, Nov–Mar), price spikes almost always occur during the 3–9pm demand window. Discharging to sell during the demand window risks a demand charge breach — a ~$100/month penalty that dwarfs any FIT revenue.
- **Outside demand window**: spikes outside 3–9pm or in off-peak months are rare on the EA116 tariff.
- **Gentle arbitrage excluded**: buying at 10¢ and selling at moderate peaks (18¢) generates insufficient margin to justify battery cycle degradation cost (~$15,000 replacement).

**Revisit only**: if/when the LP optimiser becomes authoritative — it can model the trade-off explicitly (demand penalty vs FIT revenue) and identify the narrow windows where spike discharge is genuinely profitable. Not worth a hand-coded rule.


### Rule 11 — Solcast vs Inverter Cross-Check
- During solar hours (9am–5pm), compare Solcast's hourly forecast against the inverter's actual output every 30 minutes
- The agent computes a `forecast_accuracy` label from three Solcast sensors and the SolarEdge inverter:

| Sensor | Unit | Use |
|--------|------|-----|
| `sensor.solcast_pv_forecast_forecast_this_hour` | Wh (÷1000 = kWh ≈ avg kW) | Primary accuracy reference — hourly aggregate is more stable than instantaneous |
| `sensor.solcast_pv_forecast_forecast_next_hour` | Wh (÷1000 = kWh) | Forward-looking — tells agent whether solar is improving or staying low |
| `sensor.solcast_pv_forecast_power_now` | W (÷1000 = kW) | Secondary instantaneous reference |
| `sensor.solaredge_current_power` | W (÷1000 = kW) | Actual inverter output |

**Accuracy is measured against the *bias-corrected* forecast, not raw Solcast (2026-07-24).**
Solcast over-forecasts this flat roof by ~7× at 08:00 and ~6× at 09:00 in winter, so raw
`actual/forecast` is ~14% on a *perfectly normal* morning — which read `unreliable` and zeroed
the whole day's solar credit (`expected_solar = 0`), forcing needless grid charging (the
2026-07-24 08:00 case). `_solar_accuracy()` now weights the this-hour forecast by that hour's
measured ratio (`model_params.json["solar_correction"]`, via `_hour_solar_ratio()`) before
comparing, so the ratio below asks the real question: *is solar underperforming its calibrated
expectation, or just its raw one?* Falls back to raw Solcast when the hour is uncalibrated
(n < min_samples) or Solcast attributes are missing — degrades to the old behaviour, never to a
wrong answer.

**Accuracy labels** (ratio = actual ÷ corrected this-hour forecast; ÷ raw only on fallback):
- `good` (actual ≥ 70% of the corrected expectation): use `forecast_remaining_today` as-is
- `poor` (actual 30–70%): treat `remaining_today` as ~50% of stated value
- `unreliable` (actual < 30%): ignore `remaining_today` entirely; treat as zero-solar day
- `not_applicable`: raw forecast < 0.2 kWh (night), **or** the corrected expectation is < 0.1 kW
  (deep-morning bias — nothing to be "unreliable" about, so don't condemn the day off noise)

**Solar start hour (2026-05-31):** Zero-solar detection and accuracy-based `solar_unreliable` only activate from **9am** onwards (was 8am). Before 9am, panels on a flat roof in Sydney don't produce meaningfully regardless of cloud — zero output is expected and must not be counted as evidence of a zero-solar day. The `SOLAR_START_HOUR = 9` constant controls this.

**Agent response to unreliable or poor forecast:**
The `battery_grid_charge_target` sensor is computed from Solcast's remaining forecast. On a cloudy day it will be optimistically low (e.g. "60% is enough — solar will cover the rest") even when actual solar is delivering almost nothing. When `forecast_accuracy` is `poor` or `unreliable`, the agent ignores `grid_target_pct` entirely and substitutes a time-based charge target:

| Time of day  | Substitute charge target |
|--------------|--------------------------|
| Before 12pm  | 85% — full day of load ahead, assume solar contributes little |
| 12pm–2pm     | 70% — half day left |
| After 2pm    | 50% — mostly evening load to cover |

In peak months, if `forecast_accuracy = unreliable` at 10am, begin grid charging immediately toward the 85% SoC target regardless of what `remaining_today` says. Don't wait for solar that isn't arriving.

### Rule 12 — Weather Forecast Cross-Check (Open-Meteo)

The agent calls `get_weather_forecast()` to supplement Solcast with independent weather data. This solves two problems Solcast alone can't handle:

**1. Overnight pre-charging decisions (peak months):**
At overnight cycles (10pm–6am), the agent checks tomorrow's solar outlook via Open-Meteo `radiation_wm2` (W/m²):

| Radiation (8am–3pm avg) | Outlook label | Agent action (peak month) |
|-------------------------|---------------|--------------------------|
| > 300 W/m² | good | Trust solar — no pre-charging needed |
| 150–300 W/m² | poor | Consider partial pre-charge to 70%+ tonight |
| < 150 W/m² | overcast | Pre-charge to 80–90% tonight; treat tomorrow as zero-solar |

In non-peak months this override doesn't apply — let economics decide.

**2. Daytime temporary-vs-all-day cloud disambiguation:**
If Solcast shows `unreliable` accuracy but Open-Meteo `radiation_wm2` for the next 2–3 hours is > 250 W/m², the cloud is likely passing — wait 30 min before charging from grid. If radiation is also < 150 W/m², it is a genuine all-day cloudy day — act on it immediately.

**Radiation thresholds for this site (6.12 kWp flat roof, Glebe):**
- > 300 W/m²: good (likely > 1.5 kW panel output)
- 150–300 W/m²: poor (0.5–1.5 kW)
- < 150 W/m²: overcast (< 0.5 kW, effectively no solar contribution)

Tomorrow's `solar_outlook` and `avg_radiation` are captured in `decisions.jsonl` for analyst use.

Also uses `forecast_next_hour` for timing: if next hour is forecast significantly higher, solar may be improving — wait 30 min before committing to grid charge. If next hour is also low, don't wait.


---

### Rule 13 — Time-Based Escalation: "Enough Waiting, Go Hard"

Overrides the spread table and deferral logic when time pressure is real. Not a suggestion — a hard override.

**Deferral limit (non-peak and peak months):**
The agent receives the last 3 decisions as context at the start of each cycle. If 2+ consecutive cycles show a "hold" decision while waiting for a cheaper price window — and the current price is within 2¢ of where it was in those cycles — the forecast is wrong. The cheap window is not arriving.

Action: stop deferring. Apply flat-then-spike logic immediately. Treat current price as the charge floor. Start self_consumption toward the time-based substitute target.

Recognising this pattern is as important as recognising a genuine cheap window. Each 30-minute hold costs charging time, not money.

**Peak months — hard deadline (85% SoC by 2:55pm):**

Every cycle from 9am, the agent calculates:
```
net_solar = max(expected_solar_remaining − home_load_kw × hours_to_2:55pm, 0)
            (use 0 for solar if forecast is poor or unreliable)
kWh_needed = max((0.85 − soc/100) × 13.5 − net_solar, 0)
hours_to_fill_fast = kWh_needed / avg_charge_rate(soc→85%, autonomous)
hours_to_fill_slow = kWh_needed / avg_charge_rate(soc→85%, self_consumption)
hours_remaining    = hours until 14:55
```

**Home load deduction is critical**: raw Solcast `remaining_today` is gross solar generation. Home loads consume solar first; only the surplus charges the battery. Failing to deduct home load makes the battery appear "covered" on a sunny day at 25% SoC (e.g. 10 kWh remaining − 8 kWh home load = 2 kWh net; kwh_needed = 7.6, not 0).

If `kWh_needed ≤ 0`: fire `peak_solar_will_cover` (hold — net solar covers the gap). Note: this is distinct from `peak_target_met` (SoC actually at 85%). The solar projection may be wrong — escalation rules below will fire if solar underdelivers as the day progresses.

| Condition | Action |
|-----------|--------|
| `hours_to_fill_fast ≥ hours_remaining` | Autonomous NOW — already very tight |
| `hours_to_fill_slow ≥ hours_remaining` | Autonomous NOW — self_consumption too slow |
| `hours_to_fill_slow ≥ hours_remaining − 1.0h` | Self_consumption NOW — stop deferring |

Price spread is irrelevant. Demand charge ≈ $100/month ($3.30/day). Paying 5¢/kWh extra on 10 kWh costs 50¢. Always charge — the maths is obvious.

**Implementation note (fixed 2026-06-24):** when `fill_slow ≥ hours_remaining` (self_consumption too slow), the code previously checked `price ≤ forward_min` and used `self_consumption` if prices were flat — but self_consumption physically cannot reach 85% in the available time regardless of price. Fixed: when in deadline urgency, always go `autonomous`. Rule `peak_deadline_autonomous`.

Quick checks:
- Past 12:30pm + battery < 40% + peak month → autonomous immediately
- Past 1:30pm + battery < 70% + peak month → autonomous immediately

**Non-peak months — soft deadline (avoid evening spike):**

Use `hours_to_cheap_end` (from the deadline calculation above) as the deadline — it automatically adapts to when prices actually start rising today, whether that's 3pm, 4pm, or later:
- `hours_to_fill_slow ≥ hours_to_cheap_end − 0.5h` → start self_consumption NOW, spread irrelevant
- `hours_to_fill_fast ≥ hours_to_cheap_end − 0.5h` → escalate to autonomous NOW

Additional override after noon: if battery < 30% AND price has been flat (within 3¢ of the expected cheap window) across 2+ recent cycles → charge at current price. The cheap window is not coming. Current price IS the floor.


### Rule 14 — Solar Sponge Minimum Floor

EA116's Solar Sponge window (10am–3pm) is structurally cheaper than evening prices on every day, regardless of spot price movement. This is a tariff design feature — not a forecast. The spread table does not apply to this floor.

**Rule: during 10am–1pm, if SoC < 50%, always charge to at least 50%.**

This is a floor, not a ceiling:
- If the demand window target (85%) or grid charge target is higher, use that instead
- If SoC is already ≥ 50%, normal spread logic applies for charging further
- If it's past 1pm and SoC < 50%, apply Rule 13 escalation logic instead

**Why 50%:** enough to cover ~3.5 kWh of evening home load (reasonable buffer without over-committing). Getting to 50% during Solar Sponge is always better than paying evening rates for the same kWh.

**Why 10am–1pm:** leaves 2h of Solar Sponge remaining after the target is reached, so actual solar arriving late still has headroom to charge. After 1pm the deadline-aware escalation rules take over.

**Mode:** self_consumption is sufficient (2–4h available, no urgency requiring autonomous).

*Implementation rationale:* this rule prevents the agent from deferring indefinitely on a cheap-window forecast that may not arrive. The Solar Sponge window is the reliable cheap window — it's always there. Any other cheaper dip is a bonus, not a guarantee.

### Rule 15 — Historical Price Model for Grid Charge Target (non-peak)

Replaces fixed thresholds with a self-calibrating model based on rolling 7-day price history from `decisions.jsonl`.

- `p25` = 25th percentile of recent prices (cheap anchor); `p75` = 75th percentile (normal/expensive anchor)
- `price_position = (P_now − p25) / (p75 − p25)` — 0.0 = cheapest by recent standards, 1.0 = normal
- **Solar trust**: `solar_trusted = forecast × confidence × price_position` — when prices are cheap (position→0), discount solar (cost of over-charging is low, be aggressive from grid)
- **Insurance floor**: `floor = max_insurance_floor × (1 − price_position)` — hold a minimum SoC proportional to how cheap prices are, guarding against the cheap window closing early
- `cost_target = max(solar_adjusted_target, insurance_floor)`
- Falls back to legacy (time-based substitute) when: `HISTORICAL_PRICE_MODEL = False`, insufficient history (< 48 records), price history flat (swing < 2¢), or peak month
- Rollback: set `HISTORICAL_PRICE_MODEL = False` in `energy_agent.py`
- User-settable: `input_number.battery_max_insurance_floor_pct` — live value in the HA console

**Why**: a cheap-window that closes 1.5h early (as observed 2026-05-31) causes under-charging when relying on solar forecast alone. The insurance floor ensures a meaningful minimum SoC is locked in while prices are cheap, independent of the solar forecast.

**Scope, stated plainly (added 2026-07-23).** "non-peak" in the heading has a consequence
worth spelling out: this rule — and therefore the whole `battery_max_insurance_floor_pct`
control — is **dormant for eight months of the year**. Active only **Apr, May, Sep, Oct**;
inactive Nov–Mar and Jun–Aug.

That is correct, not an oversight. In peak months Rule 13's deadline logic drives the battery
to 85% by 2:55pm regardless of price — a far higher floor than any insurance value would set,
so the floor could never bind. The demand deadline subsumes it.

Two further properties the control's name does not convey:
- **It is a *maximum*, not a fixed buffer.** The floor actually applied is
  `value × (1 − price_position)` — full strength at or below the 7-day p25, sliding linearly
  to zero at p75. At a setting of 30 with p25=14¢/p75=18¢: 30% at ≤14¢, 22.5% at 15¢, 0% at ≥18¢.
- **It can only raise the target, never discharge** — the result is clamped to `max(soc, …)`.

The question it answers exists *only* when there is no demand charge: "power is cheap now — do
I top up, or trust the solar forecast to cover the day?" Given Solcast over-forecasts this site
by ~2× in winter (Rule 29), the case for carrying some insurance is stronger here than the raw
forecast would suggest.

Suggested HA label, since "Max insurance floor" conveys none of the above:
**"Cheap-price insurance (Apr/May/Sep/Oct)"** — description: *"Minimum battery % to lock in
while power is cheap, instead of trusting the solar forecast. Scales down as price rises."*


### Rule 16 — Solar-Unreliable Autonomous Escalation (non-peak)

When `solar_unreliable = True`, self_consumption at 1.7 kW cannot be supplemented by uncertain solar. Apply a tighter autonomous escalation buffer:

- Normal (solar reliable): autonomous if `fill_slow ≥ hours_to_deadline − 0.5h`
- Solar unreliable: autonomous if `fill_slow ≥ hours_to_deadline − 1.5h`

The 1h extra buffer ensures the battery fills from grid before the cheap window closes, rather than relying on solar that may not arrive.

### Rule 17 — Sliding Forecast Detection

If the Amber cheap window has been forecast as "1–2h away" for 3+ consecutive cycles but the actual price never dropped to that level, the forecast is sliding — it's not a genuine upcoming cheap window, it's a phantom. Action: treat as `deferral_limit` and charge now at current price.

Detection: `_detect_sliding_forecast()` fires when, for the last 3+ records, `forward_min < price − 2¢` in each record but the actual recorded price never reached `forward_min + 2¢`. JSONL `rule_fired = "sliding_forecast"`.

### Rule 18 — EV Eco/Fast/Eco+ Progression

Three-phase EV charging strategy based on price position:

1. **Eco** (trickle from grid+solar): in cheap window, but a meaningfully cheaper price is still forecast (forward_min > eco_gap_c below current price) AND EV above minimum SoC → wait for the cheapest moment
2. **Fast** (full grid rate): in cheap window and this IS the cheapest upcoming price → charge hard now
3. **Eco+** (solar overflow only): target met → absorb any remaining solar export for free

Threshold user-settable: `input_number.ev_ultra_cheap_threshold_c` (Case 2) — live value in the HA console.
(`ev_eco_gap_c` was documented here but **does not exist** in the code or HA config — removed 2026-07-23.)

### Rule 19 — Negative FIT Solar Dump (EV Case 6)

When FIT price < 0¢ AND battery SoC ≥ 85% AND EV SoC < 100%:
- Switch EV to **Eco+** (not Fast) — absorbs surplus solar that would otherwise be exported at negative price
- Eco+ draws only from actual solar export, so no grid import occurs — the goal is to avoid paying to export, not to buy grid power
- Battery threshold 85% ensures this only activates when the battery is genuinely near full
- Overrides the user-set EV charge target — treats 100% as the effective target while FIT is negative


### Rule 20 — Overnight Hold: Wait for Solar Sponge

Solar Sponge (10am–3pm) is a structural tariff feature — always cheaper than evening/overnight prices. The demand window is only 3–9pm; the battery just needs 85% by 2:55pm, not by midnight.

**Default overnight behaviour: hold. Do not charge overnight when Solar Sponge will be cheaper.**

`overnight_hold` fires when:
- Time is 20:00–07:00 (nighttime)
- Current price > 10¢ (`SOLAR_SPONGE_PRICE_THRESHOLD`)
- SoC **≥ 25%** (battery not critically low)

When `overnight_hold = True`, the deterministic layer returns `hold / overnight_hold_wait_for_sponge` regardless of `deferral_detected`.

**Why:** Charging overnight at 13–17¢ when Solar Sponge at 6–8¢ is 8–12h away wastes ~55¢/night unnecessarily. Rule 13 morning deadline maths handles peak months from 9am — no pre-charge needed.

**Why ≥ 25% (not > 25%):** at 25% SoC with ~11h until Solar Sponge, the battery will drain to the Powerwall floor (~5%) before morning at typical home load (0.5 kW × 11h = 41% drain). At exactly 25%, the old `> 25` boundary let the hold fall through to deadline escalation — which then fired autonomous charging due to the deadline rollover bug below. Fixed 2026-06-24 to `>= 25`.

**Deadline rollover (fixed 2026-06-24):** `hours_to_2_55` previously computed as `max(DEMAND_DEADLINE − now_h, 0)`. After 2:55pm this goes negative and clamps to 0, making overnight cycles appear to be *at* the deadline and triggering urgent autonomous escalation. Fixed: if `now_h > DEMAND_DEADLINE`, roll over to next day — e.g. at 23:00, `hours_to_2_55 = 15.9h` (correct).

**Exceptions where overnight charging IS appropriate:**
1. Price ≤ 10¢ overnight — genuinely cheap, charging justified
2. SoC < 25% — survival top-up needed (Rule 7 handles this — self_consumption to survive-to-sponge SoC, NOT autonomous)
3. Peak month + tomorrow solar outlook is overcast (< 150 W/m²) AND price < 15¢ — Solar Sponge alone may not fill battery; pre-charge justified

**Rollback:** set `SOLAR_SPONGE_PRICE_THRESHOLD = 0` to disable, or set it very high (e.g. 30) to always apply.



### Rule 21 — Solar-Sufficient Hold (non-peak, sunny-forecast days)

**Added 2026-06-03.** On a sunny-forecast day (reliable solar accuracy) before 1pm, if net solar can cover the remaining gap to `cost_target` without any grid charging, hold and let solar do the work.

```
net_solar = max(expected_solar_remaining − home_load_kw × min(hours_to_deadline, 7h), 0)
gap       = max((cost_target − soc) / 100 × 13.5, 0)
if net_solar ≥ gap AND solar_reliable AND now < 1pm: hold ("solar_will_cover")
```

Rule fires before overnight_hold and deferral logic in the non-peak path. After 1pm, escalation rules take over regardless.

**Why this matters**: on a sunny forecast day, the human default is hold-until-you-must. Unnecessary trickle-charging from grid (a) costs money, (b) crowds out incoming solar (battery partially full is less able to absorb a solar surge). The risk asymmetry (demand charge) only pushes toward earlier charging when the solar forecast is uncertain. When it's confident, hold is the efficient choice.

**If solar underdelivers**: the accuracy checks (`forecast_accuracy = poor/unreliable`) will flip `solar_unreliable = True`, `solar_can_cover = False`, and the deadline escalation rules will take over. The design is: optimistic hold → monitor actuals → escalate if needed.


### Rule 22 — Wait-and-Go-Hard: Find the Cheapest Feasible Charge Slot

**Added 2026-06-05.** On peak days where grid charge is needed but the agent is not yet at deadline urgency, the correct strategy is not to start slow self_consumption immediately. Instead, find the cheapest upcoming slot where fast-filling to 85% still fits before the deadline, wait for it, then charge at 5kW (autonomous).

**Every cycle, scan the price forecast for the cheapest slot where:**
```
hours_until_slot + fill_fast_85_h + 0.5h ≤ hours_to_2:55pm
```
Conservative SoC projection at each slot: home load drains the battery during the wait, no solar credit (pessimistic — real outcome is better if solar produces).

| Condition | Rule | Action |
|-----------|------|--------|
| Cheaper slot ≥1¢ below now exists and is feasible | `wait_for_cheap_go_hard` | Hold and wait. Report slot price and hours-until in summary. |
| No cheaper slot in feasible window | `peak_charge_now` | Charge at self_consumption now — current price is as good as it gets. |

`go_hard_slot` (price, hours_until) is logged to JSONL `computed_context` and shown in the deterministic REFERENCE block each cycle.

**Why:** self_consumption from 8:30am at 17¢ on a day with Solar Sponge arriving at 11¢ at 10am costs more and ties up the charger during solar-producing hours. Waiting and going fast at the cheap price takes less time and money.

### Rule 23 — Receding Horizon Solar Sponge Rate Selection

**Added 2026-06-05.** Once in the Solar Sponge (or at the cheap window identified by Rule 22), the charging rate is not fixed — it's recalculated every cycle as solar data and prices update.

```
if fill_slow_85_h ≥ hours_to_2:55pm − 1.0h  →  autonomous (peak_sponge_go_hard): tight, go hard
if fill_slow_85_h < hours_to_2:55pm − 1.0h  →  self_consumption (peak_sponge_selfcons): fits, solar may reduce need
if kwh_needed_85 ≤ 0                         →  hold (peak_solar_will_cover): solar covering the gap
```

**Key principle:** each 30-minute cycle is an independent optimization — mode is NOT preserved from the previous cycle. The `battery_autonomous_revert_target_reached` automation fires within 30s of hitting 85% SoC, regardless of the LLM cycle. It is a safety net, not the primary rate controller.

**Why:** as solar improves during the morning, the grid charge needed falls each cycle. A 5kW rate justified at 10:00am (SoC=40%, solar=0.3kW) may be unnecessary at 11:00am (SoC=65%, solar=2kW, fill_slow now 1.6h vs 4.4h deadline). Recalculating prevents over-charging from grid when solar is contributing.

**`battery_grid_charge_target` floor (added 2026-06-05):** In peak months before 3pm, `sensor.battery_grid_charge_target` is clamped to a minimum of 85% regardless of Solcast remaining forecast. Without this, the Solcast-optimistic formula could return 13% on a cloudy day (thinking solar will cover everything), causing `battery_autonomous_revert_target_reached` to fire immediately after autonomous mode is set (battery already above 13%). The 85% floor ensures the automation only stops charging when the demand-window target is actually met.


### Rule 24 — Peak Survival Charge: Battery Won't Reach Solar Sponge

**Added 2026-06-23.** If the battery is projected to drain below 5% (Powerwall floor) before Solar Sponge opens (10am), and the conditions for waiting (Rule 25) aren't met, charge now rather than risk a forced emergency charge at a higher price later.

```
hours_to_sponge = max(10.0 − now_h, 0.0)
projected_soc_at_sponge = soc − (home_load_kw × hours_to_sponge / 13.5 × 100)

if kwh_needed_85 ≤ 0:          # solar covers gap to 85%
    if projected_soc ≤ 5%:     # but battery won't survive to sponge
        → Rule 25 check first
        → if not worth waiting: charge now (self_consumption, target 85%)  → "peak_solar_cover_survival"
```

**Context this fires in:** overnight or early morning (7am) when battery drained during the night and Solar Sponge is too far away or not cheap enough to justify waiting.

**Why 85% target:** emergency charge in peak months must hit the demand-window target in one pass. Setting a lower target risks needing a second charge cycle.

### Rule 25 — Peak Survival Wait: Sponge Is Close and Meaningfully Cheaper

**Added 2026-06-23.** If the battery can barely survive to Solar Sponge AND the Sponge is ≤3 hours away AND is ≥5¢ cheaper than the current price, hold and wait. Charging now at 20¢ when Solar Sponge at 11¢ is 2.5h away costs more and locks the charger during the cheapest grid window.

```
worth_waiting = (
    hours_to_sponge ≤ 3.0
    AND forward_min ≤ price − 5.0    # sponge will be at least 5¢ cheaper
)

if worth_waiting: hold  → "peak_survival_wait_for_sponge"
else:             charge (Rule 24)
```

**Why 3h / 5¢:** 3h is enough time for the battery to drain to ~5% and then charge hard once Sponge opens. 5¢ is the minimum meaningful price gap — below that, the time cost of waiting and the uncertainty of the forecast aren't worth it.

**Why this is a sub-rule of the `kwh_needed_85 ≤ 0` branch:** it only fires when solar is projected to cover the gap to 85%. If solar is unreliable (kwh_needed_85 > 0), standard deadline escalation (Rule 13) applies instead.


### Rule 26 — Peak Early Morning Hold: Don't Charge on Overnight Price Spikes

**Added 2026-06-27.** In the peak month block, when grid charge is needed (`kwh_needed_85 > 0`) but no cheaper slot appears in the Amber forecast, the fallback was `peak_charge_now`. This was correct near the deadline but wrong when Solar Sponge (10am) is still hours away and autonomous mode has ample margin to fill from any SoC. A transient overnight or early-morning spike would cause charging at 24¢ when Solar Sponge will be 9–12¢.

**Fires when — all of:**
- `fill_fast_85 < hours_to_2:55pm − 2.0h` — autonomous mode can reach 85% before the deadline with ≥2h to spare
- `price > SOLAR_SPONGE_PRICE_THRESHOLD (10¢)` — price is above Solar Sponge levels

**Physics-based, not time-based.** The condition uses `fill_fast_85` (time for autonomous/5kW to reach 85% from current SoC) rather than a clock threshold. This correctly handles any hour from midnight through to ~noon:

- At 5am, SoC=35%, fill_fast_85=1.4h, hours_to_2:55pm=9.9h → 1.4 < 7.9 → hold ✓
- At 7am, SoC=5%, fill_fast_85=2.16h, hours_to_2:55pm=7.9h → 2.16 < 5.9 → hold ✓ (previously this scenario would have fired `peak_deadline_selfcons` since fill_slow_85=7.4h > 6.9h, starting slow self_consumption at 24¢ — wrong)
- At 9am, SoC=5%, fill_fast_85=2.16h, hours_to_2:55pm=5.9h → 2.16 < 3.9 → hold ✓

The 2h margin ensures we only bail into `peak_charge_now` when autonomous itself is running short of time.

**No SoC floor.** The battery is allowed to drain toward 5% (Powerwall floor) while waiting. Once at the floor, the grid covers home load directly. When Solar Sponge opens, `peak_sponge_go_hard` or `peak_deadline_autonomous` catches up cheaply and fast.

**Result:** `peak_early_morning_hold` (hold). The next cycle re-evaluates with a fresh Amber forecast — if the spike has resolved, `wait_for_cheap_go_hard` or Solar Sponge logic takes over.

**Position in tree:** after `wait_for_cheap_go_hard` (hold for a known cheap slot) and `peak_deadline_selfcons` (charge because self_consumption is getting tight), before `peak_charge_now` (charge because no better option visible). `peak_charge_now` now fires primarily when price is at/below Solar Sponge threshold (10¢) and no forecasted cheaper slot exists — i.e., price is already as cheap as it gets.

```
fill_fast_85 < hours_to_2:55 − 2h  AND  price > 10¢
    → "peak_early_morning_hold"  (hold — let the next cycle re-evaluate)

else
    → "peak_charge_now"  (charge — price is cheap or autonomous is running short on time)
```

**Root cause that motivated this rule:** 2026-06-27, battery charged at 5am at 24¢. Realized prices were 19¢ at 4am, 24¢ at 5am, 19¢ at 6am (a transient spike). But Amber's forecast at 5am showed the spike continuing, so `_cheapest_go_hard_slot` found nothing cheaper and `peak_charge_now` fired. The 10am Solar Sponge was outside the ~6h Amber window, or forecast at a similar price to the spike. Rule 26 would have held instead — next cycle would have seen the spike resolve.


### Rule 27 — Manual Override: Human Takes the Wheel

**Control:** `input_boolean.agent_manual_override` (toggle on the HA dashboard).

While ON, the deterministic layer still computes and logs its verdict — shadow and
divergence data keep accumulating, and cycles are tagged `manual_override` in
`decisions.jsonl` so they can be excluded from accuracy analysis — but it **sends no
commands**, leaving whatever reserve and mode the user set in place.

```
override ON  → compute verdict, log it, send nothing
override OFF → normal control
```

**Hold verdicts are suppressed too.** This is the non-obvious part: a HOLD verdict
unconditionally drives reserve to 5%, so without suppressing it the agent would silently
undo a manual setting on the very next cycle while appearing to "do nothing".

**What it does NOT suspend:**
- the **Rule 2 demand-window reserve guard**, which runs earlier in `run_agent()`
- any **HA automation** (Layer 0), which fire independently of the agent

So the override can cost money; it cannot cause a demand-charge breach. That asymmetry
is deliberate — the whole point of Layer 0 is that no reasoning layer above it, human or
machine, can switch it off.

---

### Rule 28 — Control Inputs Are Range-Checked, Not Trusted

**Control:** `SETTINGS_SPEC` in `agent/energy_agent.py`.

The eight `input_number` helpers on the HA dashboard are not preferences the agent
consults politely — they are **control inputs**, read every cycle by
`compute_decision_context()`, which has been authoritative since Phase 5. A wrong value
is therefore a control fault, not a cosmetic UI issue.

**No target values are stored in code or in this document.** The HA console is the single
source of truth for what the targets *are*; `SETTINGS_SPEC` declares only the range that
is structurally valid. The distinction matters:

- a **target** ("charge the EV fast below 10¢") is a preference — it lives in HA, is set
  and displayed there, and is never duplicated anywhere else, because duplicates go stale
  silently. On 2026-07-23 `CONTEXT.md` claimed 6¢ while the console said 10¢, and this
  document gave the same helper two different "defaults" (5¢ and 6¢) in two places.
- a **band** ("below 0 or above 12 and the rule stops meaning what it should") is an
  engineering limit. Validation is impossible without one, so bands are the only numbers.

```
value inside [lo, hi]  → used as-is
value outside [lo, hi] → substituted for THIS CYCLE ONLY + notification, preferring:
                           1. the last in-band value HA itself reported
                              (from `settings_used` in decisions.jsonl)
                           2. else the bad value clamped to the nearest band edge
                           3. else the key is omitted, and the caller's own
                              `.get(key, default)` applies
unreadable/unavailable → last in-band value HA reported; silent (transport failure,
                         not a bad value)
```

Step 3 is safe precisely because `.get(key, default)` is correct for a *genuinely absent*
value — which was never the bug. The bug was a key that **existed** holding a wrong value,
where `.get`'s default can never fire.

**In-band tuning is never overridden.** The bands catch values that break control logic,
not values that merely differ from a preference. If a value genuinely is wanted, widen the
band — e.g. set `max_insurance_floor_pct`'s `lo` to 0 to allow disabling Rule 15.

**To read what is actually in force**, look at `settings_used` in the most recent
`agent/decisions.jsonl` record — the values the agent decided with, logged every cycle.

**Validate and warn, not self-heal.** Nothing is written back to HA. The substitution
protects the *current cycle's decision*; the helper keeps its bad value and the user is
notified so the UI and the agent are reconciled deliberately. Writing back would fight
legitimate adjustments and hide the drift being diagnosed.

**Audit trail:** every cycle logs `settings_used` and `settings_violations` to
`decisions.jsonl`. This deliberately does not depend on HA's recorder, which on
2026-07-23 was found not to be capturing these helpers at all — six days of history
returned one row per entity while the live states carried same-day `last_changed`.

**Why it exists:** 2026-07-23. Two failures, both invisible:

1. `battery_max_insurance_floor_pct` sat at **0**, silently disabling Rule 15's insurance
   floor. The code reads `settings.get("max_insurance_floor_pct", 70)` — but the key
   *existed* with value `0.0`, so the 70 default never fired.
2. `ev_min_soc_pct` drifted to **80** (intended 30), making `ev_soc(60) < ev_min` true.
   `ev_case3_below_minimum` fired and put the Zappi on **Fast** at 09:30 on a peak
   morning while the house battery was at 30% and falling toward the 2:55pm deadline —
   the EV competing with the battery for the cheap window. The guard here was
   `ev.get("min_soc_pct") or 20`, which only catches falsy values; 80 is truthy.

The lesson generalises: `x or default` and `dict.get(k, default)` express "if absent",
not "if wrong". A control layer needs the second, and only an explicit band gives it.

---

### Rule 29 — The Control Layer Reasons From Calibrated Solar, Not Raw Solcast

**Control:** `USE_CORRECTED_SOLAR` in `agent/energy_agent.py` (kill-switch, default True).

Solcast systematically over-forecasts this flat-roof site in winter, and the error is
strongly hour-dependent — measured actual/forecast ≈ **0.14 at 08:00**, **0.16 at 09:00**,
rising to **0.74 by 13:00** (`model_params.json["solar_correction"]`, from
`build_models.py`). Whole-day ratios run **0.26–0.53, median 0.42**.

From Phase 2.5-B's activation (2026-07-22) until 2026-07-23 the correction reached only
the **dashboard sensor** and the **shadow LP**. `compute_decision_context()` — the
authoritative layer since Phase 5 — still read raw Solcast. Layer 2's calibration was
not reaching the layer in control.

```
remaining = corrected  when USE_CORRECTED_SOLAR and corrected is not None
          = raw        otherwise
```

**Falls back to raw, never to zero.** If Solcast's `detailedHourly` attribute is missing,
the layer uses the raw figure and behaves as it did before. Falling back to *zero* would
be the dangerous choice — it would make the agent grid-charge hard on any cycle where a
Solcast attribute happened to be unavailable.

**Direction of risk:** the corrected figure is lower, so `kwh_needed_85` is larger and the
agent charges more and earlier. That costs money and protects the demand charge — the safe
direction to err.

**Honest scope — what this does and does not fix.** Replaying all 21 cycles of 2026-07-23
with raw vs corrected solar changed **which rule fired in 18 of them, and the action in
none**. The overnight holds were driven by *price* (13–16¢ overnight vs an 11¢ Solar Sponge
reachable before the deadline), not by the solar forecast, so this change would **not** have
prevented that night's drain to 17%. It was originally proposed as the fix for exactly that,
and the replay disproved it.

What it does buy is a rule layer whose `kwh_needed_85` and `net_expected_solar` are honest.
It will matter on the days when solar genuinely decides the outcome — a marginal peak day
where raw Solcast says the gap is covered and the calibrated figure says it isn't. Both
figures are logged per cycle (`solar_remaining_raw_kwh`, `solar_remaining_corrected_kwh`,
`solar_remaining_used_kwh`) so the effect can be measured rather than assumed.

**Auto-expiry:** 12 hours (`MANUAL_OVERRIDE_MAX_HOURS`), after which the agent resumes
control and says so loudly. A forgotten toggle must not silently disable the agent for
days — the failure mode is arriving at a peak-month demand window with a flat battery.

**Fails open:** if HA is unreachable the agent keeps control rather than going passive.
A 404 (helper not defined in this HA instance) is treated as "off", silently.

**Why it exists:** 2026-07-22 — the user watched the agent grid-charge at 12¢ while 7¢
was visible three hours ahead, and had no way to intervene. The only prior route was
calling `rest_command.powerwall_set_backup_reserve` by hand, which the agent then undid
at the next 30-minute cycle. A human who can see the agent is wrong should be able to
stop it without fighting it every half hour.

---

### Rule 30 — One Overnight Survival Floor (rule layer and safety net agree)

**Decided 2026-07-24.** The system had *two* survival floors that disagreed by 15 points, and
they fought:

- The **deterministic rule layer** is designed to ride the battery down to the **5% reserve
  floor** overnight. `peak_early_morning_hold` and `wait_for_cheap_go_hard` deliberately hold at
  any SoC — the battery physically stops discharging at its 5% backup reserve, grid covers home
  load there at overnight prices (no demand charge — the demand window is 3–9pm), and the Solar
  Sponge deadline logic (`peak_sponge_go_hard` / `peak_deadline_autonomous`) refills it cheaply
  before 2:55pm. Riding low overnight is *intended*, not a failure.
- The **`battery_low_soc_emergency_charge` automation** triggered at **SoC < 20%**. So it
  pre-empted the rule layer's intended low holds every low-SoC morning: it set reserve high, the
  next HOLD verdict cleared reserve back to 5%, the battery discharged, and the two layers
  oscillated (observed 2026-07-23 → 24; the 2026-07-24 08:00 over-charge began this way).

**Resolution — trust the projection, ride lower.** The emergency automation's trigger (and its
matching condition) were lowered **20% → 10%**, aligning the safety net with the rule layer's
actual operating floor. 10% rather than 5% keeps roughly one 30-minute agent cycle of margin above
the physical reserve (home load ~1.2 kW drains ~4–5 points/cycle), so the backstop still catches a
genuine projection failure or a stalled agent before the battery is truly empty, without fighting
the routine overnight holds. Exactly 5% was rejected: it would leave zero margin and be redundant
with the reserve floor itself.

**This is a deliberate reversal of scope, not of the 5% floor.** Session 10's "5% survival floor
replaces the 20% threshold" changed the *rule layer's* decision floor to 5% but left the *safety
automation* at 20% — this rule finishes that migration by moving the automation too. The rule
layer's 5% projected floor is unchanged. Residual overlap is narrow: the automation only charges
when price ≤ 20¢ (and cheap-window or ≤ 10¢), and at genuinely low SoC the rule layer's own
deadline logic is usually charging too, so they agree rather than fight.

**Not changed:** the automation still targets 85% in peak months before 3pm (full demand-window
buffer — an emergency is not the time to trust a Solcast-optimistic sub-85% target), still never
fires during the demand window or above 20¢, and still runs only 07:00–22:00. Overnight low-SoC
charging is the rule layer's job alone (the overnight top-up automations remain disabled).

---

## Decision Priority Order
When multiple rules conflict, apply in this order:

1. **Rule 6** — Never below Minimum Battery Threshold (safety)
2. **Rule 2** — Never import during demand window in peak months (cost)
3. **Rule 3** — Avoid negative FIT and export penalty (revenue protection)
4. **Rule 10** — Price spike arbitrage (deprioritised — not implemented; demand window conflict makes it rarely viable)
5. **Rule 8** — Exploit negative spot prices (revenue)
6. **Rule 1** — Get to 100% by 3 pm daily (daily target)
7. **Rule 5** — Excess solar to EV (efficiency)
8. **Rule 4** — EV tiered charging (convenience)
9. **Rule 7** — Opportunistic overnight charging (optimisation)

---

## Open Questions
- **EV sensors**: Zappi entities appear more reliable than the Polestar 4 third-party integration — primary EV detection will use Zappi (plug status, charge power). Polestar SoC sensor to be tested as we build automations; fall back to Zappi data if unreliable.
- **Powerwall export control**: whether HA can command the Powerwall to stop exporting (for Rule 3 negative FIT) — to be tested experimentally.
- **Rule 1 deadline**: currently 3 pm. On hot days, AC may draw 3–4 kW from 1–2 pm onwards, draining the battery before the demand window even starts. Consider whether to move the target deadline earlier on hot days — to be decided once Daikin integration is in place and we can observe actual AC load patterns.

## How Grid Charging Is Triggered — Critical Mechanism

**Grid charging ONLY occurs when `backup_reserve_percent > current_soc`.** This is the sole trigger.

| Reserve | Battery SoC | Grid draw? |
|---------|------------|-----------|
| 5% | 62% | ❌ No — reserve ≤ SoC, nothing to do |
| 80% | 62% | ✅ Yes — charges at ~1.7 kW (self_consumption) or ~5 kW (autonomous) until 80% |
| 100% | 62% | ✅ Yes — charges hard until full |

**Implication for the agent:** saying "system is in self_consumption mode" does NOT mean grid charging is happening. Without `reserve > soc`, the Powerwall charges from solar surplus only. To trigger intentional grid charging, the agent must call `set_powerwall_reserve(target_pct)` with `target_pct > current_soc`.

*Observed failure mode (2026-05-28 14:00):* agent correctly reasoned that 1.3 kWh grid top-up was needed, but left reserve at 5% with battery at 62% — no grid draw occurred. Correct action: `set_reserve(80%)` to trigger the grid charge.

---

## Known Limitations of the backup_reserve Control Mechanism

The `backup_reserve_percent` parameter (set via Tessie API) is the only writable control available without direct Tesla Fleet API access. It is a *target*, not a charge rate command.

**What it can do:**
- Tell the Powerwall to charge up to a target SoC (set reserve = 100%)
- Set a discharge floor (set reserve = 20% or 5%)

**What it cannot do:**
- Command a specific charge rate (e.g. "charge at 5 kW from grid now")
- Force aggressive grid draw — in `self_consumption` mode, Tesla's firmware decides how much to pull from grid, typically prioritising solar and pulling from grid conservatively
- Execute time-aware charge scheduling (e.g. "charge hard for the next 60 minutes while price is cheap and solar is available, then stop")

**Practical consequence:**
On a day where grid price is cheap AND solar is about to disappear (visible in Solcast forecast), the ideal behaviour is to pull 3–5 kW from grid immediately to top up the battery. The current system cannot command this — it can only set the reserve target and hope the Powerwall charges at a useful rate.

**Resolution path:**
This is the core problem that Model Predictive Control (MPC) solves. A proper MPC implementation would combine Solcast solar forecast + Amber price forecast + current SoC to compute an optimal charge schedule with explicit rate targets. Requires either: (a) Tesla Fleet API with finer control, or (b) a third-party battery controller that accepts charge rate commands.
