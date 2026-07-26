# Home Energy Automation — Session Context

*Read this at the start of every session. Then read any files referenced below that are relevant to what you're working on.*

---

## What this project is

A Home Assistant-based battery optimisation system for a single residential site in Glebe, Sydney. It controls a Tesla Powerwall 2 using price forecasts (Amber Electric dynamic tariff) and solar forecasts (Solcast) to minimise electricity bills and avoid network demand charges.

This is also the personal testbed for **Sol** — a multi-tenant battery optimisation product being built at `/Users/simonmonk/Simon Projects/Home Energy Console/`. The rules and architecture here will eventually be replaced by Sol's MPC solver.

---

## The site

| Hardware | Detail |
|----------|--------|
| **Battery** | Tesla Powerwall 2, 13.5 kWh usable, ~5 kW charge/discharge |
| **Solar** | SolarEdge inverter, ~5 kW peak |
| **EV** | Polestar 4 (~100 kWh), charged via Zappi 2 |
| **AC** | Daikin, 3 zones, ~3.5 kW max load |
| **Tariff** | Amber Electric, Ausgrid EA116 |
| **Location** | Glebe, Sydney (grid: Ausgrid) |

**Key tariff facts (EA116):**
- Demand charge applies **Nov, Dec, Jan, Feb, Mar, Jun, Jul, Aug** — 3–9pm daily
- Off-peak months (no demand charge): **Apr, May, Sep, Oct**
- Solar Sponge window: **10am–3pm** (cheapest grid import)
- Export penalty if export during 10am–3pm exceeds threshold

---

## How the system works

**Control mechanism**: `backup_reserve_percent` via Tessie REST API (the only writable Powerwall parameter available without Tesla Fleet API access).
- Set to `100%` → Powerwall charges toward full
- Set to `20%` → normal floor, self-consumption mode
- Set to `5%` → deep discharge floor during demand window (peak months only)

**Known limitation**: Cannot command a specific charge rate. Tesla's firmware decides how aggressively to pull from grid in `self_consumption` mode — typically conservative. This is why the battery sometimes doesn't reach 100% by 3pm even when grid is cheap. Full solution requires Tesla Fleet API or MPC with rate commands.

**Tessie API credentials:**
- Energy site ID: `2252120180790091`
- Token: in `config/secrets.yaml`
- Endpoints: `POST /api/1/energy_sites/{id}/backup` with `{"backup_reserve_percent": N}`

**Solcast credentials:**
- API Key: stored in the HA Solcast integration config (not in this repo)
- Resource ID: `fd2e-343e-680f-b27e`
- Integration: HACS "HA Solcast PV Solar Forecast Integration" by BJReplay

---

## Current automation status (as of 2026-05-18)

**9 automations live** in `config/automations.yaml`:

| # | Name | Rule | Status |
|---|------|------|--------|
| 1 | `battery_startup_set_reserve_floor` | Rule 6 — set 20% on restart | ✅ Live |
| 2 | `battery_morning_charge_trigger` | Rule 1 — 9:30am, SoC < 95%, set reserve 100% | ✅ Live |
| 3 | `battery_charge_complete_reset` | Rule 1 — when SoC hits 95%, reset to 20% | ✅ Live |
| 4 | `battery_pre_demand_window_reset` | Rule 2 — 2:55pm hard reset to 20% | ✅ Live |
| 5 | `battery_overnight_safety_topup` | Rule 7 Step 1 — 10pm, SoC < 41%, price < 25¢ → top to 41% | ✅ Live |
| 6 | `battery_morning_reserve_reset` | Rule 7 — 8am, clear overnight target | ✅ Live |
| 7 | `battery_negative_price_charge` | Rule 8 — price < $0, charge to 100% | ✅ Live |
| 8 | `battery_negative_price_reset` | Rule 8 — price returns positive, reset to 20% | ✅ Live |
| 9 | `battery_winter_overnight_precharge` | Rule 7 Step 2 — Jun–Aug, 1am, SoC < 75%, price < 15¢ → charge to 80% | ✅ Live |

**SmartShift (Amber's control)**: OFF since 11:30am 18 May 2026. HA automations in sole control.

**Rule 9 cloudy day automation**: also live — fires 7am, charges to 80% if Solcast `forecast_today < 10 kWh` and price is in cheap window.

**Not yet built:**
- EV charging automation (Rules 4 & 5)
- Daikin AC load shedding during demand window
- Solcast-aware morning charge refinement (beyond Rule 9)
- Price spike arbitrage (Rule 10)

---

## Key files

| File | What it contains |
|------|-----------------|
| `energy_rules.md` | Full rule-set (10 rules), all business logic, decision priority order, known limitations |
| `ea116_tariff.md` | EA116 tariff structure — demand charge, Solar Sponge, export penalty |
| `energy_log.md` | Chronological log of what was built each day and observations |
| `todo.md` | Personal and product to-do lists |
| `PRODUCT.md` | Full product design doc — Sol architecture, MPC design, multi-tenant vision |
| `config/automations.yaml` | The actual HA automations |
| `config/configuration.yaml` | HA config — sensors, REST commands, template sensors |

---

## What to watch for

**June 1 is critical** — demand window logic activates. Any grid import 3–9pm sets the monthly demand charge. The 2:55pm hard reset (automation 4) must fire reliably every day in peak months.

**Monitoring questions still open:**
- Does overnight top-up trigger correctly when SoC < 41% at 10pm?
- Does morning charge trigger respect the 20¢ price ceiling in off-peak months?
- Are there days where the battery doesn't reach target by 3pm due to the conservative Tessie control?

---

## The bigger picture

This HA system is intentionally a **rule-based approximation** of what an MPC solver would do. The rules are explicit and debuggable, but brittle — each goal is tangled into multiple rules, there's no look-ahead beyond simple forecast checks, and tradeoffs can't be expressed as weights.

The Sol product (`/Users/simonmonk/Simon Projects/Home Energy Console/`) is being built to replace this with a proper goal-driven MPC architecture. This site is the first use case.

When something behaves unexpectedly here, the answer is almost always in `energy_rules.md` — read that first.
