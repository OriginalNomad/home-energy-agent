# Self-Learning Energy Optimisation — Architecture

*Written 2026-06-05. Captures the full proposed architecture for evolving the current
Claude-agent system into a self-learning agentic AI optimiser. Read alongside
`PRODUCT.md` (multi-tenant product design) and `CONTEXT.md` (current live system).*

---

## What exists today (2026-06-09)

The system is further along than the original design implied. Three Python layers are running
on the Pi right now, every 30 minutes:

### Layer A — Deterministic rule layer (IN CONTROL)
`compute_decision_context()` + `_execute_deterministic_verdict()` in `energy_agent.py`.

A pure Python `if/elif` rule tree. Reads current state (SoC, price, forecast, time, peak month
flag), computes a verdict (`charge / hold`, target SoC, mode, rule name), and **executes it** —
calls the Tessie and HA APIs directly. No ML, no LLM. This is the control path.

Kill-switch: `DETERMINISTIC_AUTHORITATIVE = False` reverts to LLM control.

### Layer B — LLM narrative logger (COSMETIC ONLY)
Claude runs after the deterministic layer each cycle. It calls `get_current_state()` and
`get_price_forecast()`, reads the shadow block showing what was just executed, and calls
`log_decision()` with a plain-English explanation. Its `set_*` tool calls are no-ops.
The prompt is ~65 lines (slimmed from 470 in Phase 6 — all decision arithmetic removed).

### Layer C — LP optimiser / MPC (SHADOW, not in control path)
`optimizer.py` — a receding-horizon linear program (MPC). Runs every cycle, computes what it
*would* have done, logs to `decisions.jsonl` alongside the deterministic verdict. Not yet
in the control path. Comparison data accumulates for Phase 5 cutover validation.

### Layer 0 — HA automations (ALWAYS ACTIVE, safety net)
~12 Home Assistant automations that fire independently of the agent. Demand window guard,
export safety net, autonomous revert. These cannot be overridden by any layer above.

---

### How Layer A and Layer C differ

**Deterministic layer (Layer A)** is a hand-coded `if/elif` decision tree. It matches
the current situation against named rules in priority order — first rule that fires wins.
Rules encode explicit business logic accumulated from observed failure modes: deadline
maths, Solar Sponge timing, fill-time projections, price threshold comparisons.

- **Strength**: transparent, testable (109 unit tests), never surprises you. Each rule
  has a name (`peak_early_morning_hold`, `wait_for_cheap_go_hard`) that appears in the
  log — you always know exactly why it did what it did.
- **Weakness**: rules are brittle approximations. The right answer to "should I charge
  now?" is really "what's the cheapest sequence of actions over the next N hours?" — and
  a rule tree can only approximate that with fixed thresholds and heuristics.

**LP optimiser (Layer C)** is a receding-horizon linear program (LP = Linear Programming).
It formulates the next ~22 hours as a cost minimisation problem: decision variables are
charge/discharge amounts per 30-min slot, constraints are SoC bounds and demand window
limits, objective is minimise total electricity cost. It finds the globally cheapest
*sequence* of decisions rather than the cheapest *next action*.

- **Strength**: mathematically optimal given the forecast. Trade-offs that require
  explicit rules in Layer A (e.g. "charge a bit now to avoid importing at a spike later")
  emerge naturally from the objective without any special-casing.
- **Weakness**: only as good as its inputs. Amber's ~6h horizon, the synthetic price
  extension (7-day medians), and solar forecast uncertainty are all approximations. It
  also doesn't yet replicate a few edge-case protections the det layer learned the hard
  way (survival checks, sliding forecast detector, deferral limit).

**Why both**: the LP is the right long-term architecture. The deterministic layer encodes
constraints and hard-won edge cases that are difficult to express as LP penalties — and
where getting them wrong is costly (demand window breach, Tessie API quirks). The plan is
to track LP divergence in `decisions.jsonl`, get it below ~5% on peak-day decisions, then
cut over with the det layer as a safety backstop.

---

```
Every 30 min cycle:
  ┌──────────────────────────────────────────────────┐
  │  Layer C: LP optimiser   →  logs verdict (shadow) │
  │  Layer A: Det rule layer →  executes verdict ✓    │
  │  Layer B: LLM            →  writes narrative log  │
  └──────────────────────────────────────────────────┘
  Layer 0: HA automations — always active, independent
```

