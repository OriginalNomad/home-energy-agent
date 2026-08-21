# Energy Optimisation Agent

A Claude-powered agent that replaces the strategic HA rules with LLM reasoning.
Runs every 30 minutes. Reads system state, gets price and solar forecasts, decides
what (if anything) to do, and logs its reasoning.

## What it replaces

The HA automations that make *strategic* decisions:
- `battery_morning_charge_trigger` — when to start morning charging
- `battery_cheap_window_autonomous_charge` — switch to fast charging
- `battery_solar_sponge_mode_check` — solar sponge reserve management
- `ev_charge_mode_manager` — Zappi mode selection

**Keep the HA safety automations**: `battery_autonomous_export_safety_net`,
`battery_pre_demand_window_reset`, `battery_autonomous_revert_target_reached`.
These need millisecond-reliable triggers — not a 30-min agent cycle.

## Setup

```bash
cd agent
python3 -m venv venv
source venv/bin/activate
pip install anthropic requests pytz
```

Edit `energy_agent.py` and fill in:
- `HA_TOKEN` — HA Profile → Long-Lived Access Tokens
- `TESSIE_TOKEN` — from `config/secrets.yaml`
- `ANTHROPIC_API_KEY` — from console.anthropic.com

Verify entity IDs in the `ENTITIES` dict, especially:
- `ev_soc` — Polestar sensor ID varies by integration version
- `solar_power` — SolarEdge sensor name
- Solcast sensors — attribute keys differ between integration versions

## Test before scheduling

```bash
# Dry run — reads state and prints reasoning, no writes
python energy_agent.py --dry-run

# Live run — actually sets reserve/mode/zappi
python energy_agent.py
```

## Schedule

```cron
# crontab -e
*/30 * * * * ~/home-energy-agent/agent/venv/bin/python ~/home-energy-agent/agent/energy_agent.py >> /tmp/energy_agent.log 2>&1
```

Or trigger from HA via a shell_command + automation if you want HA to control timing.

## Cost

Each cycle makes ~3–5 API calls to Claude (~2000 tokens in, ~500 out).
At claude-opus-4-5 pricing: roughly $0.03–0.08 per cycle, ~$35–90/month at 30-min intervals.

To reduce cost: use `claude-sonnet-4-5` instead (similar reasoning quality for this task,
~5× cheaper), or run every 60 minutes.

## Logs

- Local: `agent/agent_decisions.log` — one line per cycle
- HA: persistent notification "Energy Agent" — updated each cycle
