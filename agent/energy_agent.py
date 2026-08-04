#!/usr/bin/env python3
"""
Home Energy Optimisation Agent
================================
Runs every 30 minutes. Reads state from Home Assistant, gets price and solar
forecasts, then uses Claude to reason about what (if anything) to do.

Install:  pip install anthropic requests pytz
Schedule: cron  */30 * * * *  /path/to/venv/bin/python /path/to/energy_agent.py
          — or — trigger from an HA automation via a shell_command

Verify entity IDs against your HA instance before running — especially the
Solcast and EV sensors which vary by integration version.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import anthropic
import pytz
import requests

# Optional LP optimiser (shadow mode only — never in the control path).
try:
    from optimizer import optimize_battery, OptParams
    _HAVE_OPTIMIZER = True
except Exception:                       # scipy/optimizer missing → skip cleanly
    _HAVE_OPTIMIZER = False

try:
    import data_logger
    data_logger.init_db()
    _HAVE_DATA_LOGGER = True
except Exception as _dl_exc:
    _HAVE_DATA_LOGGER = False
    print(f"  Warning: data_logger unavailable — {_dl_exc}", file=sys.stderr)

# ---------------------------------------------------------------------------
# Configuration — move sensitive values to environment variables in production
# ---------------------------------------------------------------------------

# Load .env file first so environment variables override these defaults.
# .env is gitignored — put machine-specific values (HA_URL, keys) there.
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _key = _k.strip()
            _val = _v.strip().strip('"').strip("'")
            if not os.environ.get(_key):   # set if missing OR empty
                os.environ[_key] = _val

# HA_URL default is fine (localhost). Secrets come from agent/.env only — never hardcode
# a token here (a committed token is compromised and must be rotated). See .env.example.
HA_URL   = os.environ.get("HA_URL",   "http://localhost:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

TESSIE_TOKEN   = os.environ.get("TESSIE_TOKEN",   "")
TESSIE_SITE_ID = os.environ.get("TESSIE_SITE_ID", "2252120180790091")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")  # set via agent/.env

# Optional liveness dead-man's-switch (Healthchecks.io or any ping-URL service). The agent
# pings this on each completed cycle; the external monitor alerts if the pings STOP (Pi down,
# cron broken, crash-loop) — the one failure mode nothing on the Pi can self-report. Unset =
# disabled (no-op). Lives in the Pi's agent/.env only.
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "")

LOG_FILE   = Path(__file__).parent / "agent_decisions.log"
JSONL_FILE = Path(__file__).parent / "decisions.jsonl"

MODEL = "claude-sonnet-4-6"

# Token pricing — verify at console.anthropic.com/settings/billing
# List price (USD per 1M tokens); update if you have a volume discount.
# Cache reads are charged at 10% of the input rate.
COST_PER_1M_INPUT        = 3.00
COST_PER_1M_OUTPUT       = 15.00
COST_PER_1M_CACHE_READ   = COST_PER_1M_INPUT * 0.10
COST_PER_1M_CACHE_WRITE  = COST_PER_1M_INPUT * 1.25  # 25% premium on cache writes

# ---------------------------------------------------------------------------
# Feature flag — historical price model
# Set True to use rolling price percentiles for grid target calculation.
# Set False to revert to the static cost_target logic instantly.
# ---------------------------------------------------------------------------
HISTORICAL_PRICE_MODEL = True

# ---------------------------------------------------------------------------
# Re-architecture kill-switches
# ---------------------------------------------------------------------------
# DETERMINISTIC_AUTHORITATIVE: when True, compute_decision_context() drives all set_* actions
# before the LLM runs. The LLM is called for narrative only; its set_* tool calls are no-op'd.
# Flip to False to revert to LLM-authoritative instantly. Phase 5 — see ARCHITECTURE.md.
DETERMINISTIC_AUTHORITATIVE = True

# GENTLE_CHARGE_CONTROL: when True, self_consumption charges chase reserve = SoC + offset
# (a ~1.6 kW cycle-average grid top-up) instead of writing a fixed reserve = target, which
# under firmware 26.18.3 pulls the full 5 kW from any large reserve−SoC gap. Restores the
# ~1.7 kW self_consumption rate the whole verdict tree already budgets. autonomous charges
# are unaffected (they stay reserve=100, export-guarded, full 5 kW). Flip to False to revert
# to reserve = target. See energy_rules.md Rule 31 and _gentle_charge_reserve().
GENTLE_CHARGE_CONTROL = True

# USE_CORRECTED_SOLAR: when True, compute_decision_context() reasons about the
# *bias-corrected* Solcast remaining-today figure rather than the raw one.
# Solcast over-forecasts this flat-roof site badly in winter (~0.14 of actual at
# 08:00, ~0.74 by 13:00), so raw values made the rule layer over-optimistic about
# solar covering the day — on 2026-07-23 it held `peak_solar_will_cover` for 17
# consecutive overnight cycles against ~16.6 kWh forecast when the calibrated
# expectation was 7.55 kWh, and the battery drained to 17% before the emergency
# automation caught it.
#
# Direction of risk: the corrected figure is *lower*, so the agent charges more
# and earlier. That costs money and protects the demand charge — the safe
# direction to err. Flip to False to revert to raw Solcast instantly.
USE_CORRECTED_SOLAR = True

# Insurance floor ceiling — maximum floor percentage when price is at the
# cheapest historical level. User can override via HA slider (below).
DEFAULT_MAX_INSURANCE_FLOOR = 70   # %

# ---------------------------------------------------------------------------
# SETTINGS_SPEC — declared sane bands for the HA `input_number` tunables.
#
# Why this exists (2026-07-23): these helpers are *control inputs*, read every
# cycle by compute_decision_context(), which has been authoritative since
# Phase 5. They were being trusted with no validation and no audit trail, and
# both of the fallback idioms in use gave only the illusion of a default:
#
#     ev.get("min_soc_pct") or 20        # 80 is truthy → fallback never fires
#     settings.get("max_insurance_floor_pct", 70)   # key exists as 0.0 → 0.0 wins
#
# Observed consequences: `battery_max_insurance_floor_pct` sat at 0, silently
# disabling Rule 15's insurance floor; and `ev_min_soc_pct` drifted to 80,
# which made `ev_case3_below_minimum` fire at 60% EV SoC and put the Zappi on
# Fast on a peak morning while the house battery was at 30% and falling.
#
# Semantics — deliberately conservative:
#   * value inside [lo, hi]  → used as-is, even if it differs from `intended`.
#     In-band tuning is the user's business and is never overridden.
#   * value outside [lo, hi] → `intended` is substituted **for this cycle only**
#     and a violation is recorded + notified. Nothing is written back to HA;
#     the helper keeps whatever the UI says (validate-and-warn, not self-heal).
#   * unreadable/unavailable → `intended` is substituted, no violation (that is
#     a transport failure, not a bad value).
#
# NO TARGET VALUES ARE STORED HERE. The HA console is the single source of truth
# for what the targets *are*; this table only declares what range is structurally
# valid. That distinction matters:
#
#   * a **target** ("charge the EV fast below 10¢") is a preference. It lives in
#     HA, is set and displayed there, and is never duplicated in code or docs —
#     duplicates go stale silently. On 2026-07-23 CONTEXT.md claimed 6¢ while the
#     console said 10¢, and energy_rules.md gave the same helper two different
#     "defaults" (5¢ and 6¢) in one document.
#   * a **band** ("below 0 or above 12 and the rule stops meaning what it should")
#     is an engineering limit, not a preference. Validation is impossible without
#     one, so bands are the only numbers kept here.
#
# When a value falls outside its band the substitute is, in order:
#   1. the most recent in-band value HA itself reported (from `settings_used` in
#      decisions.jsonl) — still HA as the source of truth, just an earlier read;
#   2. failing that, the bad value clamped to the nearest band edge;
#   3. failing that (entity unreadable, no history), the key is omitted entirely
#      so the caller's own `.get(key, default)` applies — which is correct for a
#      genuinely absent value, and was never the bug. The bug was a key that
#      *existed* holding a wrong value, where `.get`'s default can never fire.
#
# Widen a band if a value is genuinely wanted — e.g. set max_insurance_floor_pct's
# `lo` to 0 to allow disabling Rule 15's floor.
#
# INVARIANT (added 2026-07-31): a band and its helper's HA slider `min`/`max` in
# configuration.yaml must agree — otherwise the dashboard offers values the agent silently
# rejects. On 2026-07-31 four of the seven bands were below their slider max (ultra_cheap
# 12<15, standard 25<30, min_charge 45<60, min_soc 50<80); a value set at the top of the
# slider (ev_ultra_cheap_c=15) was overridden. Resolve a mismatch in the direction that
# matches intent: where the higher values are wanted, WIDEN the band (ultra_cheap→30,
# standard→50, min_charge→60, plus the two price sliders widened to match); where they are
# pathological, LOWER the slider instead (ev_min_soc — see its note below).
SETTINGS_SPEC = {
    # settings key                 entity alias                    lo     hi
    "ev_ultra_cheap_c":           ("ev_ultra_cheap_c",             0.0,   30.0),
    "ev_standard_price_c":        ("ev_standard_price_c",          0.0,   50.0),
    "ev_min_charge_price_c":      ("ev_min_charge_price_c",        5.0,   60.0),
    "max_insurance_floor_pct":    ("battery_max_insurance_floor", 20.0,   95.0),
    # EV helpers — these live under state["ev"], not state["settings"], but are
    # validated by the same machinery because they are read by the same layer.
    # ev_min_soc's band stays 0–50 DELIBERATELY (slider max is 80): a value >50 forces the
    # EV to Fast whenever its SoC is below that (the 2026-07-23 min_soc=80 bug — see tests
    # test_settings_drifted_ev_min_soc_no_longer_forces_fast). The invariant fix here is to
    # LOWER the slider to 50, not widen the band. Pending user decision (2026-07-31).
    "ev_min_soc_pct":             ("ev_min_soc",                   0.0,   50.0),
    "ev_charge_target_pct":       ("ev_charge_target",            50.0,  100.0),
    "ev_departure_target_pct":    ("ev_departure_target",         50.0,  100.0),
}
PRICE_HISTORY_DAYS          = 7    # days of JSONL price history to use
MIN_HISTORY_RECORDS         = 48   # minimum records before model activates (~1 day)

# Overnight hold: Solar Sponge (10am-3pm) is structurally cheaper than evening/overnight
# prices on most days. Don't charge overnight if current price exceeds this threshold —
# wait for the morning Solar Sponge instead. Rule 13 deadline maths handles peak months.
SOLAR_SPONGE_PRICE_THRESHOLD = 10.0   # ¢ — if overnight price > this, wait for Sponge

# Populated by get_current_state() / get_price_forecast() during each cycle,
# then read by log_decision() to write the structured JSON record.
_cycle_context: dict = {}
_demand_reserve_guard_fired: bool = False

# Manual override — user takes the wheel. See _manual_override_active().
MANUAL_OVERRIDE_MAX_HOURS = 12.0

SYDNEY_TZ   = pytz.timezone("Australia/Sydney")
PEAK_MONTHS = {11, 12, 1, 2, 3, 6, 7, 8}   # months with demand window

# ---------------------------------------------------------------------------
# Entity IDs — verify these match your HA instance
# ---------------------------------------------------------------------------

ENTITIES = {
    "battery_soc":          "sensor.tessie_powerwall_charge",
    "battery_soc_gateway":  "sensor.tesla_powerwall_2_charge",
    "battery_mode":         "sensor.powerwall_mode",
    "battery_reserve":      "sensor.powerwall_backup_reserve",
    "manual_override":      "input_boolean.agent_manual_override",
    "narrative_disable":    "input_boolean.agent_narrative_disable",
    "battery_target":       "sensor.battery_grid_charge_target",
    "grid_price":           "sensor.1a_wigram_road_glebe_general_price",
    "grid_forecast":        "sensor.1a_wigram_road_glebe_general_forecast",
    "cheap_window":         "sensor.amber_in_cheap_window",
    "solar_power":          "sensor.solar_power_w",                    # W — Powerwall gateway, real-time (no cloud lag)
    "solar_remaining":      "sensor.solcast_pv_forecast_forecast_remaining_today",  # kWh
    "solcast_today":        "sensor.solcast_pv_forecast_forecast_today",  # kWh + detailedHourly attr
    "solcast_tomorrow":     "sensor.solcast_pv_forecast_forecast_tomorrow",  # kWh + detailedHourly attr
    "solar_forecast_today":  "sensor.solcast_pv_forecast_forecast_today",
    "solcast_power_now":     "sensor.solcast_pv_forecast_power_now",    # W — Solcast's instantaneous estimate (÷1000 for kW)
    "solcast_this_hour":     "sensor.solcast_pv_forecast_forecast_this_hour",  # Wh — expected for current hour (÷1000 for kWh)
    "solcast_next_hour":     "sensor.solcast_pv_forecast_forecast_next_hour",  # Wh — expected for next hour (÷1000 for kWh)
    "home_load":            "sensor.home_load_30min_average",          # kW
    "battery_power":        "sensor.tesla_powerwall_2_battery_power",  # kW, positive=charging, negative=discharging
    "ev_plug":              "sensor.home_ev_charger_zappi_myenergi_home_ev_charger_zappi_plug_status",
    "ev_zappi_mode":        "select.home_ev_charger_zappi_myenergi_home_ev_charger_zappi_charge_mode",
    "ev_soc":               "sensor.polestar_7853_battery_charge_level",
    "ev_schedule_active":   "input_boolean.ev_schedule_active",
    "ev_departure_time":    "input_datetime.ev_departure_time",
    "ev_departure_target":  "input_number.ev_departure_target_pct",
    "ev_min_soc":           "input_number.ev_min_soc_pct",
    "ev_charge_target":     "input_number.ev_charge_target_pct",
    "ev_ultra_cheap_c":     "input_number.ev_ultra_cheap_threshold_c",
    "ev_standard_price_c":  "input_number.ev_standard_price_c",
    "ev_min_charge_price_c":"input_number.ev_min_charge_price_c",
    "battery_max_insurance_floor": "input_number.battery_max_insurance_floor_pct",
    "fit_price":              "sensor.1a_wigram_road_glebe_feed_in_price",
    "fit_forecast":           "sensor.1a_wigram_road_glebe_feed_in_forecast",
    "fit_descriptor":         "sensor.1a_wigram_road_glebe_feed_in_price_descriptor",
}

# ---------------------------------------------------------------------------
# Home Assistant helpers
# ---------------------------------------------------------------------------

HA_HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

def ha_get(entity_id: str) -> dict:
    r = requests.get(f"{HA_URL}/api/states/{entity_id}", headers=HA_HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()

def ha_state(entity_id: str) -> str:
    return ha_get(entity_id)["state"]

def ha_attrs(entity_id: str) -> dict:
    return ha_get(entity_id).get("attributes", {})

def ha_service(domain: str, service: str, data: dict):
    r = requests.post(
        f"{HA_URL}/api/services/{domain}/{service}",
        headers=HA_HEADERS,
        json=data,
        timeout=10,
    )
    r.raise_for_status()

def ha_set_state(entity_id: str, state: str, attributes=None):
    """Write a value directly to the HA state machine — creates read-only sensor entities."""
    r = requests.post(
        f"{HA_URL}/api/states/{entity_id}",
        headers=HA_HEADERS,
        json={"state": state, "attributes": attributes or {}},
        timeout=10,
    )
    r.raise_for_status()

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _ev_schedule(now: datetime) -> dict:
    """Read the HA EV schedule controls and return structured context for the agent."""
    active = ha_state(ENTITIES["ev_schedule_active"]) == "on"
    if not active:
        return {"active": False}
    try:
        dep_str    = ha_state(ENTITIES["ev_departure_time"])   # "YYYY-MM-DD HH:MM:SS"
        dep_target = int(float(ha_state(ENTITIES["ev_departure_target"])))
        dep_dt     = datetime.strptime(dep_str[:16], "%Y-%m-%d %H:%M").replace(tzinfo=SYDNEY_TZ)
        hours_to_departure = round((dep_dt - now).total_seconds() / 3600, 1)
        return {
            "active":              True,
            "departure_time":      dep_dt.strftime("%Y-%m-%d %H:%M"),
            "departure_target_pct": dep_target,
            "hours_to_departure":  hours_to_departure,
        }
    except Exception:
        return {"active": True, "error": "could not parse departure time"}


def _build_battery_state(soc_tessie: int, soc_gateway: int, reserve: int,
                         mode: str, grid_target: int, charge_rate_kw: float) -> dict:
    """Return battery state dict, substituting gateway SoC if Tessie reading looks wrong.

    Tessie is a cloud poll and occasionally returns 0 or an implausibly low value.
    The gateway floors at reserve when reserve > true SoC, but when reserve is low
    (gateway > reserve) the gateway reading is reliable. Use it as a sanity check.
    """
    soc_tessie = soc_tessie or 0
    soc_gateway = soc_gateway or 0

    # Gateway is reliable (not floor-clipped) when it reads above the reserve level.
    gateway_reliable = soc_gateway > reserve

    tessie_suspicious = (
        soc_tessie == 0 or
        (gateway_reliable and soc_gateway > soc_tessie + 15)
    )

    if tessie_suspicious and gateway_reliable:
        print(
            f"WARNING: Tessie SoC ({soc_tessie}%) looks wrong "
            f"(gateway={soc_gateway}%, reserve={reserve}%) — using gateway reading.",
            file=sys.stderr,
        )
        soc_used = soc_gateway
        tessie_failed = True
    else:
        soc_used = soc_tessie
        tessie_failed = False

    return {
        "soc_pct":         soc_used,
        "soc_tessie_pct":  soc_tessie,
        "soc_gateway_pct": soc_gateway,
        "tessie_soc_failed": tessie_failed,
        "mode":            mode,
        "reserve_pct":     reserve,
        "grid_target_pct": grid_target,
        "charge_rate_kw":  charge_rate_kw,
    }


def get_current_state() -> dict:
    now = datetime.now(SYDNEY_TZ)
    month, hour = now.month, now.hour
    is_peak      = month in PEAK_MONTHS
    in_demand    = is_peak and 15 <= hour < 21
    in_sponge    = 10 <= hour < 15          # Solar Sponge window

    ev_plug_state   = ha_state(ENTITIES["ev_plug"])
    # Fail-safe: only "EV Connected"/"Charging" count as plugged in. The old
    # `!= "EV Disconnected"` test treated an *unavailable* Zappi (integration
    # offline → "unavailable"/"unknown") as plugged in, which on 2026-07-29 10:00
    # made the agent narrate the EV as plugged-in-and-Fast-charging and emit a
    # set_zappi(Fast) action while the car was neither connected nor charging.
    # Unknown state ⇒ not plugged ⇒ EV verdict short-circuits to ev_disconnected.
    ev_plugged      = ev_plug_state in ("EV Connected", "Charging")
    _solar_raw      = ha_state(ENTITIES["solar_power"])
    _solar_unavail  = _solar_raw in ("unavailable", "unknown")
    if _solar_unavail:
        print("WARNING: sensor.solaredge_current_power is unavailable in HA — "
              "solar reading will be 0; zero-solar cycle NOT counted.", file=sys.stderr)

    # Control inputs are range-checked before anything reads them — these drive
    # compute_decision_context(), so a drifted helper is a control fault. The
    # values and any violations are logged per cycle to decisions.jsonl, giving
    # an audit trail that does not depend on HA's recorder (which was found on
    # 2026-07-23 not to be capturing these helpers at all).
    # History lets a bad value fall back to the last value the console itself
    # held, rather than to a target hardcoded in this file.
    # NB: get_recent_records() (list of dicts), NOT get_recent_decisions()
    # (a formatted string for the prompt). Passing the string here silently
    # iterated characters and made last-known-good never fire.
    try:
        _settings_history = get_recent_records(20)
    except Exception:
        _settings_history = []
    _validated, _setting_violations = _read_validated_settings(_settings_history)
    _cycle_context["settings_validated"]  = _validated
    _cycle_context["settings_violations"] = _setting_violations
    _notify_setting_violations(_setting_violations)

    # Bias-corrected solar, computed once per cycle and cached. Feeds both the
    # dashboard sensor and (when USE_CORRECTED_SOLAR) the decision layer.
    _solar_bd = _corrected_solar_breakdown()

    # Solar accuracy is judged against the *bias-corrected* this-hour forecast,
    # not raw Solcast — otherwise a normal winter morning (actual ~14% of raw
    # Solcast) reads "unreliable" and zeroes the day's solar credit. Read the
    # this-hour forecast once here and weight it by the current hour's measured
    # ratio; fall back to raw when the hour has no calibration data.
    _solar_now_kw     = round(_float(_solar_raw) / 1000, 2)
    _this_hour_kwh    = round(_float(ha_state(ENTITIES["solcast_this_hour"])) / 1000, 2)
    _this_hour_ratio  = _hour_solar_ratio()
    _this_hour_corr   = (round(_this_hour_kwh * _this_hour_ratio, 2)
                         if _this_hour_ratio is not None else None)

    state = {
        "timestamp":       now.strftime("%Y-%m-%d %H:%M %Z"),
        "month":           now.strftime("%B"),
        "is_peak_month":   is_peak,
        "in_demand_window": in_demand,
        "in_solar_sponge":  in_sponge,
        "battery": _build_battery_state(
            _int(ha_state(ENTITIES["battery_soc"])),
            _int(ha_state(ENTITIES["battery_soc_gateway"])),
            _int(ha_state(ENTITIES["battery_reserve"])),
            ha_state(ENTITIES["battery_mode"]),
            _int(ha_state(ENTITIES["battery_target"])),
            round(_float(ha_state(ENTITIES["battery_power"])), 2),
        ),
        "grid": {
            "price_cents_kwh":  round(_float(ha_state(ENTITIES["grid_price"])) * 100, 1),
            "in_cheap_window":  ha_state(ENTITIES["cheap_window"]) == "True",
            "fit_price_cents_kwh": round(_float(ha_state(ENTITIES["fit_price"])) * 100, 1),
            "fit_descriptor":   ha_state(ENTITIES["fit_descriptor"]),
        },
        "solar": {
            # Unit notes: solaredge_current_power=W, solcast_power_now=W, this_hour=Wh, next_hour=Wh
            # remaining_today is natively kWh (no conversion needed)
            # sensor_unavailable=True means HA returned "unavailable"/"unknown" — treat differently
            # from genuine zero production; do NOT count as a zero-solar cycle.
            "sensor_unavailable":       _solar_unavail,
            "current_kw":               _solar_now_kw,
            "solcast_power_now_kw":     round(_float(ha_state(ENTITIES["solcast_power_now"])) / 1000, 2),
            "forecast_this_hour_kwh":   _this_hour_kwh,
            "forecast_this_hour_corrected_kwh": _this_hour_corr,
            "forecast_next_hour_kwh":   round(_float(ha_state(ENTITIES["solcast_next_hour"])) / 1000, 2),
            "forecast_remaining_kwh":   round(_float(ha_state(ENTITIES["solar_remaining"])), 1),
            # Bias-corrected remaining-today (Solcast hourly × measured site ratio).
            # compute_decision_context() prefers this over the raw figure when
            # USE_CORRECTED_SOLAR is on; None means Solcast detailedHourly was
            # unavailable, in which case the decision layer falls back to raw.
            "forecast_remaining_corrected_kwh": (_solar_bd or {}).get("remaining_corrected_kwh"),
            "correction_ratio":                 (_solar_bd or {}).get("effective_ratio"),
            # Accuracy: actual kW vs the *bias-corrected* this-hour forecast
            # (Wh→kWh ≈ avg kW for the hour). Corrected ref stops normal winter
            # mornings reading "unreliable"; falls back to raw when uncalibrated.
            "forecast_accuracy":        _solar_accuracy(
                                            _solar_now_kw,
                                            _this_hour_kwh,
                                            corrected_forecast_kw=_this_hour_corr,
                                        ),
        },
        "home_load_kw":  round(_float(ha_state(ENTITIES["home_load"])), 2),
        "ev": {
            "plug_status": ev_plug_state,
            "plugged_in":  ev_plugged,
            "charging":    ev_plug_state == "Charging",
            "zappi_mode":  ha_state(ENTITIES["ev_zappi_mode"]) if ev_plugged else "n/a",
            "ev_soc_pct":  _safe_int(ENTITIES["ev_soc"]),
            # Validated via SETTINGS_SPEC — the old `or 20` / `or 80` idioms only
            # caught falsy values, so a drifted-but-truthy 80 sailed through and
            # changed control behaviour (see SETTINGS_SPEC comment).
            # If the helper is unreadable AND there is no history, fall back to
            # the *conservative extreme* rather than to an invented target: 0 for
            # both makes `ev_soc < min` false and `ev_soc >= target` true, so the
            # EV goes solar-only (Eco+). Losing sight of a setting should never
            # start grid-charging the car.
            "min_soc_pct":       int(_validated.get("ev_min_soc_pct", 0)),
            "charge_target_pct": int(_validated.get("ev_charge_target_pct", 0)),
            "schedule":    _ev_schedule(now),
        },
        # Only keys that could be established appear here; anything absent leaves
        # the consumer's own `.get(key, default)` to apply.
        "settings": {
            k: _validated[k] for k in (
                "ev_ultra_cheap_c", "ev_standard_price_c", "ev_min_charge_price_c",
                "max_insurance_floor_pct",
            ) if k in _validated
            # price_stats injected by run_agent() after get_current_state() returns
        },
    }
    _cycle_context["state"] = state
    if _HAVE_DATA_LOGGER and not _cycle_context.get("db_cycle_id"):
        # Guard: only log once per cycle. get_current_state() is called twice —
        # once by run_agent() and again by the LLM tool. Without the guard,
        # each call inserts a new row, leaving the first permanently undecided.
        try:
            _cycle_context["db_cycle_id"] = data_logger.log_cycle_start(state)
        except Exception as _exc:
            print(f"  Warning: data_logger.log_cycle_start failed — {_exc}", file=sys.stderr)
    return state


def get_price_forecast() -> list[dict]:
    """Next 12 hours of Amber prices, resampled to uniform 30-minute buckets.

    The Amber sensor mixes interval sizes: the near-term intervals are 5-minute
    and the rest are 30-minute. Downstream logic (hours_to_cheap_end, the
    sustained-rise scan, the "6h"/"12h" forecast slices) all assume uniform
    30-min spacing — so we bucket every sub-interval into its 30-min slot and
    average the price. This makes index × 0.5h an accurate "hours from now".
    """
    attrs = ha_attrs(ENTITIES["grid_forecast"])
    forecasts = attrs.get("forecasts", [])
    if not forecasts:
        print("  Warning: price forecast is EMPTY — agent is flying blind on prices",
              file=sys.stderr)
        _cycle_context["price_forecast"] = []
        return []

    buckets: dict[str, list[float]] = {}
    descriptors: dict[str, str] = {}
    order: list[str] = []
    for f in forecasts:
        nem = f.get("nem_date", "")
        if len(nem) < 16:
            continue
        slot = "00" if int(nem[14:16]) < 30 else "30"
        key  = f"{nem[:11]}{nem[11:13]}:{slot}"     # e.g. 2026-05-29T16:30
        if key not in buckets:
            buckets[key]     = []
            descriptors[key] = f.get("descriptor", "")
            order.append(key)
        buckets[key].append(_float(f.get("per_kwh", 0)) * 100)

    result = []
    for key in order[:24]:          # 24 × 30 min = 12 h
        prices = buckets[key]
        result.append({
            "time":       key[:16].replace("T", " "),
            "cents_kwh":  round(sum(prices) / len(prices), 1),
            "descriptor": descriptors[key],
        })
    _cycle_context["price_forecast"] = result
    if _HAVE_DATA_LOGGER:
        try:
            _cid = _cycle_context.get("db_cycle_id")
            if _cid:
                data_logger.log_price_forecast(_cid, result)
        except Exception as _exc:
            print(f"  Warning: data_logger.log_price_forecast failed — {_exc}", file=sys.stderr)
    return result


def get_solar_forecast() -> list[dict]:
    """Remaining Solcast forecast periods for today."""
    now   = datetime.now(SYDNEY_TZ)
    attrs = ha_attrs(ENTITIES["solar_forecast_today"])
    # Solcast integration key varies by version — try both
    periods = attrs.get("detailedForecast") or attrs.get("detailed_forecast", [])
    result  = []
    for p in periods:
        start = p.get("period_start", "")
        if start[:16] >= now.strftime("%Y-%m-%dT%H:%M"):
            result.append({
                "time":    start[:16],
                "kw_est":  round(_float(p.get("pv_estimate", 0)), 2),
            })
    return result[:16]   # 8 hours


def get_weather_forecast() -> dict:
    """
    Hourly cloud cover, solar radiation, and rain probability from Open-Meteo.
    Returns solar-relevant hours (6am–7pm) for today and tomorrow.
    No API key required.
    """
    now = datetime.now(SYDNEY_TZ)
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude":  -33.88,
            "longitude": 151.19,
            "hourly":    "cloud_cover,shortwave_radiation,precipitation_probability,precipitation",
            "timezone":  "Australia/Sydney",
            "forecast_days": 2,
        },
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()["hourly"]

    today_hours, tomorrow_hours = [], []
    tomorrow_date = (now + timedelta(days=1)).date()

    for i, time_str in enumerate(data["time"]):
        dt   = datetime.fromisoformat(time_str).replace(tzinfo=SYDNEY_TZ)
        hour = dt.hour
        # Skip hours before the current hour today; include current hour and all of tomorrow
        too_early = dt.date() == now.date() and dt.hour < now.hour
        if too_early or not (6 <= hour <= 19):
            continue
        raw_rad  = data["shortwave_radiation"][i]
        precip   = data["precipitation"][i]        # mm/h
        # Rain attenuates actual surface irradiance to ~25% of model GHI.
        # SolarEdge won't start below ~50 W/m² — so rain effectively = no solar.
        eff_rad  = round(raw_rad * 0.25) if precip > 0.1 else round(raw_rad)
        entry = {
            "time":              time_str,
            "cloud_cover_pct":   data["cloud_cover"][i],
            "radiation_wm2":     round(raw_rad),
            "effective_radiation_wm2": eff_rad,
            "precip_mm_h":       precip,
            "rain_prob_pct":     data["precipitation_probability"][i],
        }
        if dt.date() == tomorrow_date:
            tomorrow_hours.append(entry)
        else:
            today_hours.append(entry)

    # Summarise tomorrow's solar quality using effective (rain-adjusted) radiation
    core = [h for h in tomorrow_hours if 8 <= int(h["time"][11:13]) <= 15]
    if core:
        avg_rad = round(sum(h["effective_radiation_wm2"] for h in core) / len(core))
        if avg_rad > 300:
            outlook = "good"
        elif avg_rad > 150:
            outlook = "poor"
        else:
            outlook = "overcast"
    else:
        avg_rad, outlook = None, "unknown"

    result = {
        "today_remaining":         today_hours,
        "tomorrow":                tomorrow_hours,
        "tomorrow_solar_outlook":  outlook,
        "tomorrow_avg_radiation":  avg_rad,
    }
    _cycle_context["weather_forecast"] = result

    # Push current hour's values to HA as read-only sensor entities
    current_hour_str = now.strftime("%Y-%m-%dT%H:00")
    try:
        idx     = data["time"].index(current_hour_str)
        raw_rad = data["shortwave_radiation"][idx]
        precip  = data["precipitation"][idx]
        eff_rad = round(raw_rad * 0.25 if precip > 0.1 else raw_rad)
        ha_set_state("sensor.weather_radiation_now", str(eff_rad), {
            "friendly_name": "Solar Radiation Now (effective)",
            "unit_of_measurement": "W/m²",
            "state_class": "measurement",
            "raw_radiation_wm2": round(raw_rad),
            "precip_mm_h": round(precip, 1),
            "rain_adjusted": precip > 0.1,
        })
        ha_set_state("sensor.weather_precip_now", str(round(precip, 1)), {
            "friendly_name": "Precipitation Now",
            "unit_of_measurement": "mm/h",
            "state_class": "measurement",
        })
        ha_set_state("sensor.weather_tomorrow_solar", outlook, {
            "friendly_name": "Tomorrow Solar Outlook",
            "avg_radiation_wm2": avg_rad,
        })
    except Exception as exc:
        print(f"  Warning: weather HA push failed: {exc}", file=sys.stderr)

    return result


def set_powerwall_reserve(percent: int) -> str:
    r = requests.post(
        f"https://api.tessie.com/api/1/energy_sites/{TESSIE_SITE_ID}/backup",
        headers={"Authorization": f"Bearer {TESSIE_TOKEN}", "Content-Type": "application/json"},
        json={"backup_reserve_percent": percent},
        timeout=15,
    )
    r.raise_for_status()
    return f"Reserve set to {percent}%"


def set_powerwall_mode(mode: str) -> str:
    r = requests.post(
        f"https://api.tessie.com/api/1/energy_sites/{TESSIE_SITE_ID}/operation",
        headers={"Authorization": f"Bearer {TESSIE_TOKEN}", "Content-Type": "application/json"},
        json={"default_real_mode": mode},
        timeout=15,
    )
    r.raise_for_status()
    return f"Mode set to {mode}"


def set_zappi_mode(mode: str) -> str:
    ha_service("select", "select_option", {
        "entity_id": ENTITIES["ev_zappi_mode"],
        "option": mode,
    })
    return f"Zappi set to {mode}"


def _compute_projected_3pm(now: datetime) -> tuple[int, int]:
    """
    Return (goal_3pm_soc, projected_3pm_soc) from current cycle context.
    Mirrors the HA card logic so the two stay consistent.
    """
    state       = _cycle_context.get("state", {})
    battery     = state.get("battery", {})
    solar       = state.get("solar", {})

    soc         = battery.get("soc_pct", 0) or 0
    battery_kwh = soc / 100 * 13.5
    home_load   = state.get("home_load_kw", 0) or 0
    is_peak     = state.get("is_peak_month", False)

    now_h        = now.hour + now.minute / 60
    hours_to_3pm = max(15.0 - now_h, 0.0)

    # Solar accuracy scaling
    actual_kw   = solar.get("current_kw", 0) or 0
    forecast_kw = solar.get("forecast_this_hour_kwh", 0) or 0
    if forecast_kw < 0.2 or now_h < 8:
        solar_scale = 1.0
    elif actual_kw / forecast_kw < 0.3:
        solar_scale = 0.0
    elif actual_kw / forecast_kw < 0.7:
        solar_scale = 0.5
    else:
        solar_scale = 1.0

    remaining_solar = (solar.get("forecast_remaining_kwh", 0) or 0) * solar_scale
    net_solar       = max(remaining_solar - home_load * hours_to_3pm, 0.0)

    # Grid charging contribution (assume self_consumption rate)
    reserve  = battery.get("reserve_pct", 5) or 5
    grid_kwh = 0.0
    if reserve > soc:
        headroom = max((reserve / 100 * 13.5) - battery_kwh, 0.0)
        grid_kwh = min(1.7 * hours_to_3pm, headroom)

    proj_kwh = min(battery_kwh + net_solar + grid_kwh, 13.5)
    proj_soc = round(proj_kwh / 13.5 * 100)
    goal_soc = 85 if is_peak else 80
    return goal_soc, proj_soc


def _maybe_write_daily_accuracy(now: datetime):
    """
    After 3pm, write one daily_accuracy record to decisions.jsonl if not done today.
    Compares what morning cycles projected for 3pm SoC against what actually happened.
    """
    if now.hour < 15 or not JSONL_FILE.exists():
        return

    today_str = now.strftime("%Y-%m-%d")
    lines     = [l for l in JSONL_FILE.read_text().splitlines() if l.strip()]

    today_records: list[dict] = []
    for line in lines:
        try:
            r = json.loads(line)
            if r.get("ts", "").startswith(today_str):
                today_records.append(r)
        except Exception:
            continue

    # Already written today?
    if any(r.get("record_type") == "daily_accuracy" for r in today_records):
        return

    # Actual 3pm SoC — first record at or after 15:00
    actual_3pm_soc = None
    for r in today_records:
        hour = int(r.get("ts", "T00")[11:13])
        if hour >= 15 and r.get("record_type") != "daily_accuracy":
            actual_3pm_soc = r.get("soc")
            break

    if actual_3pm_soc is None:
        return  # no post-3pm record yet — will try next cycle

    # Projections from key hours
    projections: dict[str, int | None] = {}
    for r in today_records:
        if r.get("record_type") == "daily_accuracy":
            continue
        hour = int(r.get("ts", "T00")[11:13])
        for h in (6, 8, 10, 12):
            if hour == h and str(h) not in projections:
                projections[str(h)] = r.get("projected_3pm_soc")

    goal_soc = today_records[-1].get("goal_3pm_soc", 80) if today_records else 80
    errors   = {h: ((v - actual_3pm_soc) if v is not None else None)
                for h, v in projections.items()}

    record = {
        "ts":             now.isoformat(),
        "record_type":    "daily_accuracy",
        "date":           today_str,
        "goal_3pm_soc":   goal_soc,
        "actual_3pm_soc": actual_3pm_soc,
        "projections":    projections,
        "projection_errors": errors,   # positive = over-estimated, negative = under-estimated
    }
    with JSONL_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"  Daily accuracy: goal={goal_soc}% actual={actual_3pm_soc}% "
          f"projections={projections} errors={errors}")


def log_decision(summary: str, actions_taken: list[str], ev_summary: str = "") -> str:
    now     = datetime.now(SYDNEY_TZ)
    actions = ", ".join(actions_taken) if actions_taken else "hold"
    entry   = f"[{now.strftime('%Y-%m-%d %H:%M')}] {summary} | Actions: {actions}"

    # Append to plain-text log (human-readable, committed to git)
    with LOG_FILE.open("a") as f:
        f.write(entry + "\n")

    # Append structured JSON record for the analyst agent
    state    = _cycle_context.get("state", {})
    battery  = state.get("battery", {})
    grid     = state.get("grid", {})
    solar    = state.get("solar", {})
    ev       = state.get("ev", {})
    forecast = _cycle_context.get("price_forecast", [])

    reserve_set = None
    mode_set    = None
    zappi_set   = None
    for a in actions_taken:
        m = re.search(r'set_reserve\((\d+)', a)
        if m:
            reserve_set = int(m.group(1))
        if "autonomous" in a:
            mode_set = "autonomous"
        elif "self_consumption" in a:
            mode_set = "self_consumption"
        if "Eco+" in a:
            zappi_set = "Eco+"
        elif "Eco" in a:
            zappi_set = "Eco"
        elif "Fast" in a:
            zappi_set = "Fast"
        elif "Off" in a:
            zappi_set = "Off"

    record = {
        "ts":                   now.isoformat(),
        # True only on cycles where the LLM narrative call failed and this row was written by
        # the deterministic fallback (2026-07-28 robustness hardening). Lets a liveness monitor
        # / analyst distinguish "agent degraded" cycles from normal ones.
        "llm_narrative_failed": _cycle_context.get("llm_narrative_failed", False),
        "soc":                  battery.get("soc_pct"),
        "reserve_before":       battery.get("reserve_pct"),
        "mode_before":          battery.get("mode"),
        "grid_target_pct":      battery.get("grid_target_pct"),
        "reserve_set":          reserve_set,
        "mode_set":             mode_set,
        # Rule 31 gentle-charge controller: on a self_consumption charge, reserve_set is the
        # small chased reserve (SoC+offset) while charge_target_pct is the SoC being charged
        # toward. charge_rate_intent is "gentle"/"full"; null on hold cycles. Lets build_models
        # correlate the commanded offset with realized battery_power to calibrate the dial.
        "charge_target_pct":    _cycle_context.get("rate_control", {}).get("charge_target_pct"),
        "reserve_cmd_pct":      _cycle_context.get("rate_control", {}).get("reserve_cmd_pct"),
        "charge_offset_pts":    _cycle_context.get("rate_control", {}).get("charge_offset_pts"),
        "charge_rate_intent":   _cycle_context.get("rate_control", {}).get("charge_rate_intent"),
        "zappi_set":            zappi_set,
        "price_c":              grid.get("price_cents_kwh"),
        "in_cheap_window":      grid.get("in_cheap_window"),
        "is_peak_month":        state.get("is_peak_month"),
        "in_demand_window":     state.get("in_demand_window"),
        "in_solar_sponge":      state.get("in_solar_sponge"),
        "forecast_accuracy":    solar.get("forecast_accuracy"),
        "solar_current_kw":     solar.get("current_kw"),
        "solar_sensor_unavail": solar.get("sensor_unavailable", False),
        "solar_remaining_kwh":  solar.get("forecast_remaining_kwh"),
        "solar_remaining_corrected_kwh": solar.get("forecast_remaining_corrected_kwh"),
        "solar_correction_ratio":        solar.get("correction_ratio"),
        "solar_this_hour_kwh":  solar.get("forecast_this_hour_kwh"),
        "solar_next_hour_kwh":  solar.get("forecast_next_hour_kwh"),
        "home_load_kw":         state.get("home_load_kw"),
        "fit_price_c":          grid.get("fit_price_cents_kwh"),
        "fit_descriptor":       grid.get("fit_descriptor"),
        "ev_plugged":           ev.get("plugged_in"),
        "ev_soc":               ev.get("ev_soc_pct"),
        "ev_zappi_mode_before": ev.get("zappi_mode"),
        "price_forecast_6h":    [f["cents_kwh"] for f in forecast[:12]],
        "price_forecast_6h_times": [f["time"] for f in forecast[:12]],
        "tomorrow_solar_outlook":  _cycle_context.get("weather_forecast", {}).get("tomorrow_solar_outlook"),
        "tomorrow_avg_radiation":  _cycle_context.get("weather_forecast", {}).get("tomorrow_avg_radiation"),
        "input_tokens":         _cycle_context.get("input_tokens", 0),
        "output_tokens":        _cycle_context.get("output_tokens", 0),
        "cache_read_tokens":    _cycle_context.get("cache_read_tokens", 0),
        "cache_write_tokens":   _cycle_context.get("cache_write_tokens", 0),
        "est_cost_usd":         round(
                                    # input_tokens = non-cached only; cache tokens are tracked separately
                                    _cycle_context.get("input_tokens", 0)       / 1_000_000 * COST_PER_1M_INPUT +
                                    _cycle_context.get("cache_write_tokens", 0) / 1_000_000 * COST_PER_1M_CACHE_WRITE +
                                    _cycle_context.get("cache_read_tokens", 0)  / 1_000_000 * COST_PER_1M_CACHE_READ +
                                    _cycle_context.get("output_tokens", 0)      / 1_000_000 * COST_PER_1M_OUTPUT,
                                    5),
        "actions":              actions_taken,
        "summary":              summary,
        "demand_reserve_guard_fired": _demand_reserve_guard_fired,
        # True on cycles where the user held manual control, so these can be
        # excluded from divergence/accuracy analysis — the rule layer's verdict
        # was computed but never acted on.
        "manual_override":           bool(_cycle_context.get("manual_override", False)),
        # Control inputs actually used this cycle, plus anything that failed its
        # range check. HA's recorder was found not to be capturing these helpers
        # (2026-07-23), so this is the audit trail for slider drift — it pins any
        # change to a 30-minute cycle without depending on HA history.
        "settings_used":             _cycle_context.get("settings_validated", {}),
        "settings_violations":       _cycle_context.get("settings_violations", []),
    }

    goal_3pm, proj_3pm = _compute_projected_3pm(now)
    record["goal_3pm_soc"]      = goal_3pm
    record["projected_3pm_soc"] = proj_3pm

    # Shadow comparison (Phase 3): log the deterministic verdict next to what the LLM
    # actually did, so divergence can be measured over the coming weeks before cutover.
    ctx = _cycle_context.get("decision_context")
    if ctx:
        rec = ctx["recommended"]
        rec    = ctx["recommended"]
        ev_rec = ctx["ev_recommended"]
        record["computed_verdict"]    = rec
        record["computed_ev_verdict"] = ev_rec
        record["computed_context"]  = {k: ctx[k] for k in (
            "zero_solar_day", "deferral_detected", "sliding_forecast", "solar_unreliable",
            "cost_target_pct", "hours_to_cheap_end", "hours_to_deadline", "kwh_needed_85",
            "spread_c", "forward_min_c", "price_used_c", "price_spot_c", "go_hard_slot")}
        soc_now      = record.get("soc")
        actual_charge = ((reserve_set is not None and soc_now is not None and reserve_set > soc_now)
                         or mode_set == "autonomous")
        rec_charge   = rec["action"] == "charge"
        record["shadow_action_match"] = (actual_charge == rec_charge)
        record["shadow_mode_match"]   = ((mode_set == rec["mode"])
                                         if (rec_charge and mode_set) else None)
        # EV shadow match — only meaningful when EV is plugged in
        if ev_rec.get("rule_fired") != "ev_disconnected":
            record["shadow_ev_match"] = (zappi_set == ev_rec.get("zappi_mode"))

    # LP optimiser shadow fields (three-way A/B: LLM vs deterministic vs optimiser)
    ov = _cycle_context.get("optimizer_verdict")
    if ov is not None:
        record["optimizer_verdict"]  = ov
        record["optimizer_context"]  = _cycle_context.get("optimizer_context")
        _soc = record.get("soc")
        _llm_charge = ((reserve_set is not None and _soc is not None and reserve_set > _soc)
                       or mode_set == "autonomous")
        record["optimizer_action_match"] = (_llm_charge == (ov.get("action") == "charge"))
        _cv = record.get("computed_verdict")
        if _cv is not None:
            record["optimizer_vs_deterministic"] = (ov.get("action") == _cv.get("action"))

    with JSONL_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")

    if _HAVE_DATA_LOGGER:
        try:
            _cid = _cycle_context.get("db_cycle_id")
            if _cid:
                data_logger.log_agent_decision(_cid, summary, actions_taken)
        except Exception as _exc:
            print(f"  Warning: data_logger.log_agent_decision failed — {_exc}", file=sys.stderr)

    _maybe_write_daily_accuracy(now)

    # Compute today's cumulative cost from JSONL and push to HA
    try:
        today_str   = now.strftime("%Y-%m-%d")
        daily_cost  = 0.0
        daily_input = 0
        daily_output = 0
        if JSONL_FILE.exists():
            for line in JSONL_FILE.read_text().splitlines():
                try:
                    entry = json.loads(line)
                    if entry.get("ts", "").startswith(today_str):
                        daily_cost   += entry.get("est_cost_usd", 0)
                        daily_input  += entry.get("input_tokens", 0)
                        daily_output += entry.get("output_tokens", 0)
                except Exception:
                    pass
        ha_set_state("sensor.agent_daily_cost", f"{daily_cost:.4f}", {
            "friendly_name":      "Agent — Daily API Cost",
            "unit_of_measurement": "USD",
            "state_class":        "total_increasing",
            "daily_input_tokens":  daily_input,
            "daily_output_tokens": daily_output,
            "pricing_model":      "claude-opus-4-5 list price — verify at console.anthropic.com",
        })
    except Exception as exc:
        print(f"  Warning: daily cost push failed: {exc}", file=sys.stderr)

    # Per-cycle battery/EV notifications. Muted when the user has enabled quiet mode
    # (input_boolean.agent_narrative_disable, Rule 36) — they reappear every cycle via
    # fixed notification_id, which is the "notifications still firing" the toggle is meant
    # to stop. Everything else below (logbook, dashboard helpers, JSONL, heartbeat) still
    # runs so the audit trail and Phase-4 data are unaffected — only the popups go quiet.
    _quiet = bool(_cycle_context.get("narrative_disabled"))
    if _quiet:
        # Clear any popups already on screen so quiet mode actually goes quiet — they
        # persist until dismissed, so without this the last pair would linger. They are
        # transient UI, recreated the instant the toggle goes back off; nothing is lost.
        for _nid in ("energy_agent_battery", "energy_agent_ev"):
            ha_service("persistent_notification", "dismiss", {"notification_id": _nid})

    # Battery notification
    battery_actions = [a for a in actions_taken if not a.startswith("set_zappi")]
    battery_actions_str = ", ".join(battery_actions) if battery_actions else "hold"
    if not _quiet:
        ha_service("persistent_notification", "create", {
            "notification_id": "energy_agent_battery",
            "title": f"🔋 Battery — {now.strftime('%H:%M')}",
            "message": f"{summary}\n\n**Actions:** {battery_actions_str}",
        })

    # EV notification — always sent (unless quiet); ev_summary carries EV SoC from sensor.polestar_7853_battery_charge_level
    ev_actions = [a for a in actions_taken if a.startswith("set_zappi")]
    ev_msg = ev_summary if ev_summary else summary
    ev_actions_str = ", ".join(ev_actions) if ev_actions else "hold"
    if not _quiet:
        ha_service("persistent_notification", "create", {
            "notification_id": "energy_agent_ev",
            "title": f"🚗 EV — {now.strftime('%H:%M')}",
            "message": f"{ev_msg}\n\n**Actions:** {ev_actions_str}",
        })

    # Write a logbook entry — sequential history in HA History panel
    ha_service("logbook", "log", {
        "name": "Energy Agent",
        "message": f"{actions} | {summary}",
        "entity_id": "sensor.tessie_powerwall_charge",
    })

    # Update dashboard helper entities so the "Last Battery Decision" card stays live.
    # Read live sensor snapshot — values reflect state at decision time.
    try:
        soc           = _int(ha_state(ENTITIES["battery_soc"]))
        price         = round(_float(ha_state(ENTITIES["grid_price"])) * 100, 1)
        solar_rem     = round(_float(ha_state(ENTITIES["solar_remaining"])), 1)
        grid_target   = _int(ha_state(ENTITIES["battery_target"]))

        # Reserve set: extract from actions list (e.g. "set_reserve(62%)"), else current
        reserve_set = _int(ha_state(ENTITIES["battery_reserve"]))
        for a in actions_taken:
            m = re.search(r'set_reserve\((\d+)', a)
            if m:
                reserve_set = int(m.group(1))
                break

        ha_service("input_text", "set_value", {
            "entity_id": "input_text.battery_decision_automation",
            "value": f"⚡ Energy Agent — {now.strftime('%H:%M')}",
        })
        ha_service("input_text", "set_value", {
            "entity_id": "input_text.battery_decision_action",
            "value": summary[:255],
        })
        ha_service("input_number", "set_value", {
            "entity_id": "input_number.battery_decision_soc",
            "value": soc,
        })
        ha_service("input_number", "set_value", {
            "entity_id": "input_number.battery_decision_price",
            "value": price,
        })
        ha_service("input_number", "set_value", {
            "entity_id": "input_number.battery_decision_solar_remaining",
            "value": solar_rem,
        })
        ha_service("input_number", "set_value", {
            "entity_id": "input_number.battery_decision_grid_target",
            "value": grid_target,
        })
        ha_service("input_number", "set_value", {
            "entity_id": "input_number.battery_decision_reserve_set",
            "value": reserve_set,
        })
    except Exception as exc:
        print(f"  Warning: dashboard helper update failed: {exc}", file=sys.stderr)

    return "Logged"


# ---------------------------------------------------------------------------
# Short-term memory — last N decisions for deferral detection
# ---------------------------------------------------------------------------

def get_recent_decisions(n: int = 3) -> str:
    """
    Return the last n decisions from decisions.jsonl as a compact context block.
    Injected into each cycle's initial message so the agent can detect when it
    has been deferring on a forecast that repeatedly doesn't arrive.
    """
    if not JSONL_FILE.exists():
        return "No prior decisions on record."

    lines = [l for l in JSONL_FILE.read_text().splitlines() if l.strip()]
    recent: list[dict] = []
    for line in reversed(lines):
        try:
            recent.append(json.loads(line))
        except Exception:
            continue
        if len(recent) >= n:
            break

    if not recent:
        return "No recent decisions on record."

    parts = []
    for r in reversed(recent):  # chronological order
        ts            = r.get("ts", "")[:16].replace("T", " ")
        soc           = r.get("soc", "?")
        price         = r.get("price_c", "?")
        reserve_before = r.get("reserve_before", "?")
        reserve_set   = r.get("reserve_set")
        reserve_str   = (f"{reserve_before}%→{reserve_set}%"
                         if reserve_set is not None else f"{reserve_before}% (unchanged)")
        actions       = r.get("actions", [])
        action_str    = ", ".join(actions) if actions else "hold"
        summary       = r.get("summary", "")[:160]
        solar_kw   = r.get("solar_current_kw", "?")
        accuracy   = r.get("forecast_accuracy") or ""
        parts.append(
            f"  [{ts}] SoC={soc}% price={price}¢ reserve={reserve_str} "
            f"solar={solar_kw}kW ({accuracy[:20]}) | {action_str}\n"
            f"    \"{summary}\""
        )

    return "\n".join(parts)


def get_recent_records(n: int = 3) -> list[dict]:
    """Return the last n non-accuracy decision records as parsed dicts (chronological)."""
    if not JSONL_FILE.exists():
        return []
    lines = [l for l in JSONL_FILE.read_text().splitlines() if l.strip()]
    recent: list[dict] = []
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("record_type") == "daily_accuracy":
            continue
        recent.append(r)
        if len(recent) >= n:
            break
    return list(reversed(recent))


# ---------------------------------------------------------------------------
# Pure decision logic — deterministic precompute (Phase 1 of re-architecture)
#
# compute_decision_context() takes plain data in and returns a dict out: no HTTP,
# no clock reads (now is passed in), no globals. This makes every calculation the
# system prompt currently asks the LLM to do in its head unit-testable in
# isolation. It is NOT yet wired into the control path — it runs in shadow only.
# ---------------------------------------------------------------------------

USABLE_KWH       = 13.5
SLOW_KW          = 1.7
FAST_KW          = 5.0
DEMAND_DEADLINE  = 14 + 55 / 60   # 2:55pm as a decimal hour

# Gentle-charge controller (Rule 31). On a self_consumption charge the agent chases
# reserve = SoC + SELF_CONS_CHARGE_OFFSET_PTS rather than a fixed high reserve, so the
# reserve−SoC gap stays small and the Powerwall trickles instead of slamming 5 kW under
# firmware 26.18.3. Measured dial (2026-07-24, at 63–65% SoC): gap 5 → 1.67 kW, gap 10 →
# 3.96 kW, gap 20+ → 5 kW. Offset 6 → ~2.1 kW instantaneous, ~1.6 kW cycle-average once the
# gap fills mid-cycle and the taper idles it — matching the 1.67 kW the rule tree budgets.
# Tunable; the commanded offset is logged so build_models can learn the real curve later.
SELF_CONS_CHARGE_OFFSET_PTS = 6

# Overnight survival floor (Rule 30, revised 2026-07-25). The rule layer defends a ~12%
# instantaneous SoC floor so it never rides down into the `battery_low_soc_emergency_charge`
# automation's 10% trigger — which sets reserve=85 directly (a 5 kW slam) and then fought the
# next HOLD that cleared reserve back to 5% (the 2026-07-25 07:00 oscillation). Instead of
# holding while SoC drains below the floor, the agent issues a gentle self_consumption top-up
# (Rule 31, ~1.6 kW). The automation stays at 10% as a true "agent is dead / stalled" backstop:
# the 2-point margin means it only takes over after ~2 missed agent cycles, which is exactly
# when it should. Applies year-round (small cost, keeps the battery off the floor). Only ever
# overrides a HOLD verdict — never a deadline autonomous charge — and never in the demand window.
# Kill-switch: SURVIVAL_FLOOR_DEFENSE = False reverts to the old ride-to-5% behaviour.
SURVIVAL_FLOOR_DEFENSE       = True
OVERNIGHT_SURVIVAL_FLOOR_PCT = 12   # gently charge below this instantaneous SoC
SURVIVAL_FLOOR_TARGET_PCT    = 20   # the gentle top-up aims here (buffer above the 10% backstop)

# PRICE_USE_30MIN_SLOT (Rule 32, 2026-07-25): anchor every price threshold on the current
# 30-min slot (price_forecast[0]) rather than the raw 5-min settlement sample. The
# `..._general_price` sensor carries duration:5, so a single per-cycle sample is a coin-flip —
# on 2026-07-23 it crossed the 10¢ EV threshold six times in twenty minutes, and the 12:00
# cycle sampled 9¢ (→ EV Fast) while the 30-min value was 11¢ (→ Eco). forecast[0] is that same
# interval bucketed and averaged over its 5-min sub-intervals, so the anchor now matches the
# 30-min granularity the forward spread/forward_min already use. Flip to False to revert to the
# spot sample.
PRICE_USE_30MIN_SLOT = True

# Receding-horizon deadline escalation (Rule 33, 2026-07-26). The peak-month deadline branch
# used to jump straight to autonomous (5 kW) the instant self_consumption could no longer fill
# the WHOLE remaining 85% gap in the time left (fill_slow_85 >= hours_to_2_55). On 2026-07-26 that
# fired at 10:00 with SoC 16% and ~4.9h to the deadline — but a 5 kW charge fills in <2h, so there
# were ~3h of slack. It slammed 5 kW at the worst-informed moment of the day (morning solar credit
# is ~0 because Solcast over-forecasts winter mornings ~7×), when gentle self_consumption + midday
# solar would have made 85% comfortably. Fix: escalate to autonomous only at the FAST rate's
# point-of-no-return — when hours_to_2_55 <= fill_fast_85 + FAST_ESCALATE_BUFFER_H. Below that,
# lead with a gentle self_consumption charge (peak_deadline_gentle_lead) that makes progress while
# holding the 5 kW option in reserve; each cycle re-evaluates with fresher solar/price/SoC. The
# buffer IS the demand-charge safety margin: it guarantees there is always enough time to finish at
# 5 kW even if solar craters, so the ~$100/month demand charge stays protected. Bigger buffer =
# escalate earlier (safer, more premature 5 kW); smaller = leaner. Kill-switch: DEADLINE_GENTLE_LEAD
# = False reverts to the old straight-to-autonomous behaviour.
DEADLINE_GENTLE_LEAD   = True
FAST_ESCALATE_BUFFER_H = 1.5   # go autonomous when hours_to_2_55 <= fill_fast_85 + this

# Rule 35 — peak-eve run-up. The peak-deadline block (Rule 13) is gated `now_h < DEMAND_DEADLINE`,
# so on a peak-month day the 9pm–midnight window (after the demand window closes at 9pm, before the
# clock wraps back under the deadline at midnight) fell through to the NON-peak escalation chain —
# which lacks the two peak protections: the _cheapest_go_hard_slot() check and Rule 33 gentle-lead
# damping. On 2026-07-30 23:00 that chain slammed 5 kW autonomous at 19¢ (nonpeak_solar_unreliable_
# autonomous) when the LP correctly held for the 12¢ morning sponge slot. Fix: run the peak block
# through the peak-eve evening too, relying on the existing hours_to_2_55 day-wrap (line ~1708) to
# target tomorrow's 2:55pm. The demand window itself (3–9pm) is still handled first by the
# `in_demand` branch, and the afternoon-only peak_deadline_quickcheck heuristic is guarded to the
# real run-up so it can't fire at 11pm. Kill-switch: PEAK_EVE_RUNUP = False reverts to the old
# fall-through (peak block off after 2:55pm).
PEAK_EVE_RUNUP = True

# Phase 2.5-A: charge rate model (SoC-dependent rates from logged observations).
# Built from energy_log.db; falls back to SLOW_KW/FAST_KW for missing/low-sample buckets.
MODEL_PARAMS_FILE = Path(__file__).parent / "model_params.json"
_model_params: dict = {}
_charge_rate_model: dict = {}
try:
    with MODEL_PARAMS_FILE.open() as _f:
        _model_params      = json.load(_f)
        _charge_rate_model = _model_params.get("charge_rate_kw", {})
except Exception:
    pass

_MODEL_MIN_SAMPLES = 5


def _avg_charge_rate_kw(soc_from: float, soc_to: float, mode: str) -> float:
    """Weighted-average charge rate from soc_from% to soc_to% using model_params.json.

    Segments the SoC range into 10%-point buckets and averages the rates weighted
    by the kWh in each segment. Falls back to SLOW_KW / FAST_KW for missing or
    low-sample buckets so missing autonomous data is handled gracefully.
    """
    fallback = SLOW_KW if mode == "self_consumption" else FAST_KW
    if soc_to <= soc_from:
        return fallback
    table = (_charge_rate_model or {}).get(mode, {})
    total_kwh = (soc_to - soc_from) / 100.0 * USABLE_KWH
    total_time = 0.0
    bucket_floor = int(soc_from / 10) * 10
    while bucket_floor < soc_to:
        seg_bot = max(float(bucket_floor), soc_from)
        seg_top = min(float(bucket_floor + 10), soc_to)
        if seg_top > seg_bot:
            seg_kwh = (seg_top - seg_bot) / 100.0 * USABLE_KWH
            entry = table.get(str(bucket_floor))
            rate = entry["kw"] if (entry and entry.get("n", 0) >= _MODEL_MIN_SAMPLES) else fallback
            total_time += seg_kwh / rate
        bucket_floor += 10
    return round(total_kwh / total_time, 3) if total_time > 0 else fallback


def _gentle_charge_reserve(soc, target_pct: int,
                           offset: int = SELF_CONS_CHARGE_OFFSET_PTS) -> int:
    """Reserve to command for a gentle self_consumption charge (Rule 31).

    Returns ``min(SoC + offset, target_pct)``: a small reserve−SoC gap so the Powerwall
    trickles (~1.6 kW cycle-average) rather than slamming 5 kW, capped at the charge target
    so it can never overshoot or drive export, tapering to a stop as SoC climbs into the
    target. It is a *chase*, not set-and-forget — re-set each 30-min cycle as SoC rises.

    ``soc is None`` (Tessie/gateway both unreadable) falls back to the old fixed-reserve
    behaviour, which is safe: a missing SoC means we can't compute a gap, and reserve =
    target is exactly what shipped before this controller existed.
    """
    if soc is None:
        return target_pct
    return min(int(soc) + offset, target_pct)


def _accuracy_class(label: str) -> str:
    """Collapse the verbose forecast_accuracy string into one of: good/poor/unreliable/na."""
    s = (label or "").lower()
    if s.startswith("good"):
        return "good"
    if s.startswith("poor"):
        return "poor"
    if s.startswith("unreliable"):
        return "unreliable"
    return "na"


CHEAP_BAND_ALPHA = 0.30   # cheap = bottom 30% of today's price swing (trough → evening peak)
MIN_DAILY_SWING  = 5.0    # ¢ — below this the day is flat; no meaningful cheap window to "end"


def _hours_to_cheap_end(price_forecast: list[dict], current_price: float,
                        alpha: float = CHEAP_BAND_ALPHA) -> float:
    """Hours until today's cheap trough ends — i.e. price climbs out of the bottom
    `alpha`-band of the day's own range and starts the ramp toward the evening peak.

    Scale-free by design: normalises to each day's own minimum and *evening* peak
    (15:00–21:00) rather than absolute cents or a fixed +N¢ jump. This captures the
    structural daily shape (morning bump → noon trough → evening peak) on both a
    10–20¢ day and a 30–80¢ day. Anchoring the high to the evening window keeps the
    morning bump from inflating the range. Returns hours to the end of the cheap
    region that lies ahead; 0.0 if no cheap interval remains; 6.0 if it never ends
    within the horizon. Assumes uniform 30-min spacing.
    """
    prices = [f.get("cents_kwh", 0.0) for f in price_forecast]
    if len(prices) < 2:
        return 6.0

    evening = []
    for f in price_forecast:
        t  = f.get("time", "")
        hh = t[11:13] if len(t) >= 13 else ""
        if hh.isdigit() and 15 <= int(hh) < 21:
            evening.append(f.get("cents_kwh", 0.0))
    p_peak = max(evening) if evening else max(prices)
    p_min  = min(prices)
    rng    = p_peak - p_min
    if rng < MIN_DAILY_SWING:
        return 6.0                       # flat day — no real trough/ramp to track

    threshold = p_min + alpha * rng
    start = next((i for i, p in enumerate(prices) if p <= threshold), None)
    if start is None:
        return 0.0                       # already above the cheap band — window closed
    for j in range(start, len(prices) - 1):
        if prices[j] > threshold and prices[j + 1] > threshold:
            return j * 0.5               # right edge of the cheap region
    return 6.0


def _detect_deferral(recent_records: list[dict], current_price: float) -> bool:
    """2+ consecutive holds while price stayed within 2¢ → the awaited cheap window isn't coming."""
    if len(recent_records) < 2:
        return False
    holds = 0
    for r in reversed(recent_records):
        actions = r.get("actions") or []
        price   = r.get("price_c")
        if not actions and price is not None and abs(price - current_price) <= 2.0:
            holds += 1
        else:
            break
    return holds >= 2


