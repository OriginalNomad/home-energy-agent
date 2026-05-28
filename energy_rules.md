# Energy Automation Rules

## Implementation note (as of 2026-05-26)

These rules are now implemented by a **Claude-powered agent** (`agent/energy_agent.py`) rather than HA automations. The agent reads this rule-set's intent via its system prompt and applies it to real-time sensor data + price/solar forecasts every 30 minutes.

Hard constraints (Rule 2 demand window, export guard) remain as HA automations that fire independently of the agent. The agent handles all strategic decisions (when to charge, which mode, how much, EV Zappi mode).

See `CONTEXT.md` for the current automation status and which rules are agent-handled vs HA-enforced.

---

## Terminology

| Term | Definition |
|------|------------|
| **Minimum Battery Threshold** | The SoC floor (20%) below which automation will never intentionally discharge the Powerwall |
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
The agent re-runs this calculation every 30-min cycle when a price spike is visible in the forecast. It starts slow (cheap) and escalates to fast only when the maths demands it.

1. `kWh_needed = (target_soc − current_soc) / 100 × 13.5`
2. `hours_to_fill_slow = kWh_needed / 1.7` (self_consumption)
   `hours_to_fill_fast = kWh_needed / 5.0` (autonomous)
3. `hours_to_spike` = hours until price first exceeds 30¢ in forecast
4. Mode decisions:
   - `hours_to_spike > hours_to_fill_slow + 1.5h` → spread logic applies, may wait for cheaper window
   - `hours_to_spike ≤ hours_to_fill_slow + 1.5h` → start self_consumption now, no time to wait
   - `hours_to_spike ≤ hours_to_fill_fast + 0.5h` → escalate to autonomous immediately

**Escalation:** once self_consumption charging has started, the agent rechecks each cycle. If `hours_to_spike ≤ hours_to_fill_slow + 0.5h`, it switches to autonomous. Start slow and cheap; escalate automatically when the deadline demands it.

*Example: 36% SoC, 85% target, spike at 3pm, now 10:30am. kWh needed = 6.6, slow needs 3.9h, fast needs 1.3h. 4.5h to spike ≤ 3.9 + 1.5 = 5.4h → start self_consumption now. At 1pm, 55% SoC, 2h to spike, slow needs 2.4h → 2h ≤ 2.4 + 0.5 → escalate to autonomous.*

**Timing:**
- Checked at **9:30 am** (initial trigger)
- Re-checked **every 30 minutes from 9:45am to 2pm**, AND **immediately when `amber_in_cheap_window` flips to True** — charging starts the moment the cheap window opens
- Stops charging the moment `amber_in_cheap_window` flips to False
- Target reserve updates each 30-min tick as Solcast revises and SoC rises — naturally lowers the grid charge target as solar arrives
- Hard reset at **2:55 pm** — clears any active charge target before demand window

**Inverter underperformance override:**
- If Solcast `power_now > 500W` but inverter shows `< 200W`, treat as a solar deficit regardless of remaining-today forecast — charge from grid rather than waiting on solar that isn't arriving

**Low SoC emergency charge (outside solar window):**
- If battery drops below **20%** at any time between 7am–10pm and price is cheap (cheap window open OR price ≤ 10¢), switch to self_consumption and set reserve to 20% immediately
- Once SoC reaches 20%, revert to self_consumption at 5% reserve and hand off to solar
- Safety net only — gets the battery off the floor before the solar window opens
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

### Rule 3 — Avoid Negative Feed-in Tariff (FIT) and Export Penalty
- If Amber export price goes **below $0/kWh**: stop exporting to grid
- Divert excess solar to battery first, then EV (if plugged in), then curtail
- Check: `sensor.1a_wigram_road_glebe_feed_in_forecast` for upcoming negative FIT windows
- Ideally charge battery/EV *before* a negative FIT window begins to maximise absorption capacity
- **Export penalty**: EA116 applies an infrastructure export charge if solar export during 10am–3pm consistently exceeds an approved threshold — absorb excess solar on-site (battery, EV) rather than exporting during this window

### Rule 4 — EV Charging by State of Charge and Price

