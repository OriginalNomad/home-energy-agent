# Battery Control Service — Product Design

## Core Insight

A residential battery optimisation system has three distinct layers, underpinned by a **site configuration** layer that describes what hardware exists and what the tariff structure is. Without this foundation, neither the goals nor the solver can operate:

```
┌─────────────────────────────────────┐
│           USER GOALS                │  What the user wants to achieve
│   (objective function / weights)    │
└─────────────────┬───────────────────┘
                  │
                  ▼
┌─────────────────────────────────────┐
│             SOLVER                  │  Derives policy from goals + world model
│   (MPC / rule engine / LP)          │
└─────────────────┬───────────────────┘
                  │
                  ▼
┌─────────────────────────────────────┐
│      FORECASTS + CONSTRAINTS        │  The world model
│   (solar, price, load, hardware)    │
└─────────────────────────────────────┘
```

**The key insight:** Goals are far easier to articulate than the rules that try to achieve them. Every user has different goals. A goal-driven architecture means users express preferences — the system derives the policy. Users never touch rules.

This is also the key product differentiator: competing systems (e.g. Amber SmartShift) hardcode their own goals into the optimiser. A goal-driven platform lets each user define what optimal means for them.

---

---

## User Journey

Onboarding follows a deliberate sequence: **goals first, configuration second**. Users need to understand *why* they're entering technical details before they'll invest the effort. Goals create the motivation; configuration unlocks the capability.

```
1. GOALS          "What do you want to achieve?"
      ↓               Simple, conversational — no technical knowledge required
      ↓               Takes 2–3 minutes
      
2. SITE PROFILE   "What do you have to work with?"
      ↓               Hardware, tariff, integrations
      ↓               Can be partially auto-detected (Amber account, HA entities)
      ↓               Takes 10–15 minutes
      
3. REVIEW         "Here's what we'll optimise for"
      ↓               System shows derived plan: expected savings, key rules
      ↓               User confirms or adjusts
      
4. LIVE           Solver running, dashboard active
                      Alerts, savings reporting, goal adjustment over time
```

### Goal Elicitation (Step 1)

Goal elicitation is a **guided conversation**, not a form. The UX pattern is closer to a mortgage advisor than a settings page. Most users can't articulate their "loss aversion coefficient" — but they can absolutely describe how they felt when their bill doubled last winter.

Questions adapt based on answers. Terminology is explained inline when needed. The system derives solver weights from the conversation; users never see the weights directly.

#### Opening — set context before asking anything

Don't start with questions. Start with:

> *"Before we touch any settings, let's understand what you're trying to achieve. There are no wrong answers — your goals shape everything the system does."*

Then a single open prompt:

> *"What's prompted you to try this? What's been frustrating or costing you money?"*

This surfaces the real problem before structured questions begin. Someone saying "my bill doubled last winter and I don't know why" immediately signals demand charge exposure.

---

#### Topic 1 — The bill

- What does your electricity bill feel like right now? *(Too high / Unpredictable / OK but could be better)*
- Do you know if your tariff has a **demand charge**?
  > *Inline explanation: "This is a network fee based on your single highest 30-minute import during peak hours — one bad evening can add $50–100 to your bill for the whole month."*
- If yes: has that happened to you?

#### Topic 2 — Risk appetite

- If the system charged your battery from the grid at 15¢ when it could have waited for 10¢ solar — does that bother you?
- How would you feel if a month's bill was slightly higher because the system took a calculated risk that didn't pay off?
- *(Surfaces the conservative ↔ aggressive spectrum)*

#### Topic 3 — Battery

- How long do you want your battery to last? *(Anchors longevity preference)*
- Are you comfortable with it being discharged and recharged more than once a day if the price spread makes it worthwhile?
- What's the minimum charge you'd want at any given time?

#### Topic 4 — Solar

- On a good sunny day, does your solar roughly fill the battery, or does it usually fall short?
- On cloudy days — do you expect a grid top-up, or just live with less?
- Do you care about exporting to the grid, or would you rather absorb everything on-site?

#### Topic 5 — EV *(skip if no EV)*

- What's the minimum charge you need when you leave in the morning?
- What time do you typically leave?
- Have you ever been caught short — left home with less charge than you needed?
- How do you feel about charging overnight from the grid vs waiting for daytime solar?
- *(Surfaces urgency tier and price tolerance)*

#### Topic 6 — Lifestyle and peak times

- Are you usually home during the evening? *(3–9pm load profile)*
- Do you work from home? *(Daytime load)*
- Do you run the air conditioning regularly?
- Would you be comfortable with AC briefly switching off (5–10 min) if the battery was critically low during an expensive period?

#### Topic 7 — Calibration close

End with one concrete tradeoff that anchors everything:

> *"If you had to choose: save $500/year on average but with occasional months where savings are lower or zero — or save $300/year very consistently with predictable bills. Which feels better?"*

---

#### Reflect back before moving on

After topic 7, summarise what was heard — don't just advance to configuration:

> *"OK — here's what we've got. Your priority is avoiding demand charges above everything else. You're comfortable with grid charging when prices are clearly cheap, but you'd rather be conservative than clever. Your EV needs at least 60% by 7:30am. Does that sound right?"*