def _cheapest_go_hard_slot(
    price_forecast: list[dict],
    current_price_c: float,
    soc: float,
    home_load_kw: float,
    hours_to_deadline: float,
    safety_buffer_h: float = 0.5,
    min_saving_c: float = 1.0,
) -> "tuple[float, float] | None":
    """Find the cheapest upcoming slot where fast-charging to 85% before the deadline is still feasible.

    Conservative SoC projection at each slot: home load drains the battery during the wait,
    no solar credit (pessimistic — real outcome will be better when solar is producing).

    Returns (cheapest_price_c, hours_until_slot) or None if no slot is cheaper by min_saving_c.
    """
    best_price = current_price_c - min_saving_c  # must beat this to be worth returning
    best_hours: float | None = None
    for i, f in enumerate(price_forecast):
        hours_until = (i + 1) * 0.5          # 30-min slots
        slot_price  = f.get("cents_kwh", current_price_c)
        # Conservative SoC at this slot (home load draining, no solar).
        # Clip at 5% — below that the Powerwall stops discharging and grid covers home load,
        # but the slot is still reachable (battery just sits at floor until we charge).
        soc_at_slot = max(5.0, soc - home_load_kw * hours_until / USABLE_KWH * 100)
        kwh_needed  = max((0.85 - soc_at_slot / 100) * USABLE_KWH, 0.0)
        fill_fast_h = kwh_needed / FAST_KW
        # Feasible = can finish fast-fill AND have safety buffer before deadline
        if hours_until + fill_fast_h + safety_buffer_h <= hours_to_deadline:
            if slot_price < best_price:
                best_price = slot_price
                best_hours = hours_until
    if best_hours is not None:
        return best_price, best_hours
    return None