**All three control layers are Python.** No ML in the control path yet — the self-learning
models (Layer 2 below) will feed calibrated inputs into the LP optimiser when built.

---

## Background and motivation

The current system (`agent/energy_agent.py`) runs every 30 minutes, reads sensor state
from Home Assistant, and uses Claude to reason about whether to adjust the Powerwall
reserve, mode, or Zappi EV charger. It works well but has a fundamental limitation:
**every cycle starts from scratch**. The agent reasons intelligently but never improves.
It consumes forecasts as given and never learns that this specific site's Solcast readings
systematically overestimate on overcast mornings, or that the Powerwall charges at 2.8 kW
(not 5 kW) when SoC is above 80%.

This document describes an architecture where the system accumulates that knowledge and
feeds it back into better decisions. The key insight is that self-improvement doesn't
require deep learning or reinforcement learning — it requires **closing the loop**: log
what was forecast, log what actually happened, and fit simple calibration models nightly.

---

## The three learning gaps

Before describing the target architecture, here are the specific failure modes the
self-learning layer is designed to fix:

| Gap | Symptom | Root cause |
|-----|---------|------------|
| **Solar forecast bias** | Grid charging starts too late on cloudy days; battery misses 85% SoC by 2:55pm | Solcast treated as ground truth; Rule 11 only downgrades the forecast *mid-cycle*, never before |
| **Battery charge rate assumption** | "Will I reach 85% by 2:55pm?" projection is wrong; autonomous mode fires too late or too early | Agent assumes flat 1.7 kW (self_consumption) or 5 kW (autonomous); actual rate varies with SoC and temperature |
| **Price forecast accuracy** | Cheap-window decisions made on forecasts that are systematically biased at 6-12h horizon | Amber's short-horizon (30min) forecasts are accurate; longer-horizon forecasts drift — but no correction applied |
| **Home load unknown** | Demand window risk scored crudely; AC load on hot afternoons not anticipated | No learned load model; instantaneous 30-min average doesn't capture day-of-week or temperature patterns |

---

## System architecture overview

```
┌──────────────────────────────────────────────────────────────────────┐
│  LAYER 4 — Claude meta-agent                                         │
│  Supervises MPC plan, handles anomalies, explains decisions,         │
│  surfaces calibration insights from nightly outcome review           │
└──────────────────────────┬───────────────────────────────────────────┘
                           │ validated schedule
┌──────────────────────────▼───────────────────────────────────────────┐
│  LAYER 3 — MPC scheduler                                             │
│  Optimises charge/discharge schedule over rolling 24h horizon.       │
│  Re-solves every 30 min on new forecast data. Hard constraints        │
│  (demand window, SoC floor) are enforced in the solver itself.        │
└──────────────────────────┬───────────────────────────────────────────┘
                           │ calibrated forecasts + uncertainty bounds
┌──────────────────────────▼───────────────────────────────────────────┐
│  LAYER 2 — Self-calibrating forecast models                          │
│  Solar corrector · Charge rate model · Price corrector · Load model  │
│  Retrained nightly from closed-loop log. Feed MPC corrected inputs.  │
└──────────────────────────┬───────────────────────────────────────────┘
                           │ raw forecasts + actuals
┌──────────────────────────▼───────────────────────────────────────────┐
│  LAYER 1 — Closed-loop data logger                          BUILT ✓  │
│  SQLite DB (agent/energy_log.db). One row per agent cycle.           │
│  Captures: all sensor state, Amber forecast slots, agent decision.   │
└──────────────────────────┬───────────────────────────────────────────┘
                           │ sensor reads + API calls
┌──────────────────────────▼───────────────────────────────────────────┐
│  LAYER 0 — Hard constraint automations  (HA, always active)          │
│  Demand window guard · Export safety net · Emergency SoC floor       │
│  Fire deterministically in seconds. Cannot be overridden by agent.   │
└──────────────────────────────────────────────────────────────────────┘
```

Layers 0 and 1 are built. Layers 2–4 are the target architecture.

---

## Layer 0 — Hard constraint automations (current, unchanged)

12 active HA automations that fire independently of the agent. These are the safety net
that makes the higher layers safe to experiment with. Key ones:

