# Project To-Do List

## Energy Agent — Active

### Immediate

- [ ] **Phase 3 — retire the Mac HA instance** — a second, unused `homeassistant` container still runs on the Mac Studio with a Jun 4 config. Stop it, and repoint the Cloudflare tunnel (`agent.sol.io` → `192.168.68.70:8123`) at the Pi's HA instead. Nothing depends on it day-to-day, but leaving two instances running is how the 7-week config drift went unnoticed.
- [ ] **Explain reserve=5% after a set to 85%** — at 10:35 on 2026-07-22 `sensor.powerwall_backup_reserve` read 5% though the agent set 85% at 10:30 and the det layer had a charge verdict. Check Tessie persistence vs sensor lag vs an automation clearing it.
- [ ] **Explain ~5 kW self_consumption charging** — user observed ~5 kW at 10:12 and SoC jumped 33%→47% in one cycle, against a model (correctly) measuring ~1.5 kW for same-mode intervals. Re-test the reserve−SoC gap hypothesis with mode consistency enforced (the first attempt used a contaminated sample; see energy_log retraction).
- [ ] **Check `solar_unreliable` calibration** — the solar corrector shows Solcast runs at 0.14–0.16 of actual at 08:00–09:00 in winter, so "7% of forecast" mornings may be normal rather than faults. If the flag fires on ordinary winter mornings it is mislabelling them, and it gates real rule behaviour.


- [x] **Run `build_models.py` on Pi** — done 2026-07-22. Required fixing three bugs first (the script had never executed). Solar correction ratios came in at **0.14–0.74**, well below the 0.5–1.5 range anticipated here — Solcast's winter morning over-forecast is far larger than assumed. Re-run periodically to accumulate autonomous samples.

- [x] **Reload HA automations** — `battery_low_soc_emergency_charge` (20¢ ceiling + 85% peak target) and both demand window warning automations (1-min debounce) were updated 2026-06-23. Reloaded 2026-06-24.
- [ ] **Verify HA slider values** — confirm after June 2 restart: `ev_ultra_cheap_threshold_c=6`, `ev_eco_gap_c=1.5`, `battery_charge_price_threshold_c=12`, `battery_max_insurance_floor_pct=70`.

### Architecture roadmap (in order)

- [x] **Phase 2.5-B — solar corrector (wired in, 2026-06-27)** — `optimizer.py` now applies per-hour Solcast correction from `model_params.json["solar_correction"]`; autonomous rate uses `_model_avg_rate_kw()` from `model_params.json["charge_rate_kw"]["autonomous"]`. `build_models.py` created. **Pending**: run `build_models.py` on Pi (see "DO FIRST" item above) to populate the model data — until then both models fall back to their priors (Solcast uncorrected, autonomous flat 5.0 kW).
- [ ] **LP to control path** — **divergence clock restarted 2026-07-22.** The LP ran on a hardcoded 50% SoC from its Jun 1 wire-in until Jul 22 (see energy_log), so *no* prior divergence analysis is valid — including the previously recorded blocker ("LP defers to cheapest slot; det charges at first acceptable slot"), which was never actually measured. Fixed and 4 regression tests added. **Next: collect a fresh week of clean three-way data (from 2026-07-22) before reassessing.** First clean signal to watch: under flat prices the LP defers charging to the last feasible slot with no error margin — likely needs the `risk` knob or a conservative solar quantile. Plan unchanged: LP-authoritative with deterministic layer as hard-constraint backstop (Rule 2, survival floor, reserve guard). Kill-switch already in place.
- [ ] **Analyst agent** — weekly agent that reads `decisions.jsonl` + `daily_energy.jsonl` and surfaces systematic patterns: "cloudy mornings consistently start charging 1h late", "sponge threshold too tight 3 weeks in a row". Outputs proposed rule changes in plain English for human review. This is the feedback loop that makes the system self-improving rather than just self-executing.
- [ ] **Savings dashboard** — daily/weekly $ saved vs naive baseline (flat-rate charging, no demand management, no solar optimisation). Broken down by: demand charge avoided, cheap-window differential, solar self-consumption. Core product metric; also the first Sol feature users need to see.

### Tune from winter data (review at next session and monthly)