def _detect_sliding_forecast(recent_records: list[dict], current_price: float,
                             current_forward_min: float, gap_threshold: float = 2.0,
                             min_cycles: int = 3) -> bool:
    """Amber cheap window keeps being forecast as upcoming but never arrives.

    Fires when ALL of the last `min_cycles` records (including now) show:
      - a cheaper window was forecast (forward_min < price - gap_threshold), AND
      - the cheap window hadn't actually arrived yet (actual price was still above
        current_forward_min + gap_threshold, i.e. we never got there)

    If the window were real it would have arrived by now. If it keeps being 1–2h away
    each cycle, the forecast is sliding and the agent should stop deferring.
    """
    if len(recent_records) < min_cycles - 1:
        return False

    # Check current cycle first
    if current_forward_min >= current_price - gap_threshold:
        return False  # no meaningful cheap window forecast right now

    # Check the last (min_cycles - 1) records
    consecutive = 0
    for r in reversed(recent_records):
        ctx = r.get("computed_context") or {}
        rec_price       = r.get("price_c")
        rec_forward_min = ctx.get("forward_min_c")
        if rec_price is None or rec_forward_min is None:
            break
        # Was a cheap window forecast in this record?
        if rec_forward_min >= rec_price - gap_threshold:
            break  # cheap window wasn't forecast that cycle — stop counting
        # Did the cheap window actually arrive? (actual price dropped close to forward_min)
        if rec_price <= rec_forward_min + gap_threshold:
            break  # price reached the cheap band — window arrived, not sliding
        consecutive += 1
        if consecutive >= min_cycles - 1:
            return True
    return False