**Target SoC: 60%** (not 100% — parallel to battery's 95% target, but without a fixed deadline).

The EV charging logic mirrors the battery system: use `amber_in_cheap_window` as the primary price gate rather than fixed per-tier price ceilings, and apply the same "charge what's needed, let solar cover the rest" philosophy.

**Core policy: EV never charges from the Powerwall battery.**
The Powerwall cannot distinguish EV load from home appliance load on the same circuit. The workaround: use `Eco+` as the default/safe mode (charges only from actual grid export — genuine solar surplus). `Fast` mode is only used when the battery is physically unable to discharge (it's below its reserve floor and charging from grid) or has already met its reserve target.

**Priority order (first matching condition wins):**

| Priority | Condition | Zappi mode | Notes |
|----------|-----------|-----------|-------|
| 1 | Demand window + peak month | `Eco+` | Export surplus only — battery protected, Rule 2 absolute |
| 2 | Price < 5¢ or negative | `Fast` | Ultra-cheap — charge EV + Powerwall together |
| 3 | EV SoC < 30% + price < 20¢ | `Fast` | Critical EV level — charge alongside Powerwall |
| 4 | EV SoC < 60% + cheap window + battery ≥ (reserve − 5%) | `Fast` | Battery at/above its floor — EV safely takes grid |
| 5 | EV SoC < 60% + cheap window + battery < reserve (charging) | `Fast` | Battery charging from grid, cannot discharge — safe to charge EV simultaneously |
| 6 | Default | `Eco+` | Export surplus only — battery never discharged for EV |

**Case 4 vs Case 5 logic:**
- **Case 4** checks `tessie_powerwall_charge ≥ powerwall_backup_reserve − 5`. The reserve is the real floor (cloudy day top-up sets it to 80%; sunny day grid charge target sets it to ~60%). Once the battery has met its floor, EV can safely go Fast.
- **Case 5** checks `tessie_powerwall_charge < powerwall_backup_reserve`. When battery is *below* its reserve floor, it is actively charging from grid and physically cannot discharge for home or EV load. Fast mode is safe — both charge from grid simultaneously. This handles rainy days where there is no solar export but grid is cheap.
- **Default Eco+** catches everything else — cheap window closed, or battery in the gap between reserve and grid charge target. EV only charges from actual grid export (solar surplus after all home consumption).

**Zappi modes:**
- **Fast**: full rated speed from grid; solar reduces grid draw but charge is grid-led
- **Eco+**: charges only from actual grid export (power physically exporting to street). Battery discharge does not create export, so EV will not charge when battery is covering home load.
- **Eco**: throttles to match in-home surplus; battery discharge counts as surplus — EV benefits indirectly. **Not used** — replaced by Eco+ to enforce battery protection policy.

- Respect Rule 2: no EV grid charging during demand window (3–9 pm) in peak months
- Re-evaluates every 10 min and immediately on price window / plug / demand window changes
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

### Rule 7 — Opportunistic Overnight Grid Charging (Seasonal)
- Do not assume overnight charging is needed or useful — evaluate based on season, solar forecast, and price comparison

**Step 1 — Safety net: maintain Minimum Battery Threshold overnight**
- Ensure battery will stay above **Minimum Battery Threshold (20%)** until solar kicks in (~9:30 am)
- If SoC is likely to drop below 20% before solar arrives, top up to 20% with a **Price Ceiling of 25¢**
- This is a small top-up only — not a full charge
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

### Rule 11 — Solcast vs Inverter Cross-Check
- During solar hours (9am–5pm), compare Solcast's hourly forecast against the inverter's actual output every 30 minutes
- The agent computes a `forecast_accuracy` label from three Solcast sensors and the SolarEdge inverter:

| Sensor | Unit | Use |
|--------|------|-----|
| `sensor.solcast_pv_forecast_forecast_this_hour` | Wh (÷1000 = kWh ≈ avg kW) | Primary accuracy reference — hourly aggregate is more stable than instantaneous |
| `sensor.solcast_pv_forecast_forecast_next_hour` | Wh (÷1000 = kWh) | Forward-looking — tells agent whether solar is improving or staying low |
| `sensor.solcast_pv_forecast_power_now` | W (÷1000 = kW) | Secondary instantaneous reference |
| `sensor.solaredge_current_power` | W (÷1000 = kW) | Actual inverter output |

**Accuracy labels:**
- `good` (actual ≥ 70% of forecast): use `forecast_remaining_today` as-is
- `poor` (actual 30–70% of forecast): treat `remaining_today` as ~50% of stated value
- `unreliable` (actual < 30% of forecast): ignore `remaining_today` entirely; treat as zero-solar day
- `not_applicable`: night or near-zero forecast (< 0.2 kWh) — no meaningful comparison possible

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

### Rule 10 — Price Spike Arbitrage
- **Gentle arbitrage is explicitly excluded** — buying cheap and selling at moderate peaks (e.g. 10¢ buy / 18¢ sell) generates insufficient margin (~$115/year) to justify the battery cycle degradation cost (~$15,000 replacement)
- **Spike arbitrage only**: triggered when Amber export price forecast exceeds **50¢/kWh**

**Strategy: look ahead, don't discharge greedily**
- Do NOT discharge at the first price that crosses the threshold — scan the full forecast window first
- Find the **highest price period** in the upcoming 6 hours (or to end of demand window)
- Hold battery at full charge until that peak arrives, then discharge at maximum rate (5 kW)
- Calculate discharge start time based on how long the peak window lasts and battery SoC:
  - Full battery (13.5 kWh) at 5 kW = ~2.7 hours of discharge
  - e.g. if peak is forecast 7–9 pm, start discharging at 7 pm not 3 pm
- After spike passes, recharge during next cheap window — recharge cost should be well below spike revenue

**Example:** 3 pm at 50¢ vs 7 pm at $32
- Discharging at 3 pm: 13.5 kWh × 50¢ = **$6.75**
- Waiting for 7 pm: 10 kWh × $32 = **$320**
- Conclusion: always scan the full window before committing to discharge

**Safety constraints during spike arbitrage:**

| Scenario | Constraint |
|----------|------------|
| Peak month, during demand window (3–9 pm) | Do not discharge below buffer needed to cover estimated home load for remaining window hours — cannot risk needing to import before 9 pm |
| Peak month, outside demand window | Can discharge to Minimum Battery Threshold (20%) |
| Off-peak month | Can discharge to Minimum Battery Threshold (20%) |

- Spike threshold of **50¢** is configurable — monitor actual spike patterns to tune
- Forecast source: Amber feed-in forecast sensor
- Rule 2 still applies: no grid **import** during demand window regardless of price — but grid **export** during demand window is fine and encouraged during spikes

---

## Decision Priority Order
When multiple rules conflict, apply in this order:

1. **Rule 6** — Never below Minimum Battery Threshold (safety)
2. **Rule 2** — Never import during demand window in peak months (cost)
3. **Rule 3** — Avoid negative FIT and export penalty (revenue protection)
4. **Rule 10** — Price spike arbitrage > 50¢ (revenue)
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