User corrects anything. Then — and only then — configuration begins. Every config question now has a visible *why* behind it.

---

#### From conversation to solver weights (internal)

The user never sees this mapping — it happens automatically:

| Answer | Solver weight affected |
|--------|----------------------|
| "Demand charges have hit me before" | `demand_penalty` → very high |
| "I'd rather be safe than clever" | `risk_aversion` → conservative |
| "Battery lasting 15 years matters to me" | `cycle_cost` → high |
| "I'm fine with variance if average is lower" | `risk_aversion` → aggressive |
| "I'd rather absorb solar than export" | `feedin_weight` → low |
| "$500 variable beats $300 certain" | `risk_aversion` → aggressive |

---

#### LLM as onboarding agent

The conversational goal elicitation is a natural fit for an LLM-based interface rather than a scripted wizard. Instead of a fixed branch tree, an LLM conducts the conversation naturally:

- Questions adapt to what the user has already said — no redundant or irrelevant prompts
- Terminology is explained on demand, not pre-emptively
- The user can give free-text answers ("honestly my bill last summer was a nightmare and I have no idea why") and the LLM extracts the relevant signal
- Follow-up questions emerge from context rather than a decision tree
- The reflection-back step ("here's what I heard — does this sound right?") is a natural LLM strength

**Implementation sketch:**

```
System prompt:
  You are an energy advisor helping a new customer set up their 
  battery optimisation system. Your job is to understand their 
  goals and translate them into a structured goal profile.
  
  Ask questions conversationally across these topics: [topics].
  When you have enough information, output a structured JSON goal 
  profile and present a plain-English summary for the user to confirm.

Output schema:
  {
    "demand_penalty": "critical | high | medium | low | none",
    "risk_aversion": "conservative | balanced | aggressive",
    "cycle_cost_sensitivity": "high | medium | low",
    "ev_priority": "critical | high | medium | none",
    "feedin_preference": "maximise | moderate | absorb_only",
    "load_shedding_consent": true | false,
    "notes": "free text — anything unusual about the site or user"
  }
```

The structured output feeds directly into the solver configuration. The conversation is the UI — no form, no sliders, no settings page for onboarding.

This also enables **goal refinement over time**: after a month of live data, the LLM can review actuals vs expectations and suggest adjustments — "your battery ran low during the demand window twice last month — would you like to be more conservative about grid charging in the morning?"

---

## Site Configuration

Before the solver can run, it needs a complete picture of the site. This is the onboarding data collected once (and updated when hardware or tariff changes). It feeds both the solver constraints and the goal UI — for example, a user without an EV should never see EV-related goal options.

### Energy Provider & Tariff

```yaml
energy:
  provider: "Amber Electric"
  tariff_type: dynamic            # dynamic | tou | flat
  tariff_code: "EA116"
  
  # For dynamic pricing
  price_api: amber                # amber | octopus | tibber | energex | etc
  api_key: "..."

  # Demand charge (if applicable)
  demand_charge:
    enabled: true
    rate_per_kw: 9.50             # $/kW/month
    measurement: 30min_average    # how network measures peak
    window: "15:00–21:00"
    peak_months: [11,12,1,2,3,6,7,8]

  # Feed-in
  feedin_type: dynamic            # dynamic | fixed
  feedin_fixed_rate: null         # if fixed, ¢/kWh

  # Network export limits
  export_limit_kw: 5.0            # approved export threshold
  export_penalty_window: "10:00–15:00"
```

### Solar

```yaml
solar:
  installed: true
  inverter_brand: "SolarEdge"
  inverter_model: "SE5000H"
  peak_capacity_kw: 5.0
  
  # Panel orientation (for Solcast)
  panels:
    - azimuth: 0                  # degrees from north
      tilt: 10                    # degrees from horizontal
      capacity_kw: 5.0
  
  # Forecasting
  forecast_provider: solcast
  solcast_resource_id: "fd2e-343e-680f-b27e"
  solcast_api_key: "..."
  
  # Integration
  ha_entity: "sensor.solaredge_current_power"
```

### Battery

```yaml
battery:
  installed: true
  brand: "Tesla"
  model: "Powerwall 2"
  usable_capacity_kwh: 13.5
  max_charge_rate_kw: 5.0
  max_discharge_rate_kw: 5.0
  
  # Control
  control_method: tessie_api      # tessie_api | tesla_fleet_api | local | sonnen | byd | etc
  api_credentials:
    energy_site_id: "..."
    access_token: "..."
  
  # Limits
  min_soc_default: 20             # % floor outside demand window
  min_soc_demand_window: 5        # % floor during demand window
  
  # Backup reserve (if blackout protection commissioned)
  backup_reserve_enabled: false
  backup_reserve_percent: 0
  
  # Integration
  ha_soc_entity: "sensor.tesla_powerwall_2_charge"
```

### EV

