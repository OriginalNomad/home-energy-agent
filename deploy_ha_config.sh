#!/usr/bin/env bash
#
# Deploy this repo's HA config to the live Home Assistant on the Pi.
#
#   ./deploy_ha_config.sh            # deploy
#   ./deploy_ha_config.sh --check    # show what would change, touch nothing
#
# Why this exists
# ---------------
# Until 2026-07-22 `config/` in this repo was a plain COPY that no Home
# Assistant instance ever read. The live config drifted 7 weeks behind it, so
# several fixes recorded as "deployed" were never actually running — including
# the battery_grid_charge_target 85% peak floor, whose absence left
# `battery_autonomous_revert_target_reached` permanently triggered and made
# autonomous mode unusable on peak days.
#
# The live instance is the one on the Pi (Docker `homeassistant`, config mounted
# from ~/homeassistant/config). It is both what the agent talks to and what the
# browser dashboard at http://energypi.local:8123 shows. A second, vestigial HA
# container on the Mac Studio is NOT used — see energy_log 2026-07-22.
#
# Config is applied with targeted reloads (automation/template/input_boolean),
# so there is no HA restart and no gap in battery control. If `check_config`
# rejects the new files they are rolled back automatically before any reload.

set -euo pipefail

PI="${PI_HOST:-energypi.local}"
REPO_CONFIG="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config"
REMOTE_CONFIG="\$HOME/homeassistant/config"
REMOTE_BACKUPS="\$HOME/homeassistant/config-backups"
FILES=(configuration.yaml automations.yaml)

if [[ "${1:-}" == "--check" ]]; then
    echo "Diffing repo against live config on $PI (live '<' vs repo '>')"
    for f in "${FILES[@]}"; do
        echo "── $f ──────────────────────────────────────"
        ssh "$PI" "sudo cat $REMOTE_CONFIG/$f" 2>/dev/null \
            | diff - "$REPO_CONFIG/$f" || true
    done
    exit 0
fi

TS=$(date +%Y%m%d-%H%M%S)

echo "→ backing up live config to config-backups/$TS"
ssh "$PI" "sudo mkdir -p $REMOTE_BACKUPS/$TS && \
           sudo cp $REMOTE_CONFIG/configuration.yaml $REMOTE_CONFIG/automations.yaml \
                   $REMOTE_BACKUPS/$TS/"

echo "→ copying repo config to $PI"
scp -q "${FILES[@]/#/$REPO_CONFIG/}" "$PI:/tmp/"
ssh "$PI" "sudo cp /tmp/configuration.yaml /tmp/automations.yaml $REMOTE_CONFIG/ && \
           sudo chown root:root $REMOTE_CONFIG/configuration.yaml $REMOTE_CONFIG/automations.yaml"

echo "→ validating (this takes ~30s)"
if ! ssh "$PI" "docker exec homeassistant python -m homeassistant --script check_config -c /config" \
        2>&1 | tee /tmp/ha_check.log | grep -qiv "error"; then
    echo "✗ check_config reported errors — rolling back, no reload performed"
    ssh "$PI" "sudo cp $REMOTE_BACKUPS/$TS/*.yaml $REMOTE_CONFIG/"
    cat /tmp/ha_check.log
    exit 1
fi

echo "→ reloading (no restart)"
ssh "$PI" "cd \$HOME/home-energy-agent/agent && ../agent/venv/bin/python -c \"
import energy_agent as ea
for dom in ['input_boolean','template','automation']:
    ea.ha_service(dom,'reload',{}); print('   reloaded', dom)
\""

echo "→ verifying key entities"
ssh "$PI" "cd \$HOME/home-energy-agent/agent && ../agent/venv/bin/python -c \"
import energy_agent as ea, time
time.sleep(5)
for e in ['sensor.battery_grid_charge_target','input_boolean.agent_manual_override',
          'sensor.tessie_powerwall_charge']:
    try:    print(f'   {e:42s} = ' + ea.ha_get(e)['state'])
    except Exception as ex: print(f'   {e:42s} ! ' + str(ex)[:60])
\""

echo "✓ deployed. Roll back with:"
echo "    ssh $PI \"sudo cp $REMOTE_BACKUPS/$TS/*.yaml $REMOTE_CONFIG/\"  then reload"
