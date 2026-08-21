# Home Energy Agent

A battery optimisation agent for residential solar + storage systems. Runs every 30 minutes on a Raspberry Pi, reads state from Home Assistant, and decides how to charge/discharge a Tesla Powerwall based on dynamic electricity prices and solar forecasts.

Built for a single site on Amber Electric (dynamic wholesale pricing) and Ausgrid (demand tariff). The rules are specific to this setup, but the architecture is general.

## What it does

The agent manages a Powerwall's `backup_reserve_percent` via the Tessie API — the only writable control available without Tesla Fleet API access. It raises the reserve to charge from the grid, lowers it to allow discharge, and coordinates with solar production to minimise costs and avoid network demand charges.

Three layers run each cycle:

- **Deterministic rule layer** — a Python if/elif decision tree that owns the control path. ~40 rules covering morning charging, demand window protection, solar sponge mode, EV coordination, and overnight insurance.
- **LLM narrative logger** — Claude writes a plain-English explanation of each decision after the fact. No control authority.
- **LP/MPC shadow optimiser** — a receding-horizon linear program that runs in parallel, logging what it *would* have done. Used to validate the rule layer and prepare for an eventual cutover.

## What's here

```
agent/                  The agent itself
  energy_agent.py         Main agent — runs every 30 min via cron
  optimizer.py            LP/MPC shadow optimiser
  billing_data.py         Amber billing analysis
  data_logger.py          SQLite logger for self-learning pipeline
  build_models.py         Nightly model retraining (solar corrector, charge rates)
  push_virtual_sensors.py Restores HA virtual sensors on restart
  test_decision.py        Decision layer tests
  test_optimizer.py       Optimiser tests
  .env.example            Required environment variables

config/                 Home Assistant configuration
  configuration.yaml      Sensors, REST commands, template sensors
  automations.yaml        Safety automations (demand window guard, etc.)

deploy_ha_config.sh     Deploy config to the Pi's live HA instance

app/                    Sol — a Next.js prototype for goal-driven optimisation (separate project)

ARCHITECTURE.md         System design — 4-layer self-learning architecture
CONTEXT.md              Current system state and operational context
energy_rules.md         Full rule-set documentation
ea116_tariff.md         Ausgrid EA116 tariff structure
PRODUCT.md              Sol product design doc
```

## Setup

**Requirements:** Raspberry Pi (or any Linux host), Home Assistant with Tesla Powerwall and Solcast integrations, Amber Electric account, Python 3.11+.

1. Clone the repo and set up a venv:
   ```bash
   cd agent
   python3 -m venv venv
   source venv/bin/activate
   pip install anthropic requests pytz
   ```

2. Copy `.env.example` to `.env` and fill in your credentials:
   - `HA_TOKEN` — Home Assistant long-lived access token
   - `TESSIE_TOKEN` — from tessie.com
   - `TESSIE_SITE_ID` — your Powerwall energy site ID
   - `AMBER_SITE_ID` — from the Amber API
   - `ANTHROPIC_API_KEY` — for the LLM narrative layer
   - `SITE_LATITUDE` / `SITE_LONGITUDE` — for weather forecasts

3. Verify entity IDs in `energy_agent.py` match your HA instance (sensor names vary by integration version).

4. Test with a dry run:
   ```bash
   python energy_agent.py --dry-run
   ```

5. Schedule via cron:
   ```cron
   */30 * * * * ~/home-energy-agent/agent/venv/bin/python ~/home-energy-agent/agent/energy_agent.py >> /tmp/energy_agent.log 2>&1
   ```

## Adapting to your site

The rules in `energy_agent.py` are written for a specific tariff (Ausgrid EA116 with demand charges) and hardware (Powerwall 2, SolarEdge, Zappi EV charger). To adapt:

- **Tariff**: the demand window (3–9pm), peak months, and Solar Sponge periods are EA116-specific. Change these constants for your tariff.
- **Hardware**: charge rates, battery capacity, and solar capacity are calibrated for this site. `build_models.py` will recalibrate from your logged data after ~1 week.
- **Rules**: `energy_rules.md` documents every rule with its rationale. Start by disabling rules that don't apply (e.g., demand window rules if your tariff has no demand charge).

## License

MIT
