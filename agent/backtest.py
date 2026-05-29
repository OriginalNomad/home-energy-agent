#!/usr/bin/env python3
"""
Peak-month scenario backtest for the energy agent.
==================================================
Feeds the REAL agent (real system prompt + real Claude model) a set of
synthetic peak-month scenarios, while stubbing every data-read and intercepting
every write. Nothing touches Tessie or Home Assistant — safe to run anytime.

Purpose: validate the 2:55pm / 85%-SoC demand-window logic BEFORE June 1, when
it first matters for real. Each scenario states the expected decision; you read
the agent's actual reasoning and action and judge whether it matches.

Run:  python backtest.py            # all scenarios
      python backtest.py cloudy     # only scenarios whose name contains "cloudy"
"""

import sys
from datetime import datetime

import pytz

import energy_agent as ea

SYDNEY_TZ = pytz.timezone("Australia/Sydney")


def _state(hour, soc, solar_kw, solar_remaining, forecast_accuracy,
           reserve=5, mode="self_consumption", home_load=0.5,
           this_hour=None, month="June"):
    """Build a get_current_state() payload for a peak-month day."""
    this_hour = solar_kw if this_hour is None else this_hour
    return {
        "timestamp":        f"2026-06-15 {hour:02d}:00 AEST",
        "month":            month,
        "is_peak_month":    True,
        "in_demand_window": 15 <= hour < 21,
        "in_solar_sponge":  10 <= hour < 15,
        "battery": {
            "soc_pct": soc, "soc_gateway_pct": max(soc, reserve),
            "mode": mode, "reserve_pct": reserve, "grid_target_pct": 30,
        },
        "grid": {"price_cents_kwh": 16.0, "in_cheap_window": True},
        "solar": {
            "current_kw": solar_kw, "solcast_power_now_kw": solar_kw,
            "forecast_this_hour_kwh": this_hour,
            "forecast_next_hour_kwh": this_hour,
            "forecast_remaining_kwh": solar_remaining,
            "forecast_accuracy": forecast_accuracy,
        },
        "home_load_kw": home_load,
        "ev": {"plug_status": "EV Disconnected", "plugged_in": False,
               "charging": False, "zappi_mode": "n/a", "soc_pct": None},
    }


def _flat_forecast(price_c, hours=12):
    """Uniform 30-min forecast at a flat price (cents)."""
    return [{"time": f"slot+{i*30}m", "cents_kwh": price_c, "descriptor": "low"}
            for i in range(hours * 2)]


# Each scenario: name, recent-decisions block, state payload, price forecast,
# and a plain-English expectation for the reviewer to check against.
SCENARIOS = [
    {
        "name": "peak-cloudy-10am-40pct",
        "expect": "Unreliable solar + peak month + 40% at 10am → charge from grid "
                  "toward 85% NOW. self_consumption likely OK on time; autonomous if tight.",
        "recent": "  [06-15 09:00] SoC=39% price=16¢ reserve=5% (unchanged) solar=0.0kW (unreliable) | hold\n"
                  "    \"Holding, waiting to see if solar arrives\"\n"
                  "  [06-15 09:30] SoC=40% price=16¢ reserve=5% (unchanged) solar=0.0kW (unreliable) | hold\n"
                  "    \"Still waiting for solar\"",
        "state": _state(10, 40, 0.0, 9.0, "unreliable — actual 0.0kW vs forecast 2.1kW (0% of forecast)"),
        "forecast": _flat_forecast(16.0),
    },
    {
        "name": "peak-cloudy-1330-55pct",
        "expect": "1:30pm, 55%, peak month, no solar → only ~1.4h to 2:55pm. "
                  "self_consumption can't reach 85% in time → AUTONOMOUS now.",
        "recent": "  [06-15 12:30] SoC=50% price=16¢ reserve=70%→70% solar=0.2kW (unreliable) | set_reserve(70%)\n"
                  "    \"Charging self_consumption toward 85% target\"\n"
                  "  [06-15 13:00] SoC=53% price=16¢ reserve=70% (unchanged) solar=0.2kW (unreliable) | hold",
        "state": _state(13, 55, 0.2, 1.0, "unreliable — actual 0.2kW vs forecast 2.0kW (10% of forecast)",
                        reserve=70),
        "forecast": _flat_forecast(16.0),
    },
    {
        "name": "peak-sunny-11am-60pct",
        "expect": "Good solar, 60% at 11am, 8 kWh remaining → solar alone reaches 85% "
                  "by 2:55pm. HOLD or small top-up only — no autonomous.",
        "recent": "  [06-15 10:00] SoC=55% price=16¢ reserve=5% (unchanged) solar=3.1kW (good) | hold\n"
                  "    \"Good solar, on track for 85% via solar\"",
        "state": _state(11, 60, 3.2, 8.0, "good — actual 3.2kW vs forecast 3.4kW (94% of forecast)"),
        "forecast": _flat_forecast(16.0),
    },
    {
        "name": "peak-deferral-trap-1130-45pct",
        "expect": "3 consecutive holds waiting for a cheaper window that never came, "
                  "price flat ~16¢, peak month, 45% at 11:30am → deferral limit fires, "
                  "charge NOW at current price toward 85%.",
        "recent": "  [06-15 10:30] SoC=44% price=16¢ reserve=5% (unchanged) solar=0.3kW (unreliable) | hold\n"
                  "    \"Holding for cheaper window forecast at 12:30\"\n"
                  "  [06-15 11:00] SoC=44% price=16¢ reserve=5% (unchanged) solar=0.3kW (unreliable) | hold\n"
                  "    \"Cheaper window now forecast at 1pm, holding\"",
        "state": _state(11, 45, 0.3, 1.5, "unreliable — actual 0.3kW vs forecast 2.2kW (14% of forecast)"),
        "forecast": _flat_forecast(16.0),
    },
    {
        "name": "peak-255pm-boundary-50pct",
        "expect": "2:45pm, 50%, peak month. Demand window opens at 3pm. Cannot reach 85% "
                  "in 10 min. Agent should NOT import in a way that risks the 3-9pm window; "
                  "should recognise it's effectively out of time. Watch for correct handling.",
        "recent": "  [06-15 14:00] SoC=48% price=16¢ reserve=85%→85% solar=0.1kW (unreliable) | set_reserve(85%), autonomous\n"
                  "    \"Late, escalating to autonomous to reach 85% before 2:55pm\"",
        "state": _state(14, 50, 0.1, 0.3, "unreliable — actual 0.1kW vs forecast 1.0kW (10% of forecast)",
                        reserve=85, mode="autonomous"),
        "forecast": _flat_forecast(16.0),
    },
]