```yaml
ev:
  installed: true
  make: "Polestar"
  model: "4"
  year: 2026
  battery_capacity_kwh: 100
  max_ac_charge_rate_kw: 11.0
  
  # SoC data source
  soc_source: polestar_api        # polestar_api | tesla_api | zappi | manual
  ha_soc_entity: "sensor.polestar_7853_battery_charge_level"
  ha_charging_status_entity: "sensor.polestar_7853_charging_status"
  
  # Usage profile
  typical_daily_usage_kwh: 20
  departure_time: "08:00"
  minimum_soc_at_departure: 60    # %
  public_charger_cost_per_kwh: 0.60   # $/kWh — threshold below which home charging is always preferred
```

### EV Charger

```yaml
ev_charger:
  installed: true
  brand: "myenergi"
  model: "Zappi 2"
  max_charge_rate_kw: 7.2
  
  # Modes available
  modes:
    - fast                        # grid-led, full speed
    - eco_plus                    # solar + grid supplement
    - eco                         # solar surplus only
    - stop
  
  # Smart charging capability
  solar_divert: true
  grid_import_control: true
  
  # Integration
  ha_entity_prefix: "sensor.zappi_"
  control_method: myenergi_api
```

### Controllable Loads

The solver can treat these as schedulable or sheddable loads during demand window events.

```yaml
controllable_loads:

  air_conditioning:
    installed: true
    brand: "Daikin"
    zones: 3
    max_load_kw: 3.5
    integration: daikin_ha        # daikin_ha | sensibo | broadlink | none
    sheddable: true               # can be turned off during demand events
    thermostat_entity: "climate.daikin_living"

  heat_pump_hot_water:
    installed: false

  pool_pump:
    installed: false

  dishwasher:
    installed: true
    smart_plug: false             # no control capability yet
    sheddable: false

  washing_machine:
    installed: true
    smart_plug: false
    sheddable: false
```

### Site Summary (derived)

Once configuration is complete, the system can derive key site characteristics:

| Parameter | Derived value | Used for |
|-----------|--------------|----------|
| Max self-consumption | solar_kw + battery_discharge_kw | Demand window coverage |
| Max controllable load reduction | Σ sheddable loads | Emergency load shed |
| Daily EV energy need | typical_daily_usage_kwh | Charging schedule |
| Battery hours at avg load | usable_kwh ÷ avg_load_kw | Demand window planning |
| Breakeven arbitrage spread | battery cycle cost ÷ usable_kwh | Rule 10 threshold |

---

## Layer 1 — User Goals

Goals fall into three categories:

### Hard Constraints
Non-negotiable — violation is unacceptable regardless of cost.

| Constraint | Example |
|-----------|---------|
| Demand window protection | Never import from grid during 3–9pm in peak months |
| Minimum battery floor | Never discharge below 5% (hardware protection) |
| EV departure SoC | Car must have ≥ 60% by 7:30am |

### Soft Objectives
Desirable outcomes, traded off against each other by weight.

| Objective | Description |
|-----------|-------------|
| Minimise grid import cost | Buy cheap, avoid buying expensive |
| Maximise feed-in revenue | Sell at high prices when worthwhile |
| Battery longevity | Limit deep cycles, avoid unnecessary charge/discharge |
| EV charging cost | Charge EV as cheaply as possible |

### User Preferences
Expressed as priorities or sliders — translated into solver weights.

```yaml
# Example: user with demand charge tariff, EV, longevity-conscious
goals:
  avoid_demand_charges: critical        # hard constraint
  minimize_grid_cost: high
  maximize_feedin_revenue: low          # not willing to cycle battery for arbitrage
  battery_longevity: high
  ev_charged_by_departure: medium

constraints:
  demand_window: "15:00–21:00"
  peak_months: [11, 12, 1, 2, 3, 6, 7, 8]
  battery_floor_demand_window: 5%
  battery_floor_default: 20%
  target_soc_by: "15:00"
  ev_departure_time: "08:00"
  ev_departure_soc: 60%
```

**Different users, same platform:**

| User type | Goals differ in... |
|-----------|-------------------|
| No demand charge tariff | `avoid_demand_charges: not_applicable` |
| No EV | EV constraints absent |
| Flat tariff | `minimize_grid_cost` less time-sensitive |
| Arbitrage-focused | `maximize_feedin_revenue: high`, longevity weight lower |
| Off-grid minded | `battery_floor_default: 30%`, conservative |

---

## Layer 2 — Solver

The solver takes goals and the world model as inputs and outputs a charge/discharge schedule.

### Current approach (rule-based approximation)
Hand-crafted if-then rules that approximate optimal behaviour for one user's goals. Fast to implement, interpretable, brittle to goal changes. The rules in `config/automations.yaml` are this layer for the personal system.

**Known limitations of rule-based approach:**
- Goals are implicit and tangled into rules
- Adding a new goal requires editing multiple rules
- Cannot express tradeoffs — rules are binary
- No look-ahead beyond simple forecast checks

### Target approach (Model Predictive Control)

MPC solves an optimisation problem over a rolling time horizon (e.g. 24–48h), re-optimising every 30 minutes as new forecasts arrive.

**Objective function (simplified):**