SOLAR_START_HOUR = 9  # flat roof in Sydney: panels don't produce meaningfully before ~9am

def _detect_zero_solar(recent_records: list[dict], current_solar_kw: float,
                       now_h: float, sensor_unavailable: bool = False,
                       solcast_remaining_kwh: float = 0.0) -> bool:
    """0 kW actual in 2+ of the last 3 daylight cycles (incl. now) → zero-solar day.
    Only active from SOLAR_START_HOUR onward — before then, zero output is expected
    (low sun angle) and must not be counted as evidence of a zero-solar day.
    sensor_unavailable=True means HA returned "unavailable" — don't count as a zero cycle.
    If Solcast still forecasts > 2 kWh remaining, near-zero actual is just the morning
    ramp-up or SolarEdge API lag — don't override the forecast."""
    if now_h < SOLAR_START_HOUR:
        return False
    # Before 10am: near-zero actual is just the morning ramp (low sun angle + SolarEdge
    # API 15-min lag). Don't override a healthy Solcast forecast at this hour.
    # From 10am onward: if panels are still near-zero during Solar Sponge, that IS
    # evidence of a cloudy day — let the detection proceed regardless of Solcast.
    if solcast_remaining_kwh > 2.0 and now_h < 10.0:
        return False
    # Current cycle shows real solar — can't be a zero-solar day regardless of history.
    # This also clears stale history when the sensor source changes mid-day.
    if current_solar_kw > 0.1 and not sensor_unavailable:
        return False
    if sensor_unavailable:
        zeros = 0
    else:
        zeros = 1  # current is ≤ 0.1
    for r in recent_records[-3:]:
        ts_hour = int(r.get("ts", "T00")[11:13] or 0) if r.get("ts") else 0
        if ts_hour >= SOLAR_START_HOUR and (r.get("solar_current_kw") or 0) <= 0.1:
            zeros += 1
    return zeros >= 2


def load_price_history(days: int = PRICE_HISTORY_DAYS) -> list[float]:
    """Read price_c values from the last N days of JSONL records.
    Returns a sorted list of floats (¢/kWh). Returns [] if file missing."""
    if not JSONL_FILE.exists():
        return []
    cutoff = datetime.now(SYDNEY_TZ) - timedelta(days=days)
    prices = []
    try:
        for line in JSONL_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("type") == "daily_accuracy":
                continue
            price = r.get("price_c")
            ts    = r.get("ts", "")
            if price is None or not ts:
                continue
            try:
                # Parse timestamp — may have timezone offset
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = SYDNEY_TZ.localize(dt)
                if dt >= cutoff:
                    prices.append(float(price))
            except (ValueError, TypeError):
                continue
    except Exception:
        return []
    return sorted(prices)


def _price_stats(prices: list[float]):
    """Compute p25 / p75 from a sorted price list. Returns None if too few records."""
    if len(prices) < MIN_HISTORY_RECORDS:
        return None
    n    = len(prices)
    p25  = prices[int(n * 0.25)]
    p75  = prices[int(n * 0.75)]
    pmin = prices[0]
    pmax = prices[-1]
    return {"p25": p25, "p75": p75, "pmin": pmin, "pmax": pmax, "n": n}