| Automation | What it does |
|-----------|-------------|
| `battery_pre_demand_window_reset` | Sets reserve to 5% at 2:55pm every peak-month day |
| `battery_autonomous_export_safety_net` | Reverts to self_consumption within 30s if export detected in autonomous mode |
| `battery_autonomous_revert_target_reached` | Reverts to self_consumption when charge target is hit |
| `battery_negative_price_charge` | Charges to 100% on negative Amber spot price |
| `battery_demand_window_low_warning` | Alerts on low SoC during demand window |

These never change regardless of what the optimiser layer does. They are the floor below
which nothing bad can happen.

---

## Layer 1 — Closed-loop data logger

**Status: built and wired in** (`agent/data_logger.py`, wired into `energy_agent.py` 2026-06-06). Phase 2.5-A clock started — 1 week of data needed to build the charge rate model.

### What it writes

SQLite database at `agent/energy_log.db`. Two tables:

**`observations`** — one row per agent cycle (~30 min cadence)

| Column | Type | Notes |
|--------|------|-------|
| `ts` | TEXT | ISO8601 Sydney local time |
| `month`, `hour`, `day_of_week` | INTEGER | For load and solar models |
| `is_peak_month`, `in_demand_window`, `in_solar_sponge` | 0/1 | Context flags |
| `battery_soc_pct`, `battery_mode`, `battery_reserve_pct` | REAL | Powerwall state |
| `battery_grid_target_pct` | REAL | Computed target from solar shortfall formula |
| `grid_price_cents`, `in_cheap_window` | REAL/0/1 | Amber spot price |
| `solar_actual_kw` | REAL | SolarEdge inverter actual output |
| `solcast_power_now_kw`, `solcast_this_hour_kwh`, `solcast_next_hour_kwh` | REAL | Solcast forecasts |
| `solcast_remaining_kwh` | REAL | Remaining today forecast |
| `forecast_accuracy` | TEXT | good / poor / unreliable label |
| `home_load_kw` | REAL | 30-min rolling average |
| `ev_plugged`, `ev_soc_pct`, `ev_zappi_mode` | mixed | EV state if connected |
| `action_summary` | TEXT | Agent's reasoning (filled by log_decision) |
| `actions_taken_json` | TEXT | JSON array of actions taken |

**`price_forecasts`** — one row per Amber forecast slot, linked to the observation

| Column | Notes |
|--------|-------|
| `observation_id` | FK to observations |
| `slot_ts` | ISO8601 start of forecast slot |
| `horizon_slots` | How many 30-min slots ahead (1 = next slot) |
| `forecast_cents` | Amber forecast price for this slot |
| `descriptor` | Amber label (spike, high, etc.) |

### Diagnostics

```bash
cd agent && python data_logger.py
```

Prints: row count, date range, undecided rows (cycles that crashed before log_decision),
price forecast slot count, and most recent 5 cycles with SoC/price/solar/mode/decision.

### Why SQLite and not InfluxDB

InfluxDB (already running for HA sensor history) is a time-series store — good for "what
was the battery SoC at 14:32?" queries. The self-learning models need *paired observations*:
"what did Solcast forecast for hour H, and what did the inverter actually produce?" That
is a relational join (observation row + price forecast row matched by timestamp), which
SQLite handles naturally. InfluxDB would require awkward cross-measurement joins.

---

## Layer 2 — Self-calibrating forecast models

Four models, all lightweight (sklearn or numpy). Retrained nightly. Feed corrected
inputs into the MPC solver rather than raw forecasts.

### Model 1 — Solar forecast corrector

**What it fixes**: Solcast overestimates generation on partly cloudy days for this specific
flat-roof site. The agent currently uses `forecast_remaining_kwh` raw, which causes it to
underestimate the grid charge needed on marginal days.

**Approach**: OLS regression fit on last 30 days of observations:

```
actual_kw = α × solcast_this_hour_kwh + β(hour_of_day) + γ(month) + ε
```

**Outputs**:
- `solar_corrected_kw` — bias-adjusted estimate for each forecast slot
- `solar_uncertainty_kw` — residual std at this hour/month combination

**MPC input**: corrected solar profile with uncertainty bounds. Wider uncertainty = agent
charges more conservatively from grid earlier in the day, rather than waiting for solar
that may not arrive.

**When to build**: after 2 weeks of logged data (need enough observations across different
forecast accuracy levels to fit the regression reliably).