```
minimise:
  Σ [ grid_import(t) × price(t) ]           # import cost
  - Σ [ grid_export(t) × feedin(t) ]         # export revenue
  + demand_penalty × max_30min_import        # demand charge risk
  + cycle_cost × Σ |Δsoc(t)|                # battery wear

subject to:
  soc(t) ∈ [floor, 100%]                    # battery limits
  charge_rate(t) ≤ 5kW                      # hardware limit
  grid_import(t) = 0 during demand window   # hard constraint (peak months)
  soc(departure_time) ≥ ev_target_soc       # EV constraint
```

**The loss function is asymmetric — this is critical:**
- Battery empty during demand window → demand ratchet → potentially 2× monthly bill
- Battery over-charged from grid → just the cost of those kWh

The `demand_penalty` weight must be set much higher than a naive cost optimisation would suggest. This is the "loss aversion" parameter Amber's engineering team referenced.

### The demand ratchet problem

EA116 charges based on the *single highest 30-minute average import* across the entire month. This creates a non-convex objective:
- First violation in the month: full demand charge applies
- Subsequent violations at same level: no additional cost
- Violation early in month is catastrophic; late in month may be acceptable

Practical approaches ranked by complexity:
1. **Hard zero-import constraint** (current) — simple, robust, conservative
2. **Rolling maximum tracking** — tighten constraints as month's demand maximum grows
3. **Stochastic penalty** — model probability of setting new monthly maximum, apply expected cost

### Load forecast uncertainty

The hardest sub-problem. Amber's SmartShift identified this: their load model underestimated actual evening consumption, so the battery entered peak depleted.

Recommended approaches:
- **Quantile forecasting** (P90 not mean) — plan for higher-than-expected load
- **Time-of-week patterns** weighted by recent actuals
- **Weather correlation** — temperature-dependent AC load is high-value feature
- **Controllable load awareness** — EV, dishwasher as schedulable loads, not uncontrollable

---

## Layer 3 — Forecasts and Constraints

| Input | Source | Update frequency |
|-------|--------|-----------------|
| Solar generation forecast | Solcast API | Every 15–30 min |
| Electricity buy price forecast | Amber API | Every 30 min |
| Electricity sell price forecast | Amber API | Every 30 min |
| Current battery SoC | Powerwall local API / Tessie | Every 2 min |
| Home load (actual) | Powerwall load sensor | Real-time |
| Home load (forecast) | Historical pattern model | Daily |
| EV SoC | Polestar API / Zappi | Periodic |
| Hardware limits | Configuration | Static |
| Tariff structure | Configuration | Monthly |

---

## Multi-Tenant Product Architecture

For a service controlling batteries across many households:

```
┌──────────────────────────────────────────────────────┐
│                   USER INTERFACE                      │
│         Goal elicitation, preference sliders          │
│         Dashboard, alerts, savings reporting          │
└─────────────────────┬────────────────────────────────┘
                      │
┌─────────────────────▼────────────────────────────────┐
│                CLOUD OPTIMISER                        │
│  Per-site MPC solver running on rolling 48h horizon  │
│  Re-optimises every 30 min on new forecast data       │
│  Goal weights → solver objective function            │
└──────────┬──────────────────────────┬────────────────┘
           │                          │
┌──────────▼──────────┐   ┌──────────▼──────────────┐
│  FORECAST SERVICES  │   │   BATTERY CONTROL API    │
│  Solcast (solar)    │   │   Tesla Fleet API        │
│  Amber (prices)     │   │   Sonnen API             │
│  Weather (load est) │   │   BYD / Huawei / etc     │
└─────────────────────┘   └─────────────────────────-┘
```

### Multi-battery support
Each battery type has different:
- Control parameters (backup_reserve vs charge_command vs time schedule)
- API authentication models
- Charge/discharge rate limits
- SoC sensor accuracy

The solver outputs a *desired state* (charge to X% by time T at rate R). A battery-specific adapter translates that into the correct API call.

### Multi-tariff support
Each tariff has different:
- Demand charge structure (or none)
- Peak/off-peak windows
- Feed-in rates
- Network export limits

The tariff model is a constraint input to the solver, not hardcoded into rules.

### Revenue model
- Subscription per site per month
- Savings-share model (% of verifiable savings vs baseline)
- White-label for energy retailers (Amber, Octopus, Tibber integration)

---

## Cloud-Native Deployment (no on-site hardware)

*A consumer service cannot require customers to install a local device. This section works through the constraints, the key insight that resolves them, and the resulting architecture.*

### The problem: safety latency

The personal system today runs 12 active HA automations on-site, including safety rules that must fire in seconds:

| Rule | Response time | Why |
|------|---------------|-----|
| Export safety net (autonomous mode) | <30 seconds | Battery exporting at a loss to grid — revert immediately |
| Demand window reset (2:55pm) | Exact time | Must fire before 3pm demand window opens |
| Negative price charge | ~1 minute | Opportunistic, not safety-critical |
| Low SoC emergency | ~1 minute | Need grid import before battery hits 0% |

A cloud-only system stacks API latency (2–5s per call to Tessie/device API), polling intervals, and internet reliability. The export safety net — the tightest constraint — becomes marginal at best.