def _build_hourly_price_model() -> dict[int, float]:
    """Per-hour-of-day median price from the last PRICE_HISTORY_DAYS of decisions.jsonl.
    Returns {hour: median_price_c}; only hours with ≥3 samples are included.
    Used to extend the LP forecast horizon beyond Amber's ~6h window."""
    if not JSONL_FILE.exists():
        return {}
    cutoff = datetime.now(SYDNEY_TZ) - timedelta(days=PRICE_HISTORY_DAYS)
    by_hour: dict[int, list[float]] = {}
    try:
        for line in JSONL_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("type") == "daily_accuracy":
                continue
            price = r.get("price_c")
            ts    = r.get("ts", "")
            if price is None or not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = SYDNEY_TZ.localize(dt)
                if dt >= cutoff:
                    h = dt.hour
                    by_hour.setdefault(h, []).append(float(price))
            except (ValueError, TypeError):
                continue
    except Exception:
        return {}
    result = {}
    for h, p_list in by_hour.items():
        if len(p_list) >= 3:
            s = sorted(p_list)
            result[h] = s[len(s) // 2]
    return result


def _extend_forecast_to_demand_window(
    price_forecast: list[dict],
    now: datetime,
    hourly_model: dict[int, float],
) -> list[dict]:
    """Extend the Amber ~6h price forecast with synthetic 30-min slots until 22:00.

    Fills the 15:00–21:00 demand-window block so the LP can see the
    demand_penalty_c on those slots and pre-charge earlier in the day.
    Uses per-hour historical medians from the last 7 days; falls back to
    p75 of the existing forecast for hours with insufficient history.
    """
    if not price_forecast:
        return price_forecast
    last_time_str = price_forecast[-1].get("time", "")
    try:
        last_dt = datetime.fromisoformat(last_time_str.replace("T", " "))
        if last_dt.tzinfo is None:
            last_dt = SYDNEY_TZ.localize(last_dt)
    except (ValueError, TypeError):
        return price_forecast
    target_end = last_dt.replace(hour=22, minute=0, second=0, microsecond=0)
    if target_end <= last_dt:
        return price_forecast  # already reaches 22:00+
    existing_prices = sorted(f.get("cents_kwh", 15.0) for f in price_forecast)
    fallback = existing_prices[int(len(existing_prices) * 0.75)] if existing_prices else 15.0
    extended = list(price_forecast)
    slot_dt = last_dt + timedelta(minutes=30)
    while slot_dt <= target_end:
        h = slot_dt.hour
        extended.append({
            "time": slot_dt.strftime("%Y-%m-%d %H:%M"),
            "cents_kwh": hourly_model.get(h, fallback),
            "descriptor": "synthetic_historical",
        })
        slot_dt += timedelta(minutes=30)
    return extended


def _historical_grid_target(soc: float, solar_forecast_kwh: float,
                             confidence: float, p_now: float,
                             stats: dict, max_floor: float = DEFAULT_MAX_INSURANCE_FLOOR) -> float:
    """Compute grid charge target using rolling historical price percentiles.

    Two components combined with max():

    1. Solar-adjusted target: discount solar trust when prices are cheap
       (low cost of over-charging → be more aggressive from grid).
    2. Insurance floor: maintain a minimum SoC proportional to price cheapness,
       guarding against the cheap window closing earlier than forecast.

    price_position = 0.0  →  P_now at or below p25 (very cheap by recent standards)
    price_position = 1.0  →  P_now at or above p75 (normal/expensive)
    """
    swing = stats["p75"] - stats["p25"]
    if swing < 2.0:
        # Flat price history — no meaningful percentile signal, fall back
        return None
    price_position = max(0.0, min(1.0, (p_now - stats["p25"]) / swing))

    # 1. Solar trust: scale solar contribution by price_position.
    #    At price_position=0 (very cheap) → trust solar 0% (fill from grid).
    #    At price_position=1 (normal)     → trust solar fully.
    solar_trusted_kwh = solar_forecast_kwh * confidence * price_position
    solar_target = max(5.0, min(95.0, 95.0 - (solar_trusted_kwh / USABLE_KWH * 100)))

    # 2. Insurance floor: decreases as price rises toward p75.
    insurance_floor = max_floor * (1.0 - price_position)

    target = max(solar_target, insurance_floor)
    return round(max(soc, min(95.0, target)), 1)


def compute_decision_context(state: dict, price_forecast: list[dict],
                             recent_records: list[dict], now: datetime) -> dict:
    """Deterministic decision context + recommended verdict. Pure function."""
    now_h    = now.hour + now.minute / 60
    battery  = state.get("battery", {})
    grid     = state.get("grid", {})
    solar    = state.get("solar", {})
    settings = state.get("settings", {})

    # ALWAYS the Tessie reading — never the floor-clipped gateway value.
    soc          = battery.get("soc_pct", 0) or 0
    grid_target  = battery.get("grid_target_pct", 5) or 5
    price_spot   = grid.get("price_cents_kwh", 0.0) or 0.0   # raw 5-min settlement sample
    # Rule 32 — decide on the current 30-min slot, not the 5-min spot. price_forecast[0] is the
    # current interval averaged over its sub-intervals (see get_price_forecast); far more stable
    # and the same source the forward-looking spread/forward_min already use. Falls back to the
    # spot sample if the forecast is empty (agent flying blind) or the flag is off.
    _slot0 = price_forecast[0].get("cents_kwh") if price_forecast else None
    price        = (_slot0 if (PRICE_USE_30MIN_SLOT and _slot0 is not None) else price_spot)
    fit_price    = grid.get("fit_price_cents_kwh", 0.0) or 0.0
    is_peak      = state.get("is_peak_month", False)
    in_demand    = state.get("in_demand_window", False)
    in_sponge    = state.get("in_solar_sponge", False)
    solar_now          = solar.get("current_kw", 0.0) or 0.0
    solar_unavailable  = solar.get("sensor_unavailable", False)
    remaining_raw      = solar.get("forecast_remaining_kwh", 0.0) or 0.0
    # Prefer the bias-corrected forecast (Rule 29). Raw Solcast runs ~2x optimistic
    # at this site in winter, which made the rule layer hold for solar that never
    # arrived. Falls back to raw when the correction is unavailable, so a Solcast
    # attribute outage degrades to previous behaviour rather than to zero solar.
    _remaining_corr    = solar.get("forecast_remaining_corrected_kwh")
    remaining          = (_remaining_corr if (USE_CORRECTED_SOLAR and _remaining_corr is not None)
                          else remaining_raw)
    accuracy           = _accuracy_class(solar.get("forecast_accuracy", ""))

    zero_solar_day    = _detect_zero_solar(recent_records, solar_now, now_h, solar_unavailable, remaining)
    deferral_detected = _detect_deferral(recent_records, price)

    # Overnight hold: Solar Sponge (10am–3pm) is structurally cheaper than overnight
    # prices. Don't charge overnight when Solar Sponge tomorrow will be cheaper.
    # Fires when: nighttime (20:00–07:00) AND price > SOLAR_SPONGE_PRICE_THRESHOLD
    # AND SoC is not critically low (> 25% — emergency automation handles the floor).
    # Peak months: also apply — Rule 13 morning deadline maths will escalate if needed.
    is_night = now_h >= 20 or now_h < 7
    overnight_hold = (is_night
                      and price > SOLAR_SPONGE_PRICE_THRESHOLD
                      and soc >= 25)
    # Accuracy-based unreliability only valid from SOLAR_START_HOUR — before then,
    # Solcast forecasts >0 but panels haven't started yet (low sun angle, flat roof).
    # zero_solar_day has its own time guard via _detect_zero_solar.
    solar_unreliable  = (accuracy in ("poor", "unreliable") and now_h >= SOLAR_START_HOUR) or zero_solar_day

    # Confidence factor for solar forecast
    confidence_factor = {"good": 1.0, "poor": 0.5, "unreliable": 0.0, "na": 1.0}.get(accuracy, 1.0)
    if zero_solar_day:
        confidence_factor = 0.0

    # ---- Cost target: historical price model (if enabled + sufficient data) ----
    price_stats_data = settings.get("price_stats")   # injected by run_agent() each cycle
    max_floor        = settings.get("max_insurance_floor_pct", DEFAULT_MAX_INSURANCE_FLOOR)
    cost_target_method = "legacy"

    if HISTORICAL_PRICE_MODEL and price_stats_data and not is_peak:
        hist_target = _historical_grid_target(
            soc, remaining, confidence_factor, price, price_stats_data, max_floor)
        if hist_target is not None:
            cost_target = hist_target
            cost_target_method = "historical"
        else:
            # Insufficient price swing in history — fall back
            cost_target = 85 if solar_unreliable and now_h < 12 else (
                70 if solar_unreliable and now_h < 14 else (
                50 if solar_unreliable else grid_target))
    else:
        # Legacy: time-based substitute when solar unreliable; grid_target otherwise
        # (Also used for peak months — demand deadline logic overrides cost_target anyway)
        if solar_unreliable:
            cost_target = 85 if now_h < 12 else 70 if now_h < 14 else 50
        else:
            cost_target = grid_target

    # Expected solar contribution to the demand deadline (0 if solar can't be trusted)
    expected_solar = 0.0 if solar_unreliable else remaining

    # Deadline maths
    hours_to_cheap_end = _hours_to_cheap_end(price_forecast, price)
    # Roll over to next day when past the deadline (e.g. 23:00 → 15.9h until tomorrow's 2:55pm).
    # Without this, DEMAND_DEADLINE − now_h goes negative after 2:55pm and clamps to 0,
    # which triggers deadline-urgency escalation overnight.
    if now_h <= DEMAND_DEADLINE:
        hours_to_2_55 = DEMAND_DEADLINE - now_h
    else:
        hours_to_2_55 = (24.0 - now_h) + DEMAND_DEADLINE
    hours_to_deadline  = min(hours_to_2_55, hours_to_cheap_end) if is_peak else hours_to_cheap_end

    # Net solar available for battery = gross remaining minus home load consumed over the window.
    # Solar goes to loads first; only the surplus reaches the battery.
    # Peak: window is hours until 2:55pm. Non-peak: cap at 7h (full solar day).
    home_load_kw = state.get("home_load_kw", 0.5) or 0.5
    _solar_window_h = hours_to_2_55 if is_peak else min(hours_to_deadline, 7.0)
    net_expected_solar = max(expected_solar - home_load_kw * _solar_window_h, 0.0)

    # Peak-month demand fill maths (toward 85% by 2:55pm)
    # Uses net solar so we don't mistakenly hold when home load will consume most of the forecast.
    kwh_needed_85    = max((0.85 - soc / 100) * USABLE_KWH - net_expected_solar, 0.0)
    _slow_rate_85    = _avg_charge_rate_kw(soc, 85.0, "self_consumption")
    _fast_rate_85    = _avg_charge_rate_kw(soc, 85.0, "autonomous")
    fill_slow_85     = kwh_needed_85 / _slow_rate_85
    fill_fast_85     = kwh_needed_85 / _fast_rate_85

    # Cost-target fill maths
    kwh_needed   = max((cost_target / 100 - soc / 100) * USABLE_KWH, 0.0)
    _slow_rate   = _avg_charge_rate_kw(soc, float(cost_target), "self_consumption")
    _fast_rate   = _avg_charge_rate_kw(soc, float(cost_target), "autonomous")
    fill_slow    = kwh_needed / _slow_rate
    fill_fast    = kwh_needed / _fast_rate

    # Non-peak: will solar alone (net of home load) cover the gap to cost_target?
    # Only fires before 1pm when solar still has time to deliver, and when forecast is reliable.
    _kwh_gap = max((cost_target / 100 - soc / 100) * USABLE_KWH, 0.0)
    solar_can_cover = (
        not solar_unreliable
        and not is_peak
        and now_h < 13
        and _kwh_gap > 0
        and net_expected_solar >= _kwh_gap
    )

    # Spread: most expensive upcoming slot (next 6h) vs current price
    upcoming       = [f.get("cents_kwh", 0.0) for f in price_forecast[:12]]
    next_expensive = max(upcoming) if upcoming else price
    spread         = next_expensive - price
    # Cheapest price in the full forecast horizon — used to suppress deferral_limit when a
    # genuinely cheaper window is approaching (may be >6h away, e.g. overnight wait for Solar Sponge).
    all_prices  = [f.get("cents_kwh", 0.0) for f in price_forecast]
    forward_min = min(all_prices) if all_prices else price

    # Sliding forecast: cheap window forecast for 3+ consecutive cycles but never arrives.
    sliding_forecast = _detect_sliding_forecast(recent_records, price, forward_min)

    # ---- EV verdict ----
    ev              = state.get("ev", {})
    ev_plugged      = ev.get("plugged_in", False)
    ev_soc          = ev.get("ev_soc_pct") or 0
    ev_min          = ev.get("min_soc_pct") or 20
    ev_target       = ev.get("charge_target_pct") or 80

    ultra_cheap_c    = settings.get("ev_ultra_cheap_c", 5)
    standard_price_c = settings.get("ev_standard_price_c", 10)
    min_charge_price_c = settings.get("ev_min_charge_price_c", 20)

    if not ev_plugged:
        ev_rec = {"zappi_mode": "n/a", "rule_fired": "ev_disconnected"}
    elif in_demand:
        # Never pull from grid during demand window — solar-only via Eco+
        ev_rec = {"zappi_mode": "Eco+", "rule_fired": "ev_demand_window"}
    elif ev_soc < ev_min and price < min_charge_price_c:
        # Below minimum SoC — charge regardless of price (up to 20¢)
        ev_rec = {"zappi_mode": "Fast", "rule_fired": "ev_case3_below_minimum"}
    elif fit_price < 0 and soc >= 85 and ev_soc < 100:
        # FIT negative: absorb solar surplus into EV rather than paying to export
        ev_rec = {"zappi_mode": "Eco+", "rule_fired": "ev_case6_negative_fit_solar_dump"}
    elif soc >= 95 and solar_now > 0.5 and ev_soc < 100:
        # Battery full + real solar generating + EV has capacity.
        # Default: Eco (in-home surplus). Powerwall at 100% regulates grid export below
        # Zappi's 1.44 kW minimum; Eco tracks in-home surplus and charges when available.
        # Battery at ≥95% won't discharge for EV so battery-to-EV risk doesn't apply.
        # NOTE: do NOT use home_load_kw to estimate surplus here — home_load_30min_average
        # includes EV draw, creating a circular calculation.
        if is_peak and ev_soc < ev_target and hours_to_2_55 <= 0.75 and price < min_charge_price_c:
            # Demand window ≤45 min away, EV still below target, price acceptable.
            # Switch to Fast to maximise EV charge before the expensive window starts.
            ev_rec = {"zappi_mode": "Fast", "rule_fired": "ev_battery_full_fast_deadline"}
        else:
            ev_rec = {"zappi_mode": "Eco", "rule_fired": "ev_battery_full_solar_absorb"}
    elif ev_soc >= ev_target:
        ev_rec = {"zappi_mode": "Eco+", "rule_fired": "ev_target_met"}
    elif price <= ultra_cheap_c:
        # Price below ultra-cheap threshold — charge fast
        ev_rec = {"zappi_mode": "Fast", "rule_fired": "ev_ultra_cheap"}
    elif price <= standard_price_c:
        # Price below standard threshold — charge slowly (Eco: grid+solar, no battery discharge)
        ev_rec = {"zappi_mode": "Eco", "rule_fired": "ev_standard_price"}
    else:
        # Price too high — solar-only via Eco+
        ev_rec = {"zappi_mode": "Eco+", "rule_fired": "ev_price_too_high"}

    # ---- Battery verdict — ordered decision tree, first match wins ----
    def verdict(action, target, mode, rule):
        return {"action": action, "target_pct": target, "mode": mode, "rule_fired": rule}

    # Peak-eve (Rule 35): the 9pm–midnight window on a peak-month day. `in_demand` (3–9pm) is
    # handled first above; the 14:55–15:00 sliver is harmless. When PEAK_EVE_RUNUP is off this is
    # always False, so the gate below collapses to the old `now_h < DEMAND_DEADLINE` behaviour.
    peak_eve = is_peak and PEAK_EVE_RUNUP and now_h >= DEMAND_DEADLINE

    if in_demand:
        rec = verdict("hold", None, None, "demand_window_active")
    elif is_peak and soc < 85 and (now_h < DEMAND_DEADLINE or peak_eve):
        # Peak-month hard deadline escalation (Rule 13)
        if kwh_needed_85 <= 0:
            # net solar (after home load) covers the remaining gap — no grid charge needed yet.
            if soc >= 85:
                rec = verdict("hold", None, None, "peak_target_met")
            else:
                # Survival check: will the battery reach Solar Sponge (10am) without hitting the
                # 5% backup floor? If not, the grid covers home load at the expensive current price
                # anyway — charging now at self_consumption is strictly cheaper than that outcome.
                # Hours until the next Solar Sponge start (10am). Same-day before 10am; 0 while in
                # the 10am–3pm window (sponge is now); tomorrow's 10am in the peak-eve evening
                # (Rule 35) — the old `max(10 - now_h, 0)` read 0 after 3pm, which would wrongly
                # project no overnight drain. Only reachable when net solar already covers the gap,
                # so it never fires at night in practice, but kept correct for the day boundary.
                if now_h < 10.0:
                    hours_to_sponge = 10.0 - now_h
                elif now_h < DEMAND_DEADLINE:
                    hours_to_sponge = 0.0
                else:
                    hours_to_sponge = (24.0 - now_h) + 10.0
                projected_soc_at_sponge = soc - (home_load_kw * hours_to_sponge / USABLE_KWH * 100)
                if projected_soc_at_sponge <= 5.0:
                    # Battery hits floor before Solar Sponge. But if Solar Sponge is close
                    # and meaningfully cheaper, it's still better to drain to the floor and
                    # fast-charge there — the grid covers home load either way once at floor.
                    # Break-even: saving (price−forward_min) × kwh_needed must exceed the
                    # cost of home load during the drain period. A 5¢ gap with ≤3h wait
                    # is reliably worth it at this site's typical load (~0.5 kW).
                    worth_waiting = (
                        hours_to_sponge <= 3.0
                        and forward_min <= price - 5.0
                    )
                    if worth_waiting:
                        rec = verdict("hold", None, None, "peak_survival_wait_for_sponge")
                    else:
                        rec = verdict("charge", 85, "self_consumption", "peak_solar_cover_survival")
                else:
                    rec = verdict("hold", None, None, "peak_solar_will_cover")
        elif DEADLINE_GENTLE_LEAD and fill_fast_85 >= hours_to_2_55 - FAST_ESCALATE_BUFFER_H:
            # Deadline point-of-no-return: even a 5 kW autonomous charge is now within the safety
            # buffer of the deadline. Price arbitrage is irrelevant here — the demand charge
            # penalty (~$100/month) dwarfs any charging cost differential. Go autonomous.
            rec = verdict("charge", 100, "autonomous", "peak_deadline_autonomous")
        elif DEADLINE_GENTLE_LEAD and fill_slow_85 >= hours_to_2_55:
            # Gentle self_consumption alone can't fill the whole remaining gap in time, but a
            # 5 kW charge still has margin (fill_fast_85 < hours_to_2_55 - buffer). Rather than
            # slamming 5 kW now (Rule 33), lead with a gentle charge that makes progress and holds
            # the fast option in reserve; a later cycle escalates to peak_deadline_autonomous at
            # the point-of-no-return if gentle + solar fall behind. The FAST_ESCALATE_BUFFER_H
            # margin keeps the demand-window target guaranteed.
            rec = verdict("charge", 85, "self_consumption", "peak_deadline_gentle_lead")
        elif not DEADLINE_GENTLE_LEAD and (fill_fast_85 >= hours_to_2_55 or fill_slow_85 >= hours_to_2_55):
            # Kill-switch path: old straight-to-autonomous behaviour (escalate the instant
            # self_consumption can't fill the whole gap in time).
            rec = verdict("charge", 100, "autonomous", "peak_deadline_autonomous")
        elif now_h < DEMAND_DEADLINE and ((now_h >= 12.5 and soc < 40) or (now_h >= 13.5 and soc < 70)):
            # Afternoon-only backstop: absolute-hour thresholds assume we're in the real run-up to
            # 2:55pm. Guarded off in the peak-eve window (Rule 35) so it can't slam autonomous at
            # 11pm when hours_to_2_55 is ~16h — the go-hard-slot / gentle-lead branches below handle
            # the evening, deferring to the cheap morning slot instead.
            rec = verdict("charge", 100, "autonomous", "peak_deadline_quickcheck")
        elif in_sponge and now_h < 13 and soc < 50:
            rec = verdict("charge", max(50, cost_target), "self_consumption", "solar_sponge_floor")
        elif in_sponge and kwh_needed_85 > 0:
            # In Solar Sponge with grid charge still needed. Receding-horizon rate choice:
            # use autonomous (5kW) only when self_consumption won't fit the deadline;
            # otherwise self_consumption is fine — next cycle will recalculate with fresher
            # solar and price data and may downgrade further if solar improves.
            if fill_slow_85 >= hours_to_2_55 - 1.0:
                rec = verdict("charge", 85, "autonomous", "peak_sponge_go_hard")
            else:
                rec = verdict("charge", 85, "self_consumption", "peak_sponge_selfcons")
        elif kwh_needed_85 > 0:
            # Not in Solar Sponge yet. Check for a cheaper go-hard slot FIRST — waiting for
            # a cheaper fast-fill slot beats starting self_consumption now, as long as the
            # deadline is still achievable via autonomous at that cheaper price.
            best = _cheapest_go_hard_slot(
                price_forecast, price, soc, home_load_kw, hours_to_2_55
            )
            if best is not None:
                # Cheaper feasible slot exists — hold, charge fast there instead.
                rec = verdict("hold", None, None, "wait_for_cheap_go_hard")
            elif fill_slow_85 >= hours_to_2_55 - 1.0:
                # No cheaper slot AND self_consumption is getting tight — must start now.
                rec = verdict("charge", 85, "self_consumption", "peak_deadline_selfcons")
            elif fill_fast_85 < hours_to_2_55 - 2.0 and price > SOLAR_SPONGE_PRICE_THRESHOLD:
                # Autonomous mode can reach 85% before the deadline with 2h+ to spare,
                # so there's no need to start slow self_consumption at an above-threshold
                # price now. Hold at any SoC and any time of day: let the battery drain
                # toward the 5% floor if needed — grid covers home load there, then Solar
                # Sponge deadline logic (peak_sponge_go_hard / peak_deadline_autonomous)
                # catches up cheaply. The 2h margin means we only start charging early
                # when autonomous mode itself is running short of time.
                rec = verdict("hold", None, None, "peak_early_morning_hold")
            else:
                # No cheaper slot; current price is as good as it gets. Charge at self_consumption.
                rec = verdict("charge", 85, "self_consumption", "peak_charge_now")
        else:
            rec = verdict("hold", None, None, "peak_on_track")
    elif in_sponge and now_h < 13 and soc < 50:
        # Rule 14 — Solar Sponge minimum floor
        rec = verdict("charge", max(50, cost_target), "self_consumption", "solar_sponge_floor")
    elif cost_target <= soc:
        rec = verdict("hold", None, None, "target_met")
    elif solar_can_cover:
        # Solar forecast (net of home load) will cover the gap — hold, don't trickle from grid.
        # Escalation logic below will fire if solar underdelivers as the day progresses.
        rec = verdict("hold", None, None, "solar_will_cover")
    elif overnight_hold:
        # Solar Sponge tomorrow will be cheaper — don't charge overnight at high prices.
        # Rule 13 morning deadline maths will escalate if peak month demands it.
        rec = verdict("hold", None, None, "overnight_hold_wait_for_sponge")
    elif deferral_detected and (forward_min >= price - 2.0 or sliding_forecast):
        # Fire when: no meaningfully cheaper window is incoming (forward_min within 2¢),
        # OR the forecast has been sliding for 3+ cycles (cheap window keeps moving forward
        # but never arrives — forecast is unreliable, stop deferring and charge now).
        rule = "sliding_forecast" if sliding_forecast else "deferral_limit"
        rec = verdict("charge", cost_target, "self_consumption", rule)
    # When solar is unreliable, we can't count on it supplementing self_consumption —
    # apply a tighter buffer (1.5h instead of 0.5h) to escalate to autonomous sooner.
    elif fill_fast >= hours_to_deadline - 0.5:
        rec = verdict("charge", cost_target, "autonomous", "nonpeak_deadline_autonomous")
    elif solar_unreliable and fill_slow >= hours_to_deadline - 1.5:
        rec = verdict("charge", cost_target, "autonomous", "nonpeak_solar_unreliable_autonomous")
    elif fill_slow >= hours_to_deadline - 0.5:
        rec = verdict("charge", cost_target, "self_consumption", "nonpeak_deadline_selfcons")
    else:
        # Spread table — window still viable, decide on economics
        if spread < 5:
            rec = verdict("hold", None, None, "spread_too_small")
        elif spread > 15 and (cost_target - soc) > 15:
            rec = verdict("charge", cost_target, "autonomous", "spread_arbitrage")
        else:
            rec = verdict("charge", cost_target, "self_consumption", "spread_selfcons")

    # Rule 30 (revised 2026-07-25) — overnight survival-floor defense. If the verdict is to
    # HOLD while SoC sits at/below the floor, override to a gentle self_consumption top-up so
    # the battery never rides down into the emergency automation's 10% trigger (which slams
    # 5 kW and then fights the next HOLD). Only touches holds — a deadline autonomous charge is
    # never downgraded — and never during the demand window, where the battery must be free to
    # discharge and the automation is disabled anyway. Rule 31 keeps this top-up ~1.6 kW.
    if (SURVIVAL_FLOOR_DEFENSE and not in_demand
            and rec.get("action") == "hold"
            and soc <= OVERNIGHT_SURVIVAL_FLOOR_PCT):
        rec = verdict("charge", SURVIVAL_FLOOR_TARGET_PCT, "self_consumption",
                      "survival_floor_defend")

    return {
        "now_h":               round(now_h, 2),
        "soc":                 soc,
        "is_peak_month":       is_peak,
        "accuracy_class":      accuracy,
        "zero_solar_day":      zero_solar_day,
        "deferral_detected":   deferral_detected,
        "sliding_forecast":    sliding_forecast,
        "overnight_hold":      overnight_hold,
        "solar_unreliable":    solar_unreliable,
        "cost_target_pct":     cost_target,
        "cost_target_method":  cost_target_method,
        "expected_solar_kwh":      round(expected_solar, 2),
        # Both figures logged so the correction's effect on decisions is measurable
        # — `solar_remaining_used_kwh` is what the verdict was actually reasoned from.
        "solar_remaining_raw_kwh":       round(remaining_raw, 2),
        "solar_remaining_corrected_kwh": (round(_remaining_corr, 2)
                                          if _remaining_corr is not None else None),
        "solar_remaining_used_kwh":      round(remaining, 2),
        "net_expected_solar_kwh":  round(net_expected_solar, 2),
        "solar_can_cover":         solar_can_cover,
        "hours_to_cheap_end":  round(hours_to_cheap_end, 2),
        "hours_to_2_55pm":     round(hours_to_2_55, 2),
        "hours_to_deadline":   round(hours_to_deadline, 2),
        "kwh_needed_85":       round(kwh_needed_85, 2),
        "fill_slow_85_h":      round(fill_slow_85, 2),
        "fill_fast_85_h":      round(fill_fast_85, 2),
        "kwh_needed":          round(kwh_needed, 2),
        "fill_slow_h":         round(fill_slow, 2),
        "fill_fast_h":         round(fill_fast, 2),
        "spread_c":            round(spread, 1),
        "forward_min_c":       round(forward_min, 1),
        # Rule 32 audit: price_used_c is the 30-min-slot price every threshold was decided on;
        # price_spot_c is the raw 5-min sample. When they differ, the sensor read and the decision
        # diverge (same class of "displayed ≠ acted-on" as the slider drift).
        "price_used_c":        round(price, 1),
        "price_spot_c":        round(price_spot, 1),
        "fit_price_c":         round(fit_price, 1),
        "go_hard_slot":        (lambda b: {"price_c": round(b[0], 1), "hours_until": round(b[1], 1)}
                                if b else None)(
                                    _cheapest_go_hard_slot(price_forecast, price, soc, home_load_kw, hours_to_2_55)
                                    if is_peak and kwh_needed_85 > 0 else None
                                ),
        "recommended":         rec,
        "ev_recommended":      ev_rec,
    }


def _format_decision_context(ctx: dict) -> str:
    """Render the decision context as a REFERENCE block for the prompt (non-authoritative)."""
    r = ctx["recommended"]
    return (
        "## Deterministic decision helper — REFERENCE ONLY (you are still the decision-maker)\n"
        "A pure-Python helper precomputed the figures below from the same state and price\n"
        "forecast you can read via tools. Use it to sanity-check your reasoning. You may\n"
        "disagree — but if you do, state why in your log_decision summary.\n"
        f"  SoC (true/Tessie): {ctx['soc']}%   peak_month: {ctx['is_peak_month']}   now: {ctx['now_h']}h\n"
        f"  zero_solar_day: {ctx['zero_solar_day']}   deferral_detected: {ctx['deferral_detected']}   "
        f"sliding_forecast: {ctx['sliding_forecast']}   overnight_hold: {ctx['overnight_hold']}   "
        f"solar_unreliable: {ctx['solar_unreliable']} (accuracy: {ctx['accuracy_class']})\n"
        f"  cost_target: {ctx['cost_target_pct']}% ({ctx['cost_target_method']})   hours_to_cheap_end: {ctx['hours_to_cheap_end']}h   "
        f"hours_to_deadline: {ctx['hours_to_deadline']}h   spread: {ctx['spread_c']}¢\n"
        f"  to cost_target: need {ctx['kwh_needed']}kWh — self_consumption {ctx['fill_slow_h']}h / "
        f"autonomous {ctx['fill_fast_h']}h\n"
        f"  to 85% by 2:55pm: need {ctx['kwh_needed_85']}kWh — self_consumption {ctx['fill_slow_85_h']}h / "
        f"autonomous {ctx['fill_fast_85_h']}h\n"
        + (f"  go_hard_slot: cheapest feasible slot at {ctx['go_hard_slot']['price_c']}¢ in "
           f"{ctx['go_hard_slot']['hours_until']}h — wait then autonomous\n"
           if ctx.get('go_hard_slot') else "")
        + f"  >>> BATTERY: {r['action']} target={r['target_pct']}% mode={r['mode']} (rule: {r['rule_fired']})\n"
        f"  fit_price: {round(ctx.get('fit_price_c', 0) or 0, 1)}¢   "
        f"  >>> EV: zappi={ctx['ev_recommended']['zappi_mode']} (rule: {ctx['ev_recommended']['rule_fired']})"
    )


# ---------------------------------------------------------------------------
# Tool definitions (schema passed to Claude)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "get_current_state",
        "description": (
            "Read current energy system state: battery SoC, mode, reserve, grid price, "
            "solar generation, EV status, home load, and time context (peak month, "
            "demand window, solar sponge window). Call this first every cycle."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_price_forecast",
        "description": "Get Amber electricity price forecast for the next 12 hours (30-min intervals). Use this to decide whether to charge now or wait for a cheaper window.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_solar_forecast",
        "description": "Get Solcast solar generation forecast for the rest of today (30-min intervals, kW estimate). Use this to decide how much grid charge the battery needs before solar takes over.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_weather_forecast",
        "description": (
            "Get hourly cloud cover, solar radiation (W/m²), and rain probability from Open-Meteo "
            "for today's remaining solar hours and all of tomorrow. "
            "Use this to assess tomorrow's solar quality — especially at overnight cycles when deciding "
            "whether to pre-charge tonight. Also use to cross-check Solcast accuracy: if radiation_wm2 "
            "is high but Solcast shows unreliable, the problem may be temporary rather than all-day. "
            "Returns a tomorrow_solar_outlook summary (good/poor/overcast) for quick reasoning."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_powerwall_reserve",
        "description": (
            "Set Powerwall backup reserve % (floor SoC battery won't discharge below). "
            "Normal discharge: 5%. Charge toward full: 100%. "
            "Demand window protection: set to match the % needed to cover 3–9pm load."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "percent": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Reserve percentage (0–100)",
                }
            },
            "required": ["percent"],
        },
    },
    {
        "name": "set_powerwall_mode",
        "description": (
            "Set Powerwall operating mode. "
            "'self_consumption': charges from grid at a variable rate (typically 0.2–2.5 kW, read battery.charge_rate_kw) ONLY when backup_reserve_percent >"
            "current_soc. If reserve ≤ soc, battery charges from solar surplus only — no grid draw. "
            "Use for long cheap windows (3h+) or when spread doesn't justify urgency. "
            "'autonomous': fast ~5 kW grid charge. ALWAYS pair with set_powerwall_reserve(100) — "
            "this is the export guard. A HA safety net also reverts to self_consumption within 30s "
            "if export is detected, so autonomous is safe. "
            "Use autonomous when: (1) price spread > 8¢ AND need >15% AND window short (<2h); "
            "(2) peak month deadline pressure (fill_fast_85_h close to hours_to_deadline); "
            "(3) peak month + in Solar Sponge + grid charge still needed (go_hard_at_sponge strategy — "
            "fill fast at the cheapest window rather than trickling slowly through it). "
            "A 4¢ spread does NOT justify autonomous outside Solar Sponge."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["self_consumption", "autonomous"]}
            },
            "required": ["mode"],
        },
    },
    {
        "name": "set_zappi_mode",
        "description": (
            "Set Zappi EV charger mode. "
            "'Eco+': charge only from solar export past the meter — safe default, "
            "battery never powers EV. "
            "'Fast': charge from grid at full rate (~7 kW). "
            "'Off': stop charging."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["Eco+", "Eco", "Fast", "Off"]}
            },
            "required": ["mode"],
        },
    },
    {
        "name": "log_decision",
        "description": "Log your reasoning and any actions taken. Always call this last, even if you took no action.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "1–3 sentences explaining the situation and your reasoning.",
                },
                "actions_taken": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of actions taken, e.g. ['set_reserve(62%)', 'set_zappi(Eco+']. Empty list if no action.",
                },
                "ev_summary": {
                    "type": "string",
                    "description": "1–2 sentences covering EV status only: plug state, EV SoC, Zappi mode set and why. If EV is disconnected, say so briefly.",
                },
            },
            "required": ["summary", "actions_taken"],
        },
        "cache_control": {"type": "ephemeral"},  # cache all tool definitions up to here
    },
]

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
You are the narrative logger for a residential battery optimisation system in Glebe, Sydney.
The deterministic rule layer has already executed all control actions this cycle.
Your job is NARRATIVE ONLY — read the state, understand what was done and why, and log it clearly.
Do NOT call set_powerwall_reserve, set_powerwall_mode, or set_zappi_mode — those are no-ops.
The "Actions already executed" block in your context shows what was done. Explain it, don't redo it.