- [ ] **Tune `SOLAR_SPONGE_PRICE_THRESHOLD`** — currently 10¢. If winter Solar Sponge prices are regularly 12–18¢, overnight_hold fires too aggressively and battery arrives flat. Check the `rule_fired=overnight_hold_wait_for_sponge` cycles in `decisions.jsonl` against actual sponge prices.
- [ ] **Validate `peak_survival_wait_for_sponge` thresholds** — the 3h window and 5¢ price gap were set on one morning's data (Jun 23). Winter will give dozens of cycles. Check outcomes: did waiting pay off (sponge arrived and was cheaper), or did the battery hit the floor?
- [ ] **Tune historical price model** — `CHEAP_BAND_ALPHA`, `MAX_INSURANCE_FLOOR`, `PRICE_HISTORY_DAYS`. Set on May flat-price data; winter has larger swings. Review after 4 weeks of July data.
- [ ] **Tune `α` / `MIN_DAILY_SWING`** in `_hours_to_cheap_end` — same issue, set on May data.
- [ ] **Amber price forecast accuracy + risk premium** — needs ~4 weeks of JSONL data to compute signed forecast error per time-of-day bucket. 75th-percentile evening error → empirical spread threshold, replacing gut-feel 5¢. Due ~July.
- [ ] **Update charge rate model** — autonomous buckets now populate from `build_models.py` (~5 kW at 20–50% SoC, tapering to 0.97 kW at 90%), but n=2–5 so most sit below `MIN_SAMPLES=5` and still fall back to the flat 5.0 kW prior. Re-run periodically as autonomous cycles accumulate. *(A claim made earlier on 2026-07-22 that the self_consumption model was mis-specified with a p90 ~4 kW tail was **retracted** the same day — it came from a query that ignored mid-interval mode changes. The model is accurate: p25 1.35 / median 1.61 / p90 1.89 kW. See energy_log.)*

### Infrastructure

- [ ] **Watch Tessie reliability** — `tessie_soc_failed` logged in JSONL each cycle. If recurring (>1/day), investigate rate throttle or token renewal.
- [ ] **Switch Pi boot to SSD** — Pi runs from SSD (`/dev/sda2`) but SD card may still be in boot order. Confirm in `raspi-config`.
- [ ] **Tessie cost review** — ~A$10/month. Review savings achieved once first full peak month (June) data is analysed. Replace with Tesla Fleet API (personal OAuth) if cost isn't justified.
- [ ] **Migrate HA to Pi** — Docker HA on Pi, restore from Mac backup. No urgency while Mac Studio stays on, but removes the single point of failure.
- [ ] **Daikin AC integration** — AC load during demand window is the biggest unmodelled variable. If Daikin has a HA integration, it would let the agent anticipate load spikes at 3pm.

### Nice-to-have / low priority

- [ ] **BOM solar forecast** — Solcast unreliable in winter. Investigate `api.weather.bom.gov.au` gridded solar radiation as an alternative or ensemble weight. Needs InfluxDB tracking of Solcast vs actual first.
- [ ] **InfluxDB dashboards** — SoC history, charging patterns, price vs SoC correlation, 3pm accuracy over time. Foundation for the analyst agent.
- [ ] **Dynamic demand-window target** — 85% is conservative. LP naturally computes a smarter target (cover just the 3–9pm load) when authoritative. Not worth building as a rule.
- [ ] **LP `three_way_review.py` live-only filter** — add `--live-only` flag to filter out back-filled records with synthetic solar. Low value now that solar_unreliable is wired in.

---

## Product — Sol

- [ ] **Savings dashboard** — see above; also the first Sol feature
- [ ] **Analyst agent** — see above; directly reusable as Sol's learning layer
- [ ] **Multi-tenant architecture** — define how Sol handles multiple sites with different tariffs, hardware, and grid operators
- [ ] **Tesla Fleet API** — personal OAuth path; also the API Sol would use at scale (no Tessie dependency)
- [ ] **Migrate to Anthropic Managed Agents** — hosted infra solves the Mac-sleep / cron reliability problem; MCP Tunnels for HA access; relevant as Sol's compute layer
- [ ] **Multi-battery support** — Sonnen, BYD, generic Modbus
- [ ] **Multi-tariff support** — Amber AU, Octopus UK, Tibber EU

---

## Done ✅ (key milestones)

- Deterministic rule layer in control (`DETERMINISTIC_AUTHORITATIVE=True`, 2026-06-06)
- LLM narrative-only + prompt slimmed 86% (Phase 6, 2026-06-09)
- Selective narrative — routine cycles skip LLM (Phase 7, 2026-06-23)
- Charge rate model from DB observations (Phase 2.5-A, 2026-06-23)
- LP solar_unreliable fix — stops mpc_solar_only on cloudy mornings (2026-06-23)
- peak_solar_cover_survival + peak_survival_wait_for_sponge rules (2026-06-23)
- Emergency automation hardened: 20¢ ceiling + 85% peak target (2026-06-23)
- Demand window warning debounced: 1-min sensor glitch guard (2026-06-23)
- Three-way shadow layer: LLM vs deterministic vs LP (2026-06-01)
- LP horizon extended to 22:00 with synthetic price model (2026-06-03)
- data_logger.py wired in — energy_log.db accumulating (2026-06-06)
- Tessie SoC=0 guard (2026-06-06)
- Demand-window reserve guard + HA health check (2026-06-02)
- Agent deployed to Pi on cron with auto git pull (2026-06-05)
- Cloudflare Tunnel — agent.sol.io → HA (2026-06-05)