**Key query**:
```sql
SELECT hour, month,
       AVG(solar_actual_kw / NULLIF(solcast_this_hour_kwh, 0)) AS ratio,
       STDDEV(solar_actual_kw / NULLIF(solcast_this_hour_kwh, 0)) AS uncertainty
FROM observations
WHERE solcast_this_hour_kwh > 0.2   -- exclude night readings
  AND ts >= date('now', '-30 days')
GROUP BY hour, month
```

---

### Model 2 — Battery charge rate model

**What it fixes**: The agent assumes flat charge rates (1.7 kW in self_consumption,
5 kW in autonomous). The Powerwall actually charges at ~3 kW at 50% SoC, ~1.5 kW at
85%, and ~0.8 kW above 90%. This means "time to reach 85%" projections are consistently
wrong, and autonomous mode fires too late on days when it matters most.

**Approach**: lookup table binned by SoC decile × mode. Updated each cycle from new
observations by computing delta_soc / elapsed_time from consecutive rows where mode
didn't change:

```sql
SELECT
    (CAST(a.battery_soc_pct / 10 AS INT) * 10) AS soc_bucket,
    a.battery_mode,
    AVG((b.battery_soc_pct - a.battery_soc_pct) / 0.5) AS avg_charge_rate_kw
FROM observations a
JOIN observations b ON b.id = a.id + 1
WHERE b.battery_soc_pct > a.battery_soc_pct   -- charging, not discharging
  AND a.battery_mode = b.battery_mode          -- mode didn't change mid-interval
GROUP BY soc_bucket, a.battery_mode
```

**Output**: a 10×2 table (10 SoC buckets × 2 modes) of observed charge rates.

**MPC input**: `charge_rate(soc, mode)` — used to compute realistic "minutes to reach
target SoC" projections. Replaces the flat 1.7 kW / 5 kW assumptions.

**When to build**: after 1 week of logged data. This is the **highest-value model** —
the "will I make 85% by 2:55pm?" calculation is the most consequential estimate in the
whole system.

---

### Model 3 — Price forecast corrector

**What it fixes**: Amber's 30-min ahead price forecasts are quite accurate. At 6-12 hour
horizons they drift, and the drift is not symmetric — Amber tends to underestimate price
spikes and overestimate the duration of cheap windows.

**Approach**: for each horizon length (1, 2, 4, 6, 12 slots ahead), compute:

```
bias(horizon) = mean(amber_forecast_cents - actual_cents)
error_std(horizon) = stddev(amber_forecast_cents - actual_cents)
```

This requires matching forecast slots to actuals. The `price_forecasts` table stores
`slot_ts` — join against `observations.grid_price_cents` at the matching timestamp.

**Output**: bias correction and uncertainty bounds by horizon. The MPC uses wider
uncertainty intervals for slots > 4 hours ahead.

**When to build**: after 4 weeks of data (need a spread of price conditions to fit the
error distribution meaningfully). Lower priority than models 1 and 2.

---

### Model 4 — Home load model

**What it fixes**: the agent currently uses a 30-min rolling average of home load
(`sensor.home_load_30min_average`). This is reactive — it doesn't anticipate that load
will jump at 6pm when cooking starts, or that a hot afternoon means AC will add 3 kW
from 2pm onwards. The demand window risk estimate is therefore always a lagged snapshot,
not a forward-looking projection.

**Approach**: time-series regression with features:

```
load_kw = f(hour_of_day, day_of_week, month, temperature)
```

The temperature term is the most important feature for this site — Daikin AC load
correlates strongly with afternoon temperature. Requires a temperature feed: either a
BOM weather integration in HA or an API call to Open-Meteo.

**Output**: `load_forecast_kw[t]` for each slot in the MPC horizon, with P90 bound
(not point estimate — demand charge risk means we plan for higher-than-average load).

**When to build**: once a temperature sensor or API is available. Medium priority.

---

### Nightly retraining cycle

All models are retrained at 2am by a cron job:

```
0 2 * * *  /path/to/venv/bin/python /path/to/agent/train_models.py
```

`train_models.py` (to be built):
1. Opens `energy_log.db`
2. Queries last 30 days of observations and price_forecasts
3. Fits each model, saves params to `agent/model_params.json`
4. Logs a retraining summary (row count, model accuracy, biggest forecast errors)
5. The meta-agent reads this summary on its next cycle and surfaces any anomalies

`model_params.json` is committed to git so the Pi and desktop stay in sync.

---

## Layer 3 — MPC scheduler

### Why MPC