## Hardware
- Tesla Powerwall 2: 13.5 kWh usable, ~5 kW charge/discharge (autonomous) / ~1.7 kW (self_consumption)
- SolarEdge inverter: ~5 kW peak (6.12 kWp, flat roof)
- Polestar 4 EV (~100 kWh) via Zappi 2 charger
- Tariff: Amber Electric dynamic spot pricing, Ausgrid EA116

## System objectives (for narrative context — the rule layer enforces these)
1. **Demand window**: peak months (Nov–Mar, Jun–Aug) — zero grid import 3–9pm. Battery must reach 85% SoC by 2:55pm.
2. **EV never from battery**: Zappi default is Eco+ (solar export only).
3. **Minimise cost**: charge cheap (Solar Sponge 10am–3pm, spot dips), avoid expensive evening grid draw.
4. **Solar utilisation**: absorb solar surplus into battery; don't export during Solar Sponge.

## How to interpret state

**CRITICAL — which SoC reading to trust:**
Always use `soc_pct` for narrative. If `tessie_soc_failed: true`, the gateway value was substituted;
note this briefly but treat `soc_pct` as correct. Never cite `soc_gateway_pct` as evidence the charge
target was met — it floors at reserve level and will lie upward when reserve > true SoC.

**CRITICAL — how grid charging actually works:**
Grid charging ONLY occurs when `backup_reserve_percent > current_soc`.
- reserve=5%, soc=62%: no grid draw. Solar surplus charges passively.
- reserve=80%, soc=62%: grid draws at ~1.7 kW (self_consumption) or ~5 kW (autonomous).
"System is in self_consumption mode" alone does NOT mean grid is charging.

**Modes**:
- `self_consumption`: ~1.7 kW grid charge when reserve > soc. Normal operation.
- `autonomous`: ~5 kW grid charge when reserve > soc. Fast fill. Always paired with reserve=100%.

**CRITICAL — spread definition:**
`spread_c` (in the REFERENCE/computed_context block) = `current_import_price − forward_min_c`.
This is the "buy now vs buy later" saving: how much cheaper the best upcoming slot is vs charging now.
It has NOTHING to do with FIT or export price. Never define spread as import minus FIT.

## Solar forecast accuracy — for narrative
- `good`: forecast reliable — remaining_kwh is trustworthy.
- `poor`: forecast optimistic — actual ~50% of forecast.
- `unreliable`: ignore remaining_kwh — treat as zero-solar day.

## Weather forecast
Call `get_weather_forecast()` at overnight cycles (10pm–6am) to assess tomorrow's solar outlook
and explain any overnight pre-charge decision. `effective_radiation_wm2` is the key field
(rain-adjusted). > 300 W/m² = good, 150–300 = poor, < 150 = overcast/zero.

## EV — for ev_summary
Zappi modes: Eco+ (solar export only, safe default) · Eco (grid+solar, slow) · Fast (grid, ~7 kW) · Off.
The rule layer sets Zappi mode each cycle. In your ev_summary: plug status, EV SoC, what mode was set
and why (one sentence). If EV disconnected: "EV disconnected — no action."

EV cases (for context — the rule layer picks these):
- Demand window (3–9pm peak): always Eco+
- EV SoC < min AND import price cheap: Fast
- FIT < 0¢ AND battery ≥ 85% AND EV < 100%: Eco+ (absorbs negative-FIT surplus — this is the ONLY case where FIT is relevant to EV decisions)
- Battery ≥ 95% AND solar > 0.5 kW: Eco (in-home surplus)
- Import price ≤ ultra_cheap_c: Fast · Import price ≤ standard_price_c: Eco · Otherwise: Eco+
Note: FIT (feed-in tariff) is the export price you receive for solar sent to the grid. It has NO bearing on
Zappi mode except Case 6 above. Never cite FIT when explaining a standard Eco/Fast/Eco+ mode selection.

## Your task each cycle
1. Call `get_current_state()` — read the current state.
2. Call `get_price_forecast()` — understand the price context.
3. Call `log_decision()` with a clear narrative of what the rule layer did and why.
   - `summary`: 2–4 sentences. State SoC, price, mode, what action was taken (from the shadow block),
     and the reasoning behind it (the `rule_fired` name in plain English).
   - `ev_summary`: EV plug state, EV SoC, Zappi mode set and why (one sentence).