### The key insight: lean on battery firmware, not software

Every modern residential battery has **local firmware that enforces safety regardless of cloud connectivity**:

| Guarantee | Source | Cloud dependency |
|-----------|--------|------------------|
| Never discharge below reserve | Battery firmware (Tesla, Sonnen, BYD) | None |
| Never export in `self_consumption` mode | Battery firmware | None |
| Default to safe state on comms loss | Battery firmware — holds current mode | None |
| Charge rate within hardware limits | Battery firmware / inverter | None |

**The safety layer isn't "our code running locally" — it's "only issue commands the battery's own firmware handles safely."** This is a constraint on the optimiser's *action space*, not a separate system to deploy on-site.

### The sacrifice: no autonomous mode

`autonomous` / `time_based_control` mode allows ~5 kW fast grid charging but also allows the battery to export — hence the export safety net. In `self_consumption` mode, the firmware itself prevents battery-to-grid export at the hardware level. No software safety net needed.

Dropping autonomous mode means:
- **Stuck at ~1.7 kW charge rate** (Tesla self_consumption grid supplementation)
- **Need to start charging earlier** on tight peak days to reach target SoC by the demand window
- **The LP optimiser already handles this** — it just schedules earlier, using more of the Solar Sponge window

For most sites and most days, this is fine. The LP's job is to plan within the hardware's real constraints, and 1.7 kW over a 5-hour Solar Sponge window (10am–3pm) delivers ~8.5 kWh — enough to fill a Powerwall from 30% to 93%. On a cloudy day with zero solar, that's still sufficient if the optimiser starts at 10am.

Edge cases where it's not enough (e.g. battery at 10% with 3 hours to demand window, zero solar) can be handled by:
1. **Bounded commercial risk.** Brief autonomous-mode use with cloud-based monitoring. The worst case from a 30-second cloud response vs a 2-second local response is ~60 seconds of unintended export at ~3 kW, costing ~0.25¢ per incident. Guarantee "we credit any export during autonomous mode" — negligible cost.
2. **Tesla Fleet API direct charge commands** (future). Tesla Fleet API can set a specific charge rate without entering autonomous mode, eliminating the export risk entirely.

### The cloud-native architecture

```
┌──────────────────────────────────────────────────────┐
│                   SOL WEB APP                         │
│    Onboarding conversation · Dashboard · Alerts       │
│    Savings reporting · Goal adjustment                │
└─────────────────────┬────────────────────────────────┘
                      │
┌─────────────────────▼────────────────────────────────┐
│              CLOUD OPTIMISER (per-site)               │
│  LP/MPC solver every 15–30 min                       │
│  Calls device APIs directly (no local relay)          │
│  Issues only firmware-safe commands                   │
│  Daily energy journal + decision log                  │
└──────┬──────────┬──────────────┬─────────────────────┘
       │          │              │
┌──────▼──────┐ ┌─▼────────────┐ ┌▼─────────────────────┐
│  FORECAST   │ │ DEVICE APIs  │ │  DATA STORE           │
│  SERVICES   │ │ (cloud-to-   │ │  daily_energy.jsonl   │
│             │ │  cloud)      │ │  decisions.jsonl      │
│  Solcast    │ │              │ │  (or Postgres/        │
│  Amber /    │ │ Tesla Fleet  │ │   TimescaleDB)        │
│  Octopus /  │ │ Sonnen       │ │                       │
│  Tibber     │ │ myenergi     │ │  Per-site calibration │
│  BOM weather│ │ Daikin       │ │  Fleet cohort priors  │
└─────────────┘ └──────────────┘ └───────────────────────┘
```

**What's absent:** any local device, HA instance, tunnel, or on-site compute.

**What enforces safety:** the battery's own firmware, constrained by the optimiser never issuing commands the firmware can't locally enforce. The command vocabulary is:
- `set_reserve(N%)` — firmware holds this floor
- `set_mode(self_consumption)` — firmware prevents battery export
- Zappi mode changes — myenergi firmware handles locally

None of these require sub-second cloud response. If the cloud is unreachable, the battery continues in its current mode — which is always safe by construction.

### What this requires that doesn't exist yet

| Dependency | Status | Path |
|------------|--------|------|
| **Tesla Fleet API** | On todo — requires developer registration | Eliminates Tessie ($10/mo), enables direct charge commands, potential LAN-direct mode |
| **Solcast direct API** | Already have credentials + API | Move from HA integration to direct cloud call |
| **Amber direct API** | Agent already calls it directly | Already solved |
| **myenergi cloud API** | Available, not yet integrated | Standard REST API, well-documented |
| **SolarEdge monitoring API** | Available, not yet integrated | For actual-vs-forecast comparison (calibration loop) |
| **Persistent data store** | `daily_energy.jsonl` + `decisions.jsonl` exist | Move from flat files to Supabase/TimescaleDB for multi-tenant |

### Advantages over the current architecture