The current Claude agent is a greedy reasoner — it looks at current state and the next
few hours and decides what to do now. MPC (Model Predictive Control) solves the full
24-hour scheduling problem at once: given forecast solar, forecast prices, current SoC,
and all constraints, find the charge/discharge schedule that minimises total cost.

The difference matters most for multi-step tradeoffs:
- Should I charge the battery at 14¢ now, or wait for 10¢ solar in 2 hours?
- If I discharge at 3pm for arbitrage, will I have enough for the 6pm demand window?
- Should I charge the EV now (grid at 12¢) or defer to overnight (forecast 8¢)?

A greedy agent can reason about these but can't *solve* them jointly. MPC can.

### Objective function

```
minimise:
  Σ_t [ grid_import(t) × price(t) ]          # import cost
  − Σ_t [ grid_export(t) × feedin(t) ]        # export revenue  
  + demand_penalty × max_30min_import          # demand charge risk
  + cycle_cost × Σ_t |Δsoc_battery(t)|        # battery wear

subject to:
  soc_battery(t+1) = soc_battery(t)
                   + charge_rate(soc(t), mode(t)) × Δt   # Model 2
                   − discharge_rate(t) × Δt
                   − home_load_forecast(t) × Δt            # Model 4
                   + solar_corrected(t) × Δt               # Model 1

  soc_battery(t)   ∈ [reserve_floor(t), 100%]
  charge_rate(t)   ≤ charge_rate_model(soc(t), mode(t))
  grid_import(t)   = 0 during demand window (peak months)    # hard
  soc_battery(end_demand_window) ≥ buffer_for_remaining_load # soft, high weight
  soc_ev(departure_time) ≥ ev_target_soc                     # hard
```

### The asymmetric loss function

The demand charge is a ratchet: one 30-minute interval of grid import during 3–9pm in
a peak month sets the monthly charge regardless of how many intervals were clean. This
makes the loss function non-convex and asymmetric:

- Battery depleted during demand window → demand ratchet → potentially 2× monthly bill (~$100)
- Battery over-charged from grid → just the cost of those kWh (a few dollars at worst)

The `demand_penalty` weight must be set **much** higher than a naive cost minimisation
would suggest. Start at 500× the per-kWh import cost and tune from observed behaviour.

The demand baseline resets on the 1st of each peak month, so the risk is highest at the
start of the month (first violation sets the baseline) and lower late in the month (a
second violation at the same level costs nothing more).

### Price spike arbitrage (Rule 10)

When Amber export forecast exceeds 50¢/kWh, MPC handles this naturally: the export
revenue term in the objective dominates, and the solver chooses to hold charge until the
peak arrives, then discharge. The "don't discharge early" logic that currently requires
explicit rules is implicit in the look-ahead.

The constraint is: during the demand window, maintain enough SoC to cover remaining
home load — can't risk going flat before 9pm chasing arbitrage revenue.

### Implementation

The solver runs in Python using `scipy.optimize.linprog` (for the LP relaxation) or
`pyomo` (for mixed-integer if EV scheduling requires it). Solves a 48-slot (24h) horizon
in under 1 second on the Pi.

`energy_agent.py` integration: the agent calls `mpc_solver.solve(state, models)` at the
start of each cycle to get the recommended action for the current slot, then validates it
(Layer 4 meta-agent check) before executing.

The HA safety automations (Layer 0) remain active and override MPC output if a hard
constraint is violated — the MPC should never trigger them, but they're there as backstops.

---

## Layer 4 — Claude meta-agent (refocused)

Once MPC is running, the Claude agent's role shifts from *making* every decision to
*supervising* the MPC plan and handling cases where the solver's world model is wrong.

### Current role vs. refocused role

| Current (greedy decision-maker) | Refocused (MPC supervisor) |
|--------------------------------|---------------------------|
| Reasons about charge mode and reserve every 30 min | Validates MPC schedule against real-time context |
| Reacts to forecast accuracy mid-cycle | Detects anomalies: inverter fault, unexpected price spike, EV plugged in early |
| Stateless — no memory between cycles | **Stateful**: reads outcome log, surfaces calibration observations |
| Logs decision narrative to HA | Explains MPC schedule in plain English for the HA dashboard |
| Handles all strategic decisions alone | Focuses on edge cases and anomalies the solver didn't anticipate |

### What the meta-agent watches for