""".strip()

# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def _corrected_solar_breakdown(use_cache: bool = True):
    """Weight Solcast's hourly forecast by the measured per-hour site bias.

    Extracted from push_corrected_solar_forecast() on 2026-07-23 so the *control
    path* can use the same numbers the dashboard shows. Before that split the
    correction existed only on the dashboard and in the shadow LP, while
    compute_decision_context() — authoritative since Phase 5 — still read raw
    Solcast. On 2026-07-23 that had the rule layer holding `peak_solar_will_cover`
    for 17 consecutive overnight cycles against ~16.6 kWh of forecast when the
    calibrated expectation was 7.55 kWh, and the battery drained to 17%.

    Returns a dict of raw/corrected kWh for remaining-today, today-total and
    tomorrow, or None if Solcast's detailedHourly is unavailable. Never raises.

    Result is cached per cycle — get_current_state() is called more than once per
    cycle and this makes two HA attribute reads.
    """
    if use_cache and "solar_breakdown" in _cycle_context:
        return _cycle_context["solar_breakdown"]
    try:
        hourly = (ha_attrs(ENTITIES["solcast_today"]) or {}).get("detailedHourly") or []
        if not hourly:
            return None
        corr_map = (_model_params or {}).get("solar_correction") or {}
        min_n    = (_model_params or {}).get("min_samples", 5)

        now      = datetime.now(SYDNEY_TZ)
        hour_now = now.replace(minute=0, second=0, microsecond=0)

        def _apply(slots, since=None):
            """Sum (raw, corrected) kWh over slots, optionally from `since` onward."""
            raw = corrected = 0.0
            applied = 0
            for slot in slots or []:
                try:
                    start = datetime.fromisoformat(slot["period_start"])
                    pv    = float(slot["pv_estimate"])
                except (KeyError, TypeError, ValueError):
                    continue
                if since is not None and start < since:
                    continue
                raw += pv
                entry = corr_map.get(start.strftime("%H"))
                if entry and entry.get("n", 0) >= min_n:
                    corrected += pv * float(entry["ratio"])
                    applied   += 1
                else:
                    corrected += pv              # no data for this hour — don't guess
            return raw, corrected, applied

        raw, corrected, applied  = _apply(hourly, since=hour_now)
        today_raw, today_corr, _ = _apply(hourly)

        # Tomorrow matters most on the dashboard card: it drives the "overnight
        # top-up likely needed" call, and raw Solcast runs ~2x optimistic here.
        try:
            tmr_hourly = (ha_attrs(ENTITIES["solcast_tomorrow"]) or {}).get("detailedHourly") or []
        except Exception:
            tmr_hourly = []
        tmr_raw, tmr_corr, _ = _apply(tmr_hourly)

        result = {
            "remaining_raw_kwh":       round(raw, 2),
            "remaining_corrected_kwh": round(corrected, 2),
            "hours_corrected":         applied,
            "today_raw_kwh":           round(today_raw, 2),
            "today_corrected_kwh":     round(today_corr, 2),
            "tomorrow_raw_kwh":        round(tmr_raw, 2),
            "tomorrow_corrected_kwh":  round(tmr_corr, 2),
            "have_tomorrow":           bool(tmr_hourly),
            "effective_ratio":         round(corrected / raw, 3) if raw > 0 else None,
        }
        _cycle_context["solar_breakdown"] = result
        return result
    except Exception as exc:
        print(f"[solar] corrected breakdown unavailable: {exc}")
        return None


def push_corrected_solar_forecast() -> "float | None":   # quoted: Mac dev python is 3.9 (PEP 604 needs 3.10)
    """Push `sensor.solar_forecast_corrected` — Solcast, corrected for site bias.

    Solcast systematically over-forecasts this flat-roof site in winter, and the
    error is strongly hour-dependent: measured actual/forecast runs ~0.14 at
    08:00 and ~0.16 at 09:00, rising to ~0.74 by 13:00 (see
    model_params.json["solar_correction"], built by build_models.py).

    A single whole-day scalar would therefore be wrong in both directions — too
    harsh in the morning when the good midday hours are still ahead, too
    generous late in the day when only poor hours remain. So this weights each
    *remaining* hour by its own measured ratio, using Solcast's `detailedHourly`
    breakdown (verified to sum to the headline forecast).

    Ratios are read from model_params.json rather than hardcoded here, so
    rebuilding the model updates this automatically. Hours with fewer than
    min_samples observations fall back to 1.0 (uncorrected) rather than guessing.

    Returns the corrected kWh, or None if unavailable. Never raises.
    """
    try:
        b = _corrected_solar_breakdown()
        if b is None:
            return None
        raw, corrected, applied = b["remaining_raw_kwh"], b["remaining_corrected_kwh"], b["hours_corrected"]
        today_raw, today_corr   = b["today_raw_kwh"], b["today_corrected_kwh"]
        tmr_raw, tmr_corr       = b["tomorrow_raw_kwh"], b["tomorrow_corrected_kwh"]
        tmr_hourly              = b["have_tomorrow"]
        ratio                   = b["effective_ratio"]
        ha_set_state(
            "sensor.solar_forecast_corrected",
            f"{corrected:.2f}",
            {
                "unit_of_measurement": "kWh",
                "friendly_name": "Solar Forecast Remaining (bias-corrected)",
                "icon": "mdi:weather-sunny-alert",
                "state_class": "measurement",
                "solcast_raw_kwh": round(raw, 2),
                "effective_ratio": ratio,
                "hours_corrected": applied,
                "today_total_kwh": round(today_corr, 2),
                "today_total_raw_kwh": round(today_raw, 2),
                "tomorrow_kwh": round(tmr_corr, 2) if tmr_hourly else None,
                "tomorrow_raw_kwh": round(tmr_raw, 2) if tmr_hourly else None,
                "model_built_at": (_model_params or {}).get("built_at"),
            },
        )
        return round(corrected, 2)
    except Exception as exc:
        print(f"  Warning: corrected solar forecast push failed: {exc}", file=sys.stderr)
        return None


def _manual_override_active() -> tuple[bool, str]:
    """True when the user has taken manual control of the battery.

    Toggled via `input_boolean.agent_manual_override` (dashboard switch). While
    on, the rule layer still computes and logs its verdict — so shadow/divergence
    data keeps accumulating — but sends no commands, leaving whatever reserve and
    mode the user set in place.

    Auto-expires after MANUAL_OVERRIDE_MAX_HOURS so a forgotten toggle cannot
    silently disable the agent for days. On expiry the agent resumes control and
    says so loudly.

    NOT suppressed by this: the demand-window reserve guard (Rule 2 backstop),
    which runs earlier in run_agent(), and the HA safety automations, which are
    independent of the agent entirely. Manual override can cost money; it cannot
    cause a demand-charge breach.

    Fails open — if HA is unreachable the agent keeps control rather than
    silently going passive on a peak day.
    """
    try:
        obj = ha_get(ENTITIES["manual_override"])
    except Exception as exc:
        # A 404 just means the input_boolean isn't defined in this HA instance
        # yet — the normal state before deployment. Stay quiet for that; warn
        # for anything else (auth, timeout, HA down).
        if getattr(getattr(exc, "response", None), "status_code", None) == 404:
            return False, ""
        return False, f"override check failed ({exc}) — keeping control"

    if str(obj.get("state") or "").lower() != "on":
        return False, ""

    try:
        held_h = (datetime.now(SYDNEY_TZ)
                  - datetime.fromisoformat(obj["last_changed"])).total_seconds() / 3600.0
    except (KeyError, TypeError, ValueError):
        held_h = 0.0

    if held_h > MANUAL_OVERRIDE_MAX_HOURS:
        return False, (f"manual override ON for {held_h:.1f}h "
                       f"(limit {MANUAL_OVERRIDE_MAX_HOURS:.0f}h) — EXPIRED, resuming control")

    return True, f"manual override ON ({held_h:.1f}h of {MANUAL_OVERRIDE_MAX_HOURS:.0f}h)"


def _narrative_disabled() -> tuple[bool, str]:
    """True when the user has paused LLM narration to save API cost.

    Toggled via `input_boolean.agent_narrative_disable` (dashboard switch).
    While on, the agent skips the LLM narrative call entirely and logs the cycle
    with the deterministic auto-summary instead — decisions.jsonl, dashboard
    helpers, notifications, the liveness heartbeat and the shadow/optimizer
    divergence fields all still get written, just without the LLM prose (and
    without the API spend).

    Control is UNAFFECTED: the deterministic layer and the demand-window reserve
    guard both run earlier in run_agent(). This governs only the narrative step.

    Inverted sense (default off = narrate) on purpose: a fresh input_boolean with
    no initial: defaults off, and the unresolved overnight helper-reset gremlin
    (see todo) would reset it off — both of which land on the SAFE behaviour
    (keep narrating) rather than silently killing narration.

    Fails toward narrating — if HA is unreachable or the boolean isn't defined we
    keep the LLM path rather than silently suppressing it.
    """
    try:
        obj = ha_get(ENTITIES["narrative_disable"])
    except Exception as exc:
        # 404 = the input_boolean isn't defined in this HA instance yet (the
        # normal pre-deployment state). Stay quiet for that; warn for anything
        # else (auth, timeout, HA down) but keep narrating.
        if getattr(getattr(exc, "response", None), "status_code", None) == 404:
            return False, ""
        return False, f"narration-toggle check failed ({exc}) — narrating"
    if str(obj.get("state") or "").lower() == "on":
        return True, "narration paused (agent_narrative_disable ON) — LLM skipped to save API cost"
    return False, ""


def _execute_deterministic_verdict(ctx: dict, dry_run: bool = False) -> list[str]:
    """
    Execute battery and EV actions from the deterministic verdict.
    Returns a list of action strings matching the log_decision format (e.g. ['set_reserve(85%)']).
    Used when DETERMINISTIC_AUTHORITATIVE=True so the rule layer drives control, not the LLM.
    """
    rec    = ctx.get("recommended", {})
    ev_rec = ctx.get("ev_recommended", {})
    state  = _cycle_context.get("state", {})
    executed = []

    _override, _ov_msg = _manual_override_active()
    _cycle_context["manual_override"] = _override
    if _ov_msg:
        print(f"  [det] {_ov_msg}", file=sys.stderr)
    if _override:
        _note = (f"MANUAL OVERRIDE — no commands sent (verdict was "
                 f"{rec.get('action')}/{rec.get('rule_fired')}, "
                 f"target={rec.get('target_pct')}, mode={rec.get('mode')})")
        print(f"  [det] {_note}")
        return [_note]

    # Battery
    if rec.get("action") == "charge":
        target = rec.get("target_pct")
        mode   = rec.get("mode")
        if target is not None:
            # Rule 31 — gentle self_consumption charge. Under firmware 26.18.3 a fixed
            # reserve = target pulls the full 5 kW from any large reserve−SoC gap, so a
            # self_consumption charge chases reserve = SoC + offset instead (~1.6 kW). Fast
            # (autonomous) charges keep reserve = target = full rate. See _gentle_charge_reserve().
            _soc = (state.get("battery") or {}).get("soc_pct")
            if GENTLE_CHARGE_CONTROL and mode == "self_consumption":
                reserve_cmd = _gentle_charge_reserve(_soc, target)
                intent      = "gentle"
            else:
                reserve_cmd = target
                intent      = "full"
            _cycle_context["rate_control"] = {
                "charge_target_pct": target,
                "reserve_cmd_pct":   reserve_cmd,
                "charge_offset_pts": (SELF_CONS_CHARGE_OFFSET_PTS if intent == "gentle" else None),
                "charge_rate_intent": intent,
            }
            print(f"  [det] set_powerwall_reserve({reserve_cmd}%) [{intent}, target {target}%, "
                  f"soc {_soc}%] — rule: {rec.get('rule_fired')}")
            if not dry_run:
                set_powerwall_reserve(reserve_cmd)
            executed.append(f"set_reserve({reserve_cmd}%)")
        if mode is not None:
            current_mode = (state.get("battery") or {}).get("mode")
            if mode != current_mode:
                print(f"  [det] set_powerwall_mode({mode}) — rule: {rec.get('rule_fired')}")
                if not dry_run:
                    set_powerwall_mode(mode)
                executed.append(f"set_mode({mode})")
    else:
        # Hold: the battery must not be grid-charging. It can be charging two ways, and a hold
        # must undo BOTH:
        #   (1) mode == autonomous — under firmware 26.18.3 autonomous grid-charges at ~5 kW
        #       regardless of reserve, so dropping reserve alone is powerless. A hold MUST revert
        #       the mode. (2026-07-26 incident: a `hold`/peak_solar_will_cover verdict left
        #       mode=autonomous from an earlier deadline charge; the reserve-only cleanup could
        #       not stop the 5 kW charge and it kept running until reverted by hand.)
        #   (2) reserve > SoC in self_consumption — the gentle ~1.7 kW top-up toward reserve.
        _batt        = state.get("battery") or {}
        _reserve_now = _batt.get("reserve_pct")
        _soc_now     = _batt.get("soc_pct")
        _mode_now    = _batt.get("mode")

        _reverted_autonomous = False
        if _mode_now is not None and _mode_now != "self_consumption":
            print(f"  [det] set_powerwall_mode(self_consumption) — hold verdict, reverting {_mode_now} (soc={_soc_now}%)")
            if not dry_run:
                set_powerwall_mode("self_consumption")
            executed.append(f"set_mode(self_consumption) — hold verdict, reverting {_mode_now}")
            _reverted_autonomous = True

        # Drop reserve to the 5% floor. Send unconditionally when we just reverted autonomous:
        # after an autonomous charge the real reserve is high (~100) and
        # sensor.powerwall_backup_reserve can lag by 50+ points (it read 5% on 2026-07-26 while
        # the true setpoint was ~57%), so trusting that sensor is exactly what defeated the drop.
        # Otherwise send only when the best-effort read is above the floor, to avoid a redundant
        # Tessie write on every routine hold cycle.
        if _reverted_autonomous or _reserve_now is None or _reserve_now > 5:
            print(f"  [det] set_powerwall_reserve(5%) — hold verdict, clearing reserve (read={_reserve_now}%, soc={_soc_now}%)")
            if not dry_run:
                set_powerwall_reserve(5)
            executed.append(f"set_reserve(5%) — hold verdict, clearing reserve {_reserve_now}%")

    # EV
    zappi_mode = ev_rec.get("zappi_mode")
    if zappi_mode and zappi_mode != "n/a":
        current_zappi = (state.get("ev") or {}).get("zappi_mode")
        if zappi_mode != current_zappi:
            print(f"  [det] set_zappi_mode({zappi_mode}) — rule: {ev_rec.get('rule_fired')}")
            if not dry_run:
                set_zappi_mode(zappi_mode)
            executed.append(f"set_zappi({zappi_mode})")

    return executed


def _guarded_set_reserve(percent: int) -> str:
    """Block any reserve increase during the demand window — the battery must be free to discharge."""
    now = datetime.now(SYDNEY_TZ)
    in_demand = now.month in PEAK_MONTHS and 15 <= now.hour < 21
    if in_demand and percent > 10:
        msg = (f"set_powerwall_reserve({percent}%) BLOCKED — demand window active. "
               f"Reserve must stay at 5% so battery can discharge. No API call made.")
        print(f"  ⚠️  {msg}", file=sys.stderr)
        return msg
    return set_powerwall_reserve(percent)


TOOL_MAP = {
    "get_current_state":    lambda _: get_current_state(),
    "get_price_forecast":   lambda _: get_price_forecast(),
    "get_solar_forecast":   lambda _: get_solar_forecast(),
    "get_weather_forecast": lambda _: get_weather_forecast(),
    "set_powerwall_reserve":lambda a: _guarded_set_reserve(a["percent"]),
    "set_powerwall_mode":   lambda a: set_powerwall_mode(a["mode"]),
    "set_zappi_mode":       lambda a: set_zappi_mode(a["mode"]),
    "log_decision":         lambda a: log_decision(a["summary"], a["actions_taken"], a.get("ev_summary", "")),
}

# ---------------------------------------------------------------------------
# Phase 7 — selective narrative: skip LLM on routine cycles
# ---------------------------------------------------------------------------

_ROUTINE_HOLD_RULES = {
    "overnight_hold_wait_for_sponge",
    "peak_early_morning_hold",
    "peak_target_met",
    "peak_on_track",
    "peak_solar_will_cover",
    "demand_window_active",
    "target_met",
    "nonpeak_on_track",
}


def _is_interesting_cycle(ctx: dict, actions: list[str], records: list[dict],
                           demand_guard_fired: bool) -> bool:
    """Return True when this cycle needs LLM narrative (rule fired, action taken, or context shifted)."""
    if demand_guard_fired:
        return True
    if actions:
        return True  # any battery/EV action taken — always narrate
    rule = (ctx.get("recommended") or {}).get("rule_fired", "")
    if rule not in _ROUTINE_HOLD_RULES:
        return True  # unusual hold rule — worth explaining
    if records:
        prev_rule = (records[-1].get("computed_verdict") or {}).get("rule_fired")
        if prev_rule != rule:
            return True  # rule changed since last cycle
    return False


def _build_auto_summary(ctx: dict) -> tuple[str, str]:
    """One-line summaries for routine cycles where LLM is skipped.

    Returns (battery_summary, ev_summary). ev_summary is empty string when EV
    not plugged in so log_decision() can suppress the EV notification entirely.
    """
    rec    = ctx.get("recommended") or {}
    rule   = rec.get("rule_fired", "unknown")
    action = rec.get("action", "hold")   # was hardcoded "hold" — wrong when narration
                                         # is paused on a cycle that actually charged
    soc   = ctx.get("soc", "?")
    state = _cycle_context.get("state", {})
    price = (state.get("grid") or {}).get("price_cents_kwh", "?")
    solar = (state.get("solar") or {}).get("current_kw", "?")
    ev    = state.get("ev") or {}
    battery_summary = f"[auto] {rule} | battery {soc}% | {price}¢/kWh | solar {solar}kW | {action}"
    ev_soc = ev.get("ev_soc_pct", "?")
    plug   = "plugged in" if ev.get("plugged_in") else "not plugged in"
    mode   = ev.get("zappi_mode", "?")
    ev_summary = f"[auto] EV {ev_soc}% ({plug}) | mode {mode} | {action}"
    return battery_summary, ev_summary


def run_agent(dry_run: bool = False):
    """
    Run one optimisation cycle.
    dry_run=True: reads state and prints the agent's reasoning but does NOT
    call any set_* or log_decision tools — safe for testing.
    """
    _cycle_context.clear()
    client        = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    recent        = get_recent_decisions(3)

    # --- Pre-flight safety checks (run before LLM, independent of shadow layer) ------

    # 1. Demand-window reserve guard (Rule 2 backstop).
    # If we're inside the 3–9pm demand window on a peak month and the reserve is
    # stranded above 10%, the Powerwall can't discharge — grid covers home load and
    # sets a demand ratchet charge. Drop it to 5% via Tessie directly, bypassing HA
    # rest_commands entirely (those may be broken on an HA restart, as happened June 2).
    _demand_reserve_guard_fired = False
    try:
        _now_pre = datetime.now(SYDNEY_TZ)
        _is_peak_pre = _now_pre.month in PEAK_MONTHS
        _in_demand_pre = _is_peak_pre and 15 <= _now_pre.hour < 21
        if _in_demand_pre and not dry_run:
            _reserve_pre = _int(ha_state(ENTITIES["battery_reserve"]))
            _soc_pre     = _int(ha_state(ENTITIES["battery_soc"]))
            if _reserve_pre is not None and _reserve_pre > 10:
                print(f"  ⚠️  DEMAND WINDOW RESERVE GUARD: reserve={_reserve_pre}% during demand"
                      f" window (soc={_soc_pre}%) — dropping to 5% via Tessie directly",
                      file=sys.stderr)
                set_powerwall_reserve(5)
                _demand_reserve_guard_fired = True
    except Exception as _exc:
        print(f"  Warning: demand-window reserve guard failed: {_exc}", file=sys.stderr)

    # 1b. Bias-corrected solar forecast → sensor.solar_forecast_corrected.
    # Dashboard/diagnostic only; nothing in the control path reads it.
    if not dry_run:
        _corr_kwh = push_corrected_solar_forecast()
        if _corr_kwh is not None:
            print(f"  Corrected solar remaining: {_corr_kwh} kWh")

    # 2. HA rest_command health check — warn if the safety automations are broken.
    try:
        _svc_r = requests.get(f"{HA_URL}/api/services", headers=HA_HEADERS, timeout=10)
        if _svc_r.status_code == 200:
            _domains = {d["domain"] for d in _svc_r.json()}
            if "rest_command" not in _domains:
                print("  ⚠️  HA rest_command NOT LOADED — safety automations (2:55pm reset,"
                      " startup floor) are broken. Restart HA to fix.", file=sys.stderr)
    except Exception as _exc:
        print(f"  Warning: HA health check failed: {_exc}", file=sys.stderr)

    # ------------------------------------------------------------------------------------

    # Deterministic decision layer — always computed.
    # When DETERMINISTIC_AUTHORITATIVE=True: executes actions directly, LLM gets narrative-only prompt.
    # When False: injects verdict as REFERENCE ONLY, LLM remains in control (legacy shadow mode).
    shadow_block = ""
    _det_executed_actions: list[str] = []
    try:
        _state    = get_current_state()
        _forecast = get_price_forecast()
        _records  = get_recent_records(3)
        _prices   = load_price_history(PRICE_HISTORY_DAYS)
        _stats    = _price_stats(_prices)
        _state.setdefault("settings", {})["price_stats"] = _stats
        _ctx      = compute_decision_context(_state, _forecast, _records,
                                             datetime.now(SYDNEY_TZ))
        _cycle_context["decision_context"] = _ctx

        if DETERMINISTIC_AUTHORITATIVE:
            # Execute the verdict now, before the LLM runs.
            _det_executed_actions = _execute_deterministic_verdict(_ctx, dry_run=dry_run)
            _rec    = _ctx.get("recommended", {})
            _ev_rec = _ctx.get("ev_recommended", {})
            _cmds   = _det_executed_actions or ["hold — no change needed"]
            shadow_block = (
                "\n\n## Actions already executed by deterministic rule layer\n"
                f"  rule_fired: {_rec.get('rule_fired')}  action: {_rec.get('action')}  "
                f"target_pct: {_rec.get('target_pct')}  mode: {_rec.get('mode')}\n"
                f"  EV: zappi={_ev_rec.get('zappi_mode')} (rule: {_ev_rec.get('rule_fired')})\n"
                f"  Commands sent: {_cmds}\n\n"
                + _format_decision_context(_ctx)
            )
        else:
            shadow_block = "\n\n" + _format_decision_context(_ctx)
    except Exception as exc:
        print(f"  Warning: deterministic decision context failed: {exc}", file=sys.stderr)

    # LP optimiser shadow verdict — separate try so it can never affect the
    # deterministic shadow or the control path. Shadow only, not authoritative.
    if _HAVE_OPTIMIZER:
        try:
            _opt_state  = dict(_cycle_context.get("state") or {})
            _opt_prices = _cycle_context.get("price_forecast")
            if _opt_state and _opt_prices:
                # Pass solar_unreliable flag so the LP zeroes out solar on cloudy mornings
                # (previously the LP would see a positive Solcast forecast and hold when the
                # deterministic layer correctly charged — all divergences were this bug).
                _det_ctx = _cycle_context.get("decision_context") or {}
                _opt_state["solar_unreliable"] = _det_ctx.get("solar_unreliable", False)
                # Flatten SoC to the top level — optimize_battery() expects a flat
                # state dict and has no knowledge of our nested "battery" sub-dict.
                # Without this the LP ran on a hardcoded 50% default for every cycle
                # from its 2026-06-01 wire-in to 2026-07-22 (fixed 2026-07-22).
                _opt_state["soc_pct"] = (_opt_state.get("battery") or {}).get("soc_pct")
                # Extend the ~6h Amber forecast with synthetic historical prices so the LP
                # can see the 15:00–21:00 demand-window block and apply its demand_penalty.
                _hourly_model = _build_hourly_price_model()
                _opt_prices_ext = _extend_forecast_to_demand_window(
                    _opt_prices, datetime.now(SYDNEY_TZ), _hourly_model)
                _opt = optimize_battery(_opt_state, _opt_prices_ext,
                                        get_solar_forecast(), datetime.now(SYDNEY_TZ),
                                        model_params=_model_params)
                _cycle_context["optimizer_verdict"]  = _opt["verdict"]
                _cycle_context["optimizer_context"]  = {
                    k: v for k, v in _opt.items() if k != "verdict"}
        except Exception as exc:
            print(f"  Warning: optimizer shadow failed: {exc}", file=sys.stderr)

    # LLM narration gate. Two independent reasons to skip the paid LLM call, both
    # of which still log the cycle via the deterministic auto-summary (so
    # decisions.jsonl / dashboard helpers / notifications / heartbeat and the
    # shadow+optimizer divergence fields keep getting written — Phase-4 data
    # collection is uninterrupted):
    #   (1) Phase 7 — routine (uninteresting) hold cycle. Always active.
    #   (2) User paused narration via input_boolean.agent_narrative_disable to
    #       save API cost. Forces the skip even on an "interesting" cycle.
    # Only when DETERMINISTIC_AUTHORITATIVE=True (control path is deterministic).
    if DETERMINISTIC_AUTHORITATIVE and not dry_run:
        try:
            _narr_off, _narr_msg = _narrative_disabled()
            # Let log_decision() see the toggle without a second HA round-trip, so it
            # can also MUTE the per-cycle battery/EV notifications when paused (the user
            # wants quiet, not just cheap). False on the LLM path → notifications fire.
            _cycle_context["narrative_disabled"] = _narr_off
            _interesting = _is_interesting_cycle(
                _ctx, _det_executed_actions, _records, _demand_reserve_guard_fired)
            if _narr_off or not _interesting:
                _auto_bat, _auto_ev = _build_auto_summary(_ctx)
                # Routine holds record "hold"; a narration-paused cycle that the rule
                # layer actually acted on records what it executed, not a false "hold".
                _skip_actions = (_det_executed_actions
                                 if (_narr_off and _interesting and _det_executed_actions)
                                 else ["hold — no change needed"])
                log_decision(_auto_bat, _skip_actions, _auto_ev)
                _why = _narr_msg if _narr_off else (
                    f"routine, rule: {(_ctx.get('recommended') or {}).get('rule_fired', '?')}")
                print(f"Cycle complete (LLM skipped — {_why}).")
                return
        except Exception as _exc:
            print(f"  Warning: narration-gate check failed ({_exc}) — running LLM", file=sys.stderr)

    if DETERMINISTIC_AUTHORITATIVE:
        initial_msg = (
            "The deterministic rule layer has already executed this cycle's control actions "
            "(see below). Your job this cycle is NARRATIVE ONLY:\n"
            "1. Call get_current_state() and get_price_forecast() to read current state.\n"
            "2. Call log_decision() with a clear human-readable summary of what was done and why.\n"
            "   - Do NOT call set_powerwall_reserve, set_powerwall_mode, or set_zappi_mode — "
            "those calls will be ignored.\n"
            "   - The actions_taken list in log_decision should reflect what the rule layer executed "
            "(shown below under 'Commands sent').\n\n"
            "## Recent decisions (last 3 cycles)\n"
            f"{recent}\n"
            f"{shadow_block}"
        )
    else:
        initial_msg = (
            "Run your energy optimisation cycle now.\n\n"
            "## Recent decisions (last 3 cycles)\n"
            f"{recent}\n\n"
            "Check this before deciding to hold. If you see 2+ consecutive holds waiting for "
            "a cheaper window that hasn't arrived, apply the deferral limit rule and charge now."
            f"{shadow_block}"
        )
    messages = [{"role": "user", "content": initial_msg}]

    print(f"\n{'='*60}")
    print(f"Energy agent — {datetime.now(SYDNEY_TZ).strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"{'='*60}")
    if dry_run:
        print("DRY RUN — no writes will be made\n")

    # System prompt as a cacheable content block — static content cached across turns
    system_prompt_block = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]

    # Fault-isolate the LLM narrative loop. The deterministic control path already ran
    # (above), so an LLM failure here (expired ANTHROPIC_API_KEY, Anthropic outage, network)
    # threatens only observability — but critically, log_decision() is called BY the LLM as a
    # tool, so a crash here means decisions.jsonl / dashboard helpers / notifications never get
    # written and the agent LOOKS frozen though control is fine (the 2026-07-26 incident).
    # On failure we degrade to the same deterministic auto-summary the Phase-7 routine path uses.
    _llm_logged = False   # did the LLM successfully call log_decision this cycle?
    try:
        while True:
            response = client.messages.create(
                model=MODEL,
                max_tokens=2048,
                system=system_prompt_block,
                tools=TOOLS,
                messages=messages,
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
            )

            messages.append({"role": "assistant", "content": response.content})

            # Accumulate token usage — track cache hits separately for accurate cost calculation
            _cycle_context["input_tokens"]        = _cycle_context.get("input_tokens", 0)        + response.usage.input_tokens
            _cycle_context["output_tokens"]       = _cycle_context.get("output_tokens", 0)       + response.usage.output_tokens
            _cycle_context["cache_read_tokens"]   = _cycle_context.get("cache_read_tokens", 0)   + getattr(response.usage, "cache_read_input_tokens", 0)
            _cycle_context["cache_write_tokens"]  = _cycle_context.get("cache_write_tokens", 0)  + getattr(response.usage, "cache_creation_input_tokens", 0)

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue

                    name  = block.name
                    args  = block.input or {}
                    is_write = name.startswith("set_") or name == "log_decision"

                    print(f"→ {name}({_args_summary(args)})")

                    det_noop = DETERMINISTIC_AUTHORITATIVE and name.startswith("set_") and name != "log_decision"
                    if dry_run and is_write:
                        result = f"[dry-run] {name} skipped"
                    elif det_noop:
                        result = f"[deterministic-authoritative] {name} ignored — rule layer already acted"
                        print(f"  (no-op: deterministic mode)")
                    else:
                        try:
                            result = TOOL_MAP[name](args)
                        except Exception as exc:
                            result = f"ERROR: {exc}"
                            print(f"  !! {result}", file=sys.stderr)

                    # Record a successful narrative log so the except-handler below won't
                    # double-write the JSONL row / re-notify if the LLM fails on a LATER turn.
                    if name == "log_decision" and not (isinstance(result, str) and result.startswith("ERROR")):
                        _llm_logged = True

                    print(f"  ← {json.dumps(result)[:120]}")
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     json.dumps(result),
                    })

                messages.append({"role": "user", "content": tool_results})
            else:
                break   # unexpected stop reason

        print("\nCycle complete.")
    except Exception as exc:
        # Control already executed; only the narrative failed. Write the deterministic
        # fallback so the cycle still records itself — unless the LLM already logged before
        # failing (a later-turn error), which would otherwise double-write and re-notify.
        print(f"  !! LLM narrative call failed ({type(exc).__name__}: {exc}) — "
              f"writing deterministic fallback summary", file=sys.stderr)
        if not _llm_logged:
            _cycle_context["llm_narrative_failed"] = True
            try:
                # decision_context mirrors _ctx but via _cycle_context so this never NameErrors
                # even if the shadow-context block above failed before _ctx was assigned.
                _fb_ctx = _cycle_context.get("decision_context") or {}
                _fb_bat, _fb_ev = _build_auto_summary(_fb_ctx)
                _fb_actions = _det_executed_actions or ["hold — no change needed"]
                log_decision(_fb_bat, _fb_actions, _fb_ev)
            except Exception as exc2:
                print(f"  !! deterministic fallback summary also failed: {exc2}", file=sys.stderr)
        print("\nCycle complete (LLM degraded — deterministic fallback logged).")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _float(val, default=0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default

def _int(val, default=0) -> int:
    return int(_float(val, default))

def _hour_solar_ratio(hour_str: "str | None" = None) -> "float | None":
    """Measured Solcast bias ratio (actual/forecast) for a local hour ("%H").

    Reads model_params.json["solar_correction"] — the same per-hour ratios the
    dashboard sensor and the LP use. Returns None when the hour has fewer than
    min_samples observations behind it, so callers fall back to raw Solcast
    rather than guess. Never raises.
    """
    corr_map = (_model_params or {}).get("solar_correction") or {}
    min_n    = (_model_params or {}).get("min_samples", _MODEL_MIN_SAMPLES)
    if hour_str is None:
        hour_str = datetime.now(SYDNEY_TZ).strftime("%H")
    entry = corr_map.get(hour_str)
    if entry and entry.get("n", 0) >= min_n:
        try:
            return float(entry["ratio"])
        except (TypeError, ValueError):
            return None
    return None


def _solar_accuracy(actual_kw: float, forecast_kw: float,
                    corrected_forecast_kw: "float | None" = None) -> str:
    """Return a plain-English accuracy label for the agent to reason about.

    When a bias-corrected forecast is supplied, accuracy is measured against it
    rather than raw Solcast. This matters because Solcast over-forecasts this
    flat roof by ~7x at 08:00 and ~6x at 09:00 in winter (model_params
    solar_correction), so raw actual/forecast is ~0.14 on a *normal* winter
    morning and the label reads "unreliable". That flag zeroes `expected_solar`
    in compute_decision_context() (`expected_solar = 0.0 if solar_unreliable`),
    which made the rule layer plan the whole day as zero-solar and grid-charge
    needlessly hard (2026-07-24 08:00 case). The corrected forecast already bakes
    in that hour's expected bias, so comparing against it asks the real question:
    is solar underperforming its *calibrated* expectation, or just its raw one?

    Falls back to raw Solcast when no corrected figure is available (Solcast
    attribute outage, or an hour with too few observations) — degrades to the
    previous behaviour rather than to a wrong answer.
    """
    if forecast_kw < 0.2:
        # Raw Solcast itself expects nothing — night or pre-dawn. Keep gating on
        # raw here so "night is night" regardless of the correction.
        return "not_applicable (night or near-zero forecast)"
    if corrected_forecast_kw is not None:
        if corrected_forecast_kw < 0.1:
            # Calibrated expectation is ~0 (deep-morning bias), so there is
            # nothing to be "unreliable" about — don't let a near-zero reference
            # blow the ratio up and condemn the whole day's remaining solar.
            return (f"not_applicable (calibrated expectation "
                    f"{corrected_forecast_kw:.2f}kW ~0)")
        ref, basis = corrected_forecast_kw, f"corrected {corrected_forecast_kw:.2f}kW"
    else:
        ref, basis = forecast_kw, f"forecast {forecast_kw:.1f}kW"
    ratio = actual_kw / ref
    if ratio < 0.3:
        return f"unreliable — actual {actual_kw:.1f}kW vs {basis} ({ratio:.0%})"
    if ratio < 0.7:
        return f"poor — actual {actual_kw:.1f}kW vs {basis} ({ratio:.0%})"
    return f"good — actual {actual_kw:.1f}kW vs {basis} ({ratio:.0%})"

def _safe_int(entity_id: str, default=None):
    """Read an entity state as int, returning default if entity missing or unavailable."""
    try:
        return _int(ha_state(entity_id))
    except Exception:
        return default

def _safe_float(entity_id: str, default: float = 0.0) -> float:
    """Read an entity state as float, returning default if entity missing or unavailable."""
    try:
        return float(ha_state(entity_id) or default)
    except Exception:
        return default


def _last_known_good(key: str, history: list):
    """Most recent value HA itself genuinely reported in-band for `key`, or None.

    Reads `settings_used` from past decisions.jsonl records, so the substitute
    for a bad value is a value the console actually held — never a target
    hardcoded here. Returns None when there is no usable history (e.g. before
    this logging existed, or after a long outage).

    **Records where this key was itself substituted are skipped.** `settings_used`
    logs the value *used*, which may be a substitute; without this check the
    fallback would read its own earlier output back as evidence and launder a
    substitute into a permanent "known good" value. Observed 2026-07-23: a
    hardcoded 70 written by an earlier build was picked up from the log an hour
    after the hardcoding was removed, and reported as though it came from HA.
    """
    _alias, lo, hi = SETTINGS_SPEC[key]
    for rec in reversed(history or []):
        try:
            if any(v.get("setting") == key
                   for v in (rec.get("settings_violations") or [])):
                continue          # this cycle's value was substituted, not observed
            value = (rec.get("settings_used") or {}).get(key)
            if value is not None and lo <= float(value) <= hi:
                return float(value)
        except (AttributeError, TypeError, ValueError):
            continue
    return None


def _validated_setting(key: str, history=None):
    """Read one SETTINGS_SPEC helper from HA and range-check it.

    Returns (value_or_None, violation_or_None). See SETTINGS_SPEC for the full
    semantics. In short: in-band values pass through untouched; out-of-band
    values are replaced by the last in-band value HA reported, else by the bad
    value clamped to the nearest band edge. A `None` value means the entity was
    unreadable and no history exists — the caller should apply its own default,
    which is the correct handling for a genuinely absent value.
    """
    _alias, lo, hi = SETTINGS_SPEC[key]
    entity = ENTITIES[_alias]
    # ha_state() raises on a 404 rather than returning None, so a helper that has
    # been deleted from configuration.yaml (or not yet created) would otherwise
    # take down the whole cycle from inside get_current_state(). A missing entity
    # is the "unreadable" case this function already handles.
    try:
        raw = ha_state(entity)
    except Exception:
        raw = None

    def _substitute(found, reason):
        lkg = _last_known_good(key, history)
        if lkg is not None:
            used, source = lkg, "last_known_good"
        elif isinstance(found, float):
            used, source = min(max(found, lo), hi), "clamped_to_band"
        else:
            used, source = None, "unavailable_no_history"
        return used, {"setting": key, "entity": entity, "found": found,
                      "used": used, "band": [lo, hi],
                      "reason": reason, "source": source}

    if raw in (None, "", "unknown", "unavailable"):
        # Transport failure, not a bad value. Substitute quietly if we can, and
        # only raise a violation when we cannot supply anything at all.
        lkg = _last_known_good(key, history)
        if lkg is not None:
            return lkg, None
        return None, {"setting": key, "entity": entity, "found": raw, "used": None,
                      "band": [lo, hi], "reason": "unreadable",
                      "source": "unavailable_no_history"}
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _substitute(raw, "unparseable")
    if lo <= value <= hi:
        return value, None
    return _substitute(value, "out_of_band")


def _read_validated_settings(history=None) -> tuple[dict, list]:
    """Read every SETTINGS_SPEC helper, returning (values, violations).

    Keys whose value could not be established at all are omitted, so the
    caller's own `.get(key, default)` applies — correct for a genuinely absent
    value, and never the failure mode this guards against (a key that exists
    holding a wrong value, where `.get`'s default can never fire).
    """
    values, violations = {}, []
    for key in SETTINGS_SPEC:
        value, violation = _validated_setting(key, history)
        if value is not None:
            values[key] = value
        if violation:
            violations.append(violation)
    return values, violations


def _notify_setting_violations(violations: list) -> None:
    """Log and push an HA notification when a control input is out of band.

    Deliberately loud: these values drive compute_decision_context(), so a bad
    one is a control fault, not a cosmetic UI issue. Never raises — a failed
    notification must not take the agent down.
    """
    if not violations:
        return
    lines = [
        f"- {v['setting']}: found {v['found']}, using {v['used']} "
        f"(sane band {v['band'][0]}–{v['band'][1]}, {v['reason']})"
        for v in violations
    ]
    body = "\n".join(lines)
    print(f"[settings] {len(violations)} control input(s) out of band:\n{body}")
    try:
        ha_service("persistent_notification", "create", {
            "title": f"⚠️ Agent: {len(violations)} control input(s) out of band",
            "message": (
                "These HA helpers drive the agent's decisions. The agent has "
                "substituted its intended value **for this cycle only** — the "
                "helper still holds the bad value, so please fix it in the UI.\n\n"
                + body
            ),
            "notification_id": "agent_settings_out_of_band",
        })
    except Exception as exc:
        print(f"[settings] could not push notification: {exc}")

def _args_summary(args: dict) -> str:
    if not args:
        return ""
    return ", ".join(f"{k}={v}" for k, v in args.items())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _send_heartbeat(suffix: str = "", body: str = "") -> None:
    """Ping the liveness dead-man's-switch so an external monitor alerts if cycles stop.

    Monitor-agnostic (any ping-URL service; recommended: Healthchecks.io). No-op when
    HEALTHCHECK_URL is unset, and NEVER raises — liveness reporting must not affect the
    control cycle. suffix: "" = success/up, "/fail" = hard failure.
    """
    if not HEALTHCHECK_URL:
        return
    try:
        requests.post(HEALTHCHECK_URL.rstrip("/") + suffix,
                      data=body.encode("utf-8")[:10000], timeout=8)
    except Exception as exc:
        print(f"  (heartbeat ping failed, non-fatal: {exc})", file=sys.stderr)


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    try:
        run_agent(dry_run=dry)
    except Exception as exc:
        # Cycle failed hard (control-path exception, HA totally unreachable, ...). Signal the
        # external monitor so it can alert, then re-raise so the crash stays visible in logs/cron.
        if not dry:
            _send_heartbeat("/fail", f"run_agent crashed: {type(exc).__name__}: {exc}")
        raise
    else:
        # Cycle completed — including LLM-degraded cycles, which now return normally (robustness
        # #1). Ping success so the dead-man's-switch stays satisfied; tag degraded cycles in the
        # body for visibility on the monitor. Skipped for --dry-run (manual test, not production).
        if not dry:
            _degraded = bool(_cycle_context.get("llm_narrative_failed"))
            _send_heartbeat("", "degraded: llm_narrative_failed" if _degraded else "ok")