def run_scenario(sc):
    print(f"\n{'#'*70}\n# SCENARIO: {sc['name']}\n{'#'*70}")
    print(f"EXPECT: {sc['expect']}\n")

    # Stub all data-reads
    ea.get_current_state  = lambda: (ea._cycle_context.__setitem__("state", sc["state"]) or sc["state"])
    ea.get_price_forecast = lambda: (ea._cycle_context.__setitem__("price_forecast", sc["forecast"]) or sc["forecast"])
    ea.get_solar_forecast = lambda: []
    ea.get_weather_forecast = lambda: {"tomorrow_solar_outlook": "overcast", "tomorrow_avg_radiation": 80}
    ea.get_recent_decisions = lambda n=3: sc["recent"]

    # Intercept every write
    captured = {"actions": [], "summary": None, "reserve": None, "mode": None, "zappi": None}
    def _reserve(p): captured["reserve"] = p; return f"[backtest] reserve→{p}%"
    def _mode(m):    captured["mode"] = m;    return f"[backtest] mode→{m}"
    def _zappi(m):   captured["zappi"] = m;   return f"[backtest] zappi→{m}"
    def _log(summary, actions_taken):
        captured["summary"] = summary; captured["actions"] = actions_taken
        return "[backtest] logged"
    ea.set_powerwall_reserve = _reserve
    ea.set_powerwall_mode    = _mode
    ea.set_zappi_mode        = _zappi
    ea.log_decision          = _log

    # Rebuild TOOL_MAP so it points at the stubs (lambdas captured originals)
    ea.TOOL_MAP = {
        "get_current_state":     lambda _: ea.get_current_state(),
        "get_price_forecast":    lambda _: ea.get_price_forecast(),
        "get_solar_forecast":    lambda _: ea.get_solar_forecast(),
        "get_weather_forecast":  lambda _: ea.get_weather_forecast(),
        "set_powerwall_reserve": lambda a: ea.set_powerwall_reserve(a["percent"]),
        "set_powerwall_mode":    lambda a: ea.set_powerwall_mode(a["mode"]),
        "set_zappi_mode":        lambda a: ea.set_zappi_mode(a["mode"]),
        "log_decision":          lambda a: ea.log_decision(a["summary"], a["actions_taken"]),
    }

    ea.run_agent(dry_run=False)   # writes are stubbed, nothing real happens

    print(f"\n>>> DECISION: reserve={captured['reserve']} mode={captured['mode']} "
          f"zappi={captured['zappi']}")
    print(f">>> ACTIONS: {captured['actions']}")
    print(f">>> SUMMARY: {captured['summary']}")


if __name__ == "__main__":
    filt = sys.argv[1] if len(sys.argv) > 1 else ""
    for sc in SCENARIOS:
        if filt in sc["name"]:
            run_scenario(sc)