**Anomalies that override MPC:**
- `forecast_accuracy = unreliable` at 10am in a peak month → override MPC, charge to 85%
  immediately regardless of what the solar corrector says
- Inverter power < 200W when Solcast says > 500W for 2+ consecutive cycles → inverter
  fault, treat as zero-solar day
- Amber spot price spikes beyond what the forecast showed → re-solve with updated prices

**Calibration observations (written to nightly retraining log):**
- "Solar corrector predicted 3.2 kW, actual was 1.1 kW — 3 consecutive cycles. Weight
  recent overcast observations more heavily in next retrain."
- "Charge rate model predicted 4.2 kW at 60% SoC in autonomous mode, actual was 2.9 kW.
  This SoC bucket needs re-sampling."

**Plain-English explanation for HA dashboard:**
Each cycle the agent writes a one-sentence explanation of the MPC schedule to the
`input_text.battery_decision_action` helper entity, visible on the HA dashboard. This is
the primary debugging tool — if something looks wrong, the explanation shows whether it's
the MPC, the models, or a sensor issue.

---

## Implementation roadmap

### Phase 2.5-A: data collection (now → 2 weeks)

The data logger is running. Let it accumulate. Nothing to build.

**At the 1-week mark:**
- Build and deploy the **charge rate model** (Model 2)
- It only needs 1 week of data and is the highest-value fix
- A simple Python script that queries the DB, computes the lookup table, writes
  `agent/model_params.json`, and the agent reads it on startup

**At the 2-week mark:**
- Build the **solar forecast corrector** (Model 1)
- OLS fit on `observations` where `solcast_this_hour_kwh > 0.2`
- Verify the fit makes sense before plugging into the agent

### Phase 2.5-B: nightly retraining (2-4 weeks)

- Build `agent/train_models.py` — the nightly retraining script
- Cron it at 2am alongside the agent
- Models 1 and 2 are retrained; model params committed to git (or stored in DB)
- Add a "model accuracy" section to the nightly outcome summary that the meta-agent reads

**At the 4-week mark:**
- Build the **price forecast corrector** (Model 3) — enough data to fit error distribution
- This is lower priority; Amber is already fairly accurate short-term

### Phase 3: MPC solver

- Implement `agent/mpc_solver.py` using `scipy.optimize.linprog`
- Start with the battery-only problem (no EV) to keep it tractable
- Hard constraints: demand window, SoC floor
- Soft constraints: 85% by 2:55pm (high weight), battery wear (low weight)
- Run MPC in shadow mode first: let Claude agent keep making decisions, log what MPC
  *would have* decided, compare outcomes over 2 weeks before switching over
- Once validated, replace the Claude greedy loop with MPC + meta-agent supervision

### Phase 4: home load model + EV scheduling

- Add temperature feed (Open-Meteo API or BOM HA integration)
- Build load model (Model 4) once 4+ weeks of temperature-correlated load data exists
- Extend MPC to include EV charging as a schedulable load with departure deadline
- This fully replaces the current Zappi mode logic

---

## File structure (target)

```
agent/
  energy_agent.py       # Main agent — will become MPC supervisor in Phase 3
  data_logger.py        # Layer 1 — closed-loop SQLite logger  ✓ BUILT
  train_models.py       # Layer 2 — nightly model retraining   (Phase 2.5-B)
  mpc_solver.py         # Layer 3 — MPC optimisation engine     (Phase 3)
  model_params.json     # Layer 2 — calibrated model params     (Phase 2.5-A)
  energy_log.db         # Layer 1 — operational data (gitignored)
  .env                  # API keys (gitignored)
  agent_decisions.log   # Human-readable decision log (gitignored)
```

---

## Key design decisions and trade-offs

### Why not reinforcement learning?

RL could theoretically learn an optimal policy without the intermediate modelling step.
It's not appropriate here because:

1. **Data rate**: this site generates ~1 episode/day (one demand window, one solar day).
   Training a useful RL policy requires thousands of episodes — that's years of real data.

2. **Exploration cost**: RL requires exploring suboptimal actions to learn. On this site,
   a single exploratory action during the demand window could cost $100. Constrained RL
   (safe RL) exists but is significantly more complex to implement and validate.