- **No on-site hardware** — pure SaaS, nothing to install or maintain
- **No HA** — eliminates Docker, YAML, recorder rolloff, config divergence, update breakage
- **Multi-tenant from day one** — same cloud stack serves every customer
- **Better reliability** — cloud compute doesn't sleep, doesn't need cron, auto-restarts
- **Simpler onboarding** — "sign in, connect your Tesla/Amber/Solcast accounts, have a conversation about your goals, done"
- **The daily energy journal becomes the product's core data asset** — per-site learning from day one

### Disadvantages and risks

- **Device API reliability** — Tessie/Tesla Fleet goes down → no control. Mitigated: battery holds safe state; retry on next cycle. Monitor API health as an SLA metric.
- **API rate limits** — Tesla Fleet limits polling to ~200 requests/day per vehicle. Must be efficient: poll every 2–5 min (not 2 seconds), batch reads. The optimiser's 30-min cycle fits well within limits.
- **Latency for opportunistic actions** — negative price spikes, sudden solar surplus. 1–2 min response is fine for most; truly sub-minute reactions would need a local agent. Price this as a known limitation, not a failure.
- **Internet dependency** — if customer's internet is down, no optimisation. Battery defaults to self_consumption with its last reserve setting — safe, just not optimal. This is also true of every smart thermostat, every cloud-controlled device. Acceptable for a consumer product.
- **HA ecosystem loss** — Daikin AC, SolarEdge local monitoring, community integrations. For Sol as a product, build direct API integrations for supported devices rather than depending on HA's (brittle, update-breaking) integration layer.

### Migration path from the current system

1. **Now (Phase 4):** finish LP optimiser cutover on the personal system (still on HA + Mac Studio)
2. **Next:** replace Tessie with Tesla Fleet API (direct control, no middleman)
3. **Then:** move the optimiser to cloud (Supabase Edge Functions / fly.io / Lambda), calling Tesla Fleet + Amber + Solcast directly. HA stays for dashboard/monitoring but is no longer in the control path.
4. **Finally:** remove HA entirely. Sol web app replaces the dashboard. The daily energy journal replaces the recorder. Direct device APIs replace HA integrations.

The personal system becomes the first Sol customer. Same cloud stack, same onboarding, same optimiser — just with the full history already captured in `daily_energy.jsonl`.

---

## Optimisation Engine — Depth (target architecture)

*Layer 2 above sketches the MPC objective; the Multi-Tenant section sketches the adapters. This section goes deeper on three things that are the actual hard problems: what MPC really buys us, how to survive bad forecasts, and how one engine serves every user while learning over time. It is the design the repo-root agent is converging toward.*

### Why an optimiser, not a rule stack

Battery optimisation under a dynamic tariff is a **textbook finite-horizon optimal control problem**, not a reasoning problem. The repo-root agent today approximates the optimum with a hand-tuned rule stack (charge in the cheapest window, 85% by 2:55pm, Solar Sponge floor, deferral limits, a percentile price model…). Every one of those rules is an *emergent output* of a cost-minimising optimiser — none should be hand-written. A rule stack accretes special cases forever: each new edge case becomes a new rule, which becomes a new ordering/interaction bug. The fix is to stop approximating the optimum and **compute it**.

The problem is small and well-posed: one battery (~13.5 kWh, ±5 kW), 48 half-hour slots a day, a known price forecast, a known solar forecast, a known load profile, a demand-charge constraint. This is solvable to optimality by a small linear program in **milliseconds**.

### What MPC actually is (and why it tolerates bad forecasts)

**Model Predictive Control.** At each timestep you hold three things — a **model** of how the system evolves, a **forecast** of the future, and an **objective** — and you solve for the optimal action sequence over a finite horizon (next 24–48 h). You then **apply only the first action**, discard the rest, and **re-solve** at the next timestep with updated forecasts. That "receding horizon" re-planning is the whole trick.

The key consequence: **MPC is inherently robust to forecast error.** Forecasts degrade with horizon — accurate for the next 30 min, vague 6 h out. Because MPC only ever *commits* to the near-term slot (the most accurate part) and re-plans before the far slots arrive, day-ahead error largely washes out. The discipline is: don't try to be right about 2 pm at 8 am — be roughly right about the next slot, and re-plan.

Important: **the repo-root agent already runs the MPC loop** — it wakes every 30 min, looks ahead, acts, re-evaluates next cycle. It already has the *cadence* of MPC; it just uses an LLM + rule-stack as the "solver" in the middle. The re-architecture swaps that core for an actual optimiser; the loop structure stays. A thin **control-quantisation layer** maps the optimiser's ideal kW schedule onto whatever the hardware exposes (today: the indirect `backup_reserve_percent` hack; with Tesla Fleet API: a direct charge command), keeping the optimiser hardware-agnostic.

### Handling forecast uncertainty (solar especially)

On top of the structural robustness above, in priority order:

