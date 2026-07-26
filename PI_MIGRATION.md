# Pi Migration Runbook

Migrate Home Assistant + energy agent from Mac Studio to Raspberry Pi 5.

**Outcome:** HA running on Pi at `http://homeassistant.local:8123` (local) and
`https://home.sol.io` (remote via Cloudflare Tunnel). Agent cron running on same Pi.
Mac Studio no longer needed for home automation.

---

## Before you start (do on Mac today)

- [ ] **HA backup** — Settings → System → Backups → Create backup → Download the `.tar` file
- [ ] **Note your HA version** — Settings → About (Pi restore must match or be newer)
- [ ] **Amber integration** — note your account email (re-auth needed after restore)
- [ ] **Tessie token** — already in `config/secrets.yaml` and `agent/energy_agent.py`
- [ ] **Solcast API key** — already in `CONTEXT.md`
- [ ] **Cloudflare** — confirm `sol.io` zone is active in your Cloudflare account

---

## Step 1 — Flash HA OS to SSD

On the Mac:

1. Download **Raspberry Pi Imager** from raspberrypi.com
2. Connect Samsung T7 SSD via USB
3. In Imager:
   - Device: **Raspberry Pi 5**
   - OS: **Other specific-purpose OS → Home Assistant → Home Assistant OS (RPi5)**
   - Storage: your Samsung T7
4. Flash — no need to set Wi-Fi or SSH in Imager for HA OS
5. Plug SSD into Pi, connect ethernet to router, power on

---

## Step 2 — First boot & restore

1. Wait ~5 min for first boot (HA OS installs itself)
2. On any browser: `http://homeassistant.local:8123`
   - If that doesn't resolve: check your router's DHCP leases for the Pi's IP and use that
3. At the welcome screen choose **Restore from backup**
4. Upload the `.tar` backup from Step 0
5. Wait for restore (~10 min) — HA will restart
6. Log in — all integrations, automations, dashboards should be there
7. Check each integration — most will reconnect automatically; Amber may need re-auth

**Integrations to verify after restore:**

| Integration | Likely status |
|-------------|--------------|
| Amber Electric | May need re-auth (OAuth token) |
| Solcast | Should restore (API key in config) |
| Tesla/Tessie | Should restore (token in config) |
| Zappi (myenergi) | May need re-auth |
| Polestar | May need re-auth |
| Met.no / weather | Auto |

---

## Step 3 — Assign Pi a static IP

In your router's DHCP settings, assign the Pi a fixed IP (e.g. `192.168.1.50`).
This ensures the Cloudflare Tunnel config never breaks after a router restart.

Note it down — you'll use it in Step 5.

---

## Step 4 — Enable SSH on the Pi

In HA: Settings → System → Advanced → Enable SSH (or install the **Terminal & SSH** add-on).

Or from the HA UI: Settings → Add-ons → Add-on Store → search "Terminal" → install
**Terminal & SSH** → Start → enable "Start on boot".

SSH in from the Mac:
```bash
ssh root@homeassistant.local
# default password: none (set one in the add-on config)
```

---

## Step 5 — Install Cloudflare Tunnel

SSH into the Pi, then run as root:

```bash
# Install cloudflared
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 \
  -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

# Authenticate (opens browser — do this in your SSH session or paste URL manually)
cloudflared tunnel login

# Create the tunnel
cloudflared tunnel create homeassistant
# → note the tunnel ID printed (a UUID like 12345abc-...)

# Create DNS record (points home.sol.io → tunnel)
cloudflared tunnel route dns homeassistant home.sol.io
```

Create the config file:
```bash
mkdir -p /root/.cloudflared
```

Drop the config file at `/root/.cloudflared/config.yml` (contents below — fill in your tunnel ID):

```yaml
tunnel: <YOUR-TUNNEL-ID>
credentials-file: /root/.cloudflared/<YOUR-TUNNEL-ID>.json

ingress:
  - hostname: home.sol.io
    service: http://localhost:8123
    originRequest:
      noTLSVerify: false
  - service: http_status:404
```

Install as a system service and start:
```bash
cloudflared service install
systemctl enable cloudflared
systemctl start cloudflared
systemctl status cloudflared  # should show "active (running)"
```

Test: open `https://home.sol.io` — should show the HA login page.

---

## Step 6 — Tell HA to trust the Cloudflare proxy

In the HA **File Editor** add-on (or Terminal), edit `/config/configuration.yaml` and add:

```yaml
http:
  use_x_forwarded_for: true
  trusted_proxies:
    - 127.0.0.1
    - ::1
```

Restart HA: Settings → System → Restart.

---

## Step 7 — iOS app

In the HA iOS app:
- Settings → Companion App → (your server) → External URL: `https://home.sol.io`
- Internal URL: `http://homeassistant.local:8123` (for when on home Wi-Fi)

The app switches automatically between internal/external.

---

## Step 8 — Clone repo and install agent

In the Pi terminal:

```bash
# HA OS uses a containerised OS — the agent runs in the host OS
# Access host OS shell from HA Terminal add-on:
#   ha > login  (drops you to host shell)

cd /root
git clone https://github.com/OriginalNomad/home-energy-console.git
cd home-energy-console/agent

# Install Python deps
pip3 install anthropic requests pytz scipy

# Create .env with Anthropic key
cat > .env << 'EOF'
ANTHROPIC_API_KEY="sk-ant-api03-...  # in agent/.env — never commit the real key"
EOF
```

---

## Step 9 — Set up cron

```bash
crontab -e
```

Add these lines (same as Mac, paths updated for Pi):

```
*/30 * * * * ANTHROPIC_API_KEY="sk-ant-api03-...  # in agent/.env — never commit the real key" /usr/bin/python3 /root/home-energy-console/agent/energy_agent.py >> /tmp/energy_agent.log 2>&1

5 21 * * * /usr/bin/python3 /root/home-energy-console/agent/log_daily_energy.py >> /tmp/daily_energy.log 2>&1 && /usr/bin/python3 /root/home-energy-console/agent/demand_window_summary.py --post >> /tmp/demand_window.log 2>&1

0 * * * * /usr/bin/python3 /root/home-energy-console/agent/demand_window_summary.py --post >> /tmp/demand_window.log 2>&1
```

Test the agent runs:
```bash
ANTHROPIC_API_KEY="sk-ant-api03-..." /usr/bin/python3 /root/home-energy-console/agent/energy_agent.py
```

---

## Step 10 — Turn off Mac Studio automation

Once the Pi is confirmed working:

1. Remove the cron entries from the Mac: `crontab -e` → delete the agent lines
2. Optionally stop HA on the Mac (if it was running there)
3. The Mac is now just a dev machine

---

## Troubleshooting

| Problem | Check |
|---------|-------|
| `homeassistant.local` not resolving | Use IP from router DHCP table instead |
| Integration not reconnecting | Settings → Integrations → click the integration → reconfigure |
| Cloudflare Tunnel not connecting | `systemctl status cloudflared` and `journalctl -u cloudflared -n 50` |
| Agent can't reach HA | Confirm HA is running: `curl http://localhost:8123/api/` from Pi terminal |
| Python dep missing | `pip3 install <package>` from Pi terminal |
| Git push failing from Pi | Set up SSH key: `ssh-keygen` → add public key to GitHub |

---

## Python deps reference

```
anthropic
requests
pytz
scipy
```

All available via pip, no special versions required.