3. **Hard constraints**: the demand window zero-import constraint is provably enforced in
   MPC (it's an explicit solver constraint). In RL it's a penalty term that the policy
   can still violate, especially early in training.

MPC with learned calibration models achieves the same goal — improving over time as the
system learns the site's characteristics — without these risks.

### Why not a larger / always-on LLM?

The current Claude agent already reasons well about the tradeoffs. The bottleneck is not
reasoning quality — it's that Claude has no *calibrated model of how this specific site
behaves*. Giving Claude more context (e.g. the last 30 days of logs) helps marginally,
but it can't substitute for a proper optimizer that evaluates thousands of scenarios in
the MPC solve. The LLM role is anomaly detection and explanation, not numerical
optimisation.

### Why keep Layer 0 (HA automations)?

The hard constraint automations (demand window guard, export safety net) fire in seconds
via HA event loop. The agent runs every 30 minutes. There's a window where the agent
could set an unsafe state and nothing corrects it for up to 29 minutes. Layer 0 closes
that window. They also provide an independent safety check during the transition from the
current greedy agent to MPC — if MPC makes an error, Layer 0 catches it.

### Raspberry Pi constraints

The Pi 4 has enough compute to run the MPC solver (LP over 48 slots solves in < 1s with
`scipy`). The main constraints are:

- **SD card wear**: SQLite WAL mode is used to batch writes and reduce flash wear.
  At 30-min cadence, write volume is very low (< 1 KB/cycle).
- **Memory**: sklearn models for 30 days × 48 slots/day × 4 features are tiny (<1 MB).
  `scipy.optimize.linprog` peak memory for a 48-slot LP is under 10 MB.
- **No GPU needed**: all models are linear/tabular. No neural networks.

---

## Monitoring and debugging

### Is the logger running?

```bash
cd agent && python data_logger.py
```

Check: `Undecided` count should be 0 or small. If it's growing, the agent is crashing
before calling `log_decision` — check `agent_decisions.log` for errors.

### Is the solar corrector working?

```sql
-- Compare raw Solcast vs corrected forecast accuracy by month
SELECT month, hour,
       AVG(solar_actual_kw) as avg_actual,
       AVG(solcast_this_hour_kwh) as avg_forecast,
       AVG(solar_actual_kw / NULLIF(solcast_this_hour_kwh, 0)) as ratio
FROM observations
WHERE solcast_this_hour_kwh > 0.2
GROUP BY month, hour
ORDER BY month, hour;
```

A ratio consistently < 0.7 in certain hours/months indicates the corrector has something
to learn. A ratio near 1.0 across all hours means Solcast is accurate for this site.

### Did we trigger a demand charge?

```sql
-- Any cycles during demand window with grid import (price spike would show high price)
SELECT ts, battery_soc_pct, grid_price_cents, action_summary
FROM observations
WHERE in_demand_window = 1
  AND is_peak_month = 1
  AND battery_soc_pct < 10   -- low SoC during demand window is a warning sign
ORDER BY ts;
```

### Charge rate model accuracy

```sql
-- Compute actual charge rate from consecutive rows and compare to model assumption
SELECT
    (CAST(a.battery_soc_pct / 10 AS INT) * 10) AS soc_bucket,
    a.battery_mode,
    ROUND(AVG((b.battery_soc_pct - a.battery_soc_pct) / 0.5), 2) AS avg_rate_kw,
    COUNT(*) as n_samples
FROM observations a
JOIN observations b ON b.id = a.id + 1
WHERE b.battery_soc_pct > a.battery_soc_pct
  AND a.battery_mode = b.battery_mode
GROUP BY soc_bucket, a.battery_mode
ORDER BY soc_bucket, a.battery_mode;
```

---

## Connection to Sol (multi-tenant product)

This architecture is a single-site instance of the Sol product design in `PRODUCT.md`.
The mapping is direct:

| This system | Sol product |
|-------------|------------|
| `data_logger.py` | Per-site observation store (cloud DB, one partition per site) |
| Layer 2 models | Per-site calibration models, retrained nightly per site |
| MPC solver | Cloud-hosted solver, one solve per site per 30-min tick |
| Claude meta-agent | Sol's reasoning/alerting layer — explains decisions, surfaces anomalies |
| HA Layer 0 automations | Per-device safety adapter (battery-specific firmware constraints) |
| `model_params.json` | Site model parameters, stored in Sol's config layer |

The three-layer architecture (intent → optimiser → safety rules) is preserved at product
scale. The self-learning layer is the differentiator: Sol improves per-site over time
without manual tuning, which commodity competitors (Amber SmartShift, fixed-rule systems)
cannot do.