1. **Learn the site's own error signature.** Log forecast-vs-actual every cycle (the agent already writes `daily_accuracy` records) and fit a running **bias/scale correction** — e.g. "Solcast over-predicts ~30% under cloud at this flat roof in Glebe." A generic vendor model can't know a site's microclimate; the site can learn it in weeks. Highest value, lowest risk.
2. **Ensemble the sources.** Solcast + Open-Meteo + BOM, weighted by each one's *recent track record at this site* (inverse-error weighting beats trusting any single source).
3. **Nowcast the near slots.** The best predictor of solar in 30 min is solar *now* plus recent trend. Weight live inverter readings heavily for near slots, the model for far slots. (The agent's "if actual=0 for 2 cycles, treat as zero-solar day" is a crude version of this.)
4. **Move from point forecasts to distributions.** Instead of "solar = 18.9 kWh," produce quantiles ("p10=8 / p50=16 / p90=22") and run **robust MPC** — optimise against a *conservative* quantile for anything that hurts when you're short. The cost structure picks the quantile: being short of solar on a peak day risks a ~$100 demand charge, while over-charging from cheap grid wastes cents, so you plan against ~p25 solar (and ~p75/P90 evening load), not the mean. The agent's "insurance floor" and `price_position` model are hand-rolled approximations of this; the distribution makes it principled.
5. **Two-tier safety/opportunity split.** A *safety* plan that survives the demand window even if solar is zero (pre-charge enough cheap grid), plus an *opportunistic* layer on top that uses expected solar to avoid over-charging.

The mindset: **forecast accuracy is a product feature you build, not an input you're handed.** Every site produces its own ground truth daily — the system that learns each site's error model beats any vendor's generic forecast. That is a defensible moat, and it connects directly to the multi-tenant design below.

### One engine for every user: separate what varies from what's universal

Intent, devices, and tariff all differ per user. The architectural answer is a single principle: **the optimiser is universal; everything that differs between users is data fed into it.** No per-user branching logic — one general optimiser parameterised by three per-user models.

**Layer 1 — Intent → Objective function** *(Sol's core differentiator).*
The elicitation conversation translates fuzzy human values into **objective weights + hard constraints + a risk parameter**:

- "minimise my bill" → cost weight
- "battery longevity" → cycling / depth-of-discharge penalty, or a cycle budget
- "never lose power in a blackout" → minimum-reserve hard constraint
- "I hate exporting for free" → penalty on low-FIT export
- the **$500-variable-vs-$300-certain calibration question elicits a risk-aversion coefficient** — and that coefficient becomes the *quantile* the robust MPC optimises against (point 4 above). This is the synthesis of the uncertainty and personalisation problems: **intent decides how conservatively the engine plans against the same forecast uncertainty.**

Intent is data, not code. Same optimiser, personalised objective. (This deepens the existing `From conversation to solver weights` table — those weights *are* the objective coefficients.)

**Layer 2 — Devices → System model.**
Each device type is a plugin implementing a common interface (state, constraints, control mapping): battery (capacity, kW, efficiency, degradation, control protocol), solar (capacity, orientation, inverter limit, forecast source), EV (capacity, charge rate, departure schedule, charger modes), flexible loads (power, thermal model, comfort constraints). Adding a Sonnen battery or a heat pump is **writing a driver, not touching the optimiser.** The optimiser composes whatever devices are present into one model. (This is the "Multi-battery adapter layer" made first-class.)

**Layer 3 — Tariff/grid → Price + constraint model.**
A function `time → (import_price, export_price, structural constraints)`. Amber = real-time feed; Octopus Agile = day-ahead; flat ToU = static schedule; demand charges = a monthly-peak term. One interface, many implementations.

**The universal core:** `optimise(objective, system_model, price_model) → control commands`. Identical code for every tenant. All personalisation lives in the three input models.

### The self-learning loops

Learning happens at three levels, kept **deliberately separate** (mixing them is how you get instability):

| Loop | Scope | Cadence | What it learns |
|------|-------|---------|----------------|
| **Calibration** | per-site | continuous | solar bias, household load shape, real battery efficiency vs nameplate, price-error buckets — improves the *inputs* to the optimiser. ~80% of "self-learning"; mostly online regression / Bayesian updating. Highest ROI, lowest risk. |
| **Fleet / cohort** | cross-tenant | daily/weekly | priors that transfer between similar sites. A new flat-roof Glebe Powerwall inherits similar sites' models (solves cold-start). Hierarchical: global prior → cohort (region / hardware / tariff) → individual. The **network-effect moat**: more users → better priors → better onboarding → more users. (Privacy: learn aggregate patterns without centralising raw data; federate where possible.) |
| **Meta / analyst** | system-wide | weekly, human-gated | the "analyst agent" — reviews outcomes vs counterfactual baselines, surfaces *systemic* issues ("this cohort over-charges on cloudy days", "elicited risk looks too conservative vs outcomes"), proposes changes to objective-construction or priors → human review → deploy. Meta-learning about the system, kept out of the real-time loop so it can't destabilise live control. |

### Two non-negotiable design principles

1. **Learning never touches safety.** Learned components feed *forecasts and objective weights* only. The *hard constraints* — demand window, deep-discharge floor, blackout reserve — are never learned; they are guarantees the optimiser operates within. This preserves the existing three-layer split (**intent → optimiser → safety rules**): the safety layer stays deterministic and per-device-certified, and learning lives strictly above it. Learning optimises *within* a safe envelope; it can never widen it.

2. **Validate learning the way you validate code: shadow mode.** The repo-root agent's shadow-then-cutover-with-kill-switch discipline (log a new decision layer's verdict alongside the incumbent, measure divergence, cut over behind a revertible flag) generalises directly to a fleet: every new model/optimiser version runs in shadow against the incumbent across tenants, you measure regret per cohort, and roll out gradually per cohort with kill-switches. The validation pattern already exists; it just runs fleet-wide.

### Where the LLM lives in the target architecture

The LLM is repositioned, not removed. It is genuinely good at four things the optimiser is not, and dropped from the one place it doesn't belong:

- **Elicitation** — intent → objective (the differentiator). Per user, at onboarding and periodic re-check.
- **Explanation / trust** — "we charged at 1 pm and saved you $34 this week, here's how." What makes a paid service feel worth it (see the savings-dashboard work in `todo.md`).
- **Graceful degradation** — on sensor/API failure it holds safely and explains, where a bare optimiser needs explicit fallback logic.
- **Meta / analyst loop** — reading logs and proposing hypotheses for human review.
- **Not** the per-cycle optimiser — that is the LP / MPC.

### The synthesis

> A **universal robust-MPC core**, fed by **three per-user models** (intent→objective, devices→dynamics, tariff→prices), where **per-site calibration** continuously learns each system's forecast-error signature, **the user's elicited risk tolerance sets how conservatively the MPC plans against that uncertainty**, and **fleet learning** supplies priors for cold-start — all validated in shadow and operating strictly inside a non-learned safety envelope.

The solar-inaccuracy problem and the multi-tenant problem are the same problem at two scales: **each site learns its own truth, and intent decides how much to trust it.**

### Migration path (from today's repo-root agent)

1. **Now — improve the current rule layer** (low risk): give the deterministic layer an absolute-cheap floor at low SoC, next-day peak-demand lookahead, control-write hysteresis to stop reserve thrashing, and a load-forecast-derived demand-window target instead of a fixed 85%.
2. **Next — prototype the optimiser** (`agent/optimizer.py`): a small LP/MPC reading the same state + forecasts, logging an `optimizer_verdict` alongside the existing `computed_verdict` and the LLM's decision. Run all three in shadow on live June peak-week data — zero control-path risk.
3. **Cut over** the optimiser behind a kill-switch once shadow data supports it; it subsumes the entire rule stack (cheap-window, demand deadline, Solar Sponge floor, spread table, historical price model) in one stroke.
4. **Foundational — Tesla Fleet API** to replace the `backup_reserve_percent` hack with direct charge/export commands, deleting the export safety-net and the control-quantisation layer (and the Tessie fee).
5. **Productise** the three-model separation and the three learning loops into the multi-tenant Sol service.

---

## Roadmap

### Phase 1 — Personal system (current)
- [x] Rule-based automation for single site
- [x] Solcast solar forecasting integration
- [x] Amber dynamic pricing integration
- [x] Autonomous mode (time_based_control) for aggressive grid charging
- [ ] EV charging automation (Rules 4 & 5)
- [ ] Load shedding during demand window (Daikin AC, smart plugs)

### Phase 2 — Formalise goal layer
- [ ] Define goal schema (YAML or structured config)
- [ ] Separate goal definition from rule implementation
- [ ] Make loss aversion weights explicit and tunable

### Phase 3 — Solver
- [ ] Replace hand-crafted rules with LP/MPC solver
- [ ] Integrate Solcast + Amber forecasts as solver inputs
- [ ] Implement rolling horizon re-optimisation (every 30 min)
- [ ] Load forecast model (historical patterns + weather)

### Phase 4 — Multi-tenant platform
- [ ] Tesla Fleet API (direct, no Tessie dependency)
- [ ] Multi-battery adapter layer (devices → system model)
- [ ] Multi-tariff constraint model (tariff → price model)
- [ ] User goal elicitation UI (intent → objective)
- [ ] Savings reporting and verification

### Phase 5 — Self-learning (see Optimisation Engine — Depth)
- [ ] Per-site calibration: solar bias / load shape / battery efficiency / price-error buckets from logged actuals
- [ ] Robust MPC: quantile forecasts + risk-tuned objective (intent sets the quantile)
- [ ] Fleet/cohort priors for cold-start onboarding
- [ ] Meta/analyst loop: human-gated systemic improvement
- [ ] Fleet-wide shadow validation + per-cohort kill-switch rollout

---

## Notes from Amber Engineering (May 2026)

Amber's team reviewed site data 8–15 May and identified SmartShift's load model was underestimating actual evening consumption, leaving the battery depleted entering the evening peak.

Their engineering team expressed interest in HA-based solver approaches, specifically asking about:
> *"constraints/tolerances with respect to loss aversion/weighting if you get into 'solver' style solutions that use forecast prices/loads"*

Key response points:
1. Loss function must be asymmetric — demand charge violation is far more costly than over-charging
2. Demand ratchet creates non-convex objective — hard zero-import constraint is the pragmatic solution
3. Load forecast uncertainty is the hardest sub-problem — quantile forecasting (P90) recommended over point estimates
4. MPC with 30-min re-optimisation cadence matches Amber's forecast update frequency naturally
