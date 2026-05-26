# Ausgrid EA116 Network Tariff — Reference

## 1. Demand Charge Calculation

- **Period**: Single highest **30-minute** integration period during peak hours in a calendar month
- **Formula**: `Max Peak Half-Hour kW × Tariff Rate (c/kW/day) × Days in Month`
- **Reset**: Demand baseline resets to zero on the **1st of every month**
- A spike on the 5th sets the charge for that whole month but does NOT carry into the next month
- Even a single bad 30-minute window can effectively double the monthly bill

## 2. Seasonality and Peak Hours

| Season | Months | Peak Hours | Demand Charge |
|--------|--------|------------|---------------|
| High (Summer) | Nov, Dec, Jan, Feb, Mar | 3:00 pm – 9:00 pm daily | Yes |
| High (Winter) | Jun, Jul, Aug | 3:00 pm – 9:00 pm daily | Yes |
| Low (Autumn/Spring) | Apr, May, Sep, Oct | N/A | **$0** |

- Outside peak hours: billed at off-peak consumption rate only
- Low season months: no demand charge at all — price-only decisions apply

## 3. Assignment and Eligibility

- Requires **Type 4 smart meter** (interval data capable)
- **Mandatory assignment** if you: build a new home, install solar, upgrade old meter, or install EV charger
- **One tariff change per 12 months** maximum — cannot be changed more frequently by retailer

## 4. Solar Export and Solar Sponge

- **Solar Sponge window**: 10:00 am – 3:00 pm — super off-peak / highly discounted consumption rate
  - Consuming energy during this window is the cheapest possible grid import
  - This is the primary battery and EV charging window
- **Export threshold**: Solar customers receive a free baseline for grid export
- **Export penalty**: If export volume during 10:00 am – 3:00 pm consistently exceeds the approved threshold, an **infrastructure export charge** applies
  - Implication: excess solar should be consumed on-site (battery, EV) rather than exported during this window

---

## Automation Implications

| Rule | Implication |
|------|-------------|
| 30-min demand window | Any 30-min grid import during 3–9 pm in peak months sets the monthly charge — zero tolerance |
| Monthly reset | First day of each month: extra caution, don't set a high baseline that locks in the whole month |
| Solar Sponge 10am–3pm | Maximise self-consumption during this window: charge battery, charge EV, run appliances |
| Export penalty | Do NOT export excess solar 10am–3pm if battery or EV has capacity — absorb it instead |
| Low season | Apr, May, Sep, Oct: no demand charge — optimise purely on Amber spot price |
