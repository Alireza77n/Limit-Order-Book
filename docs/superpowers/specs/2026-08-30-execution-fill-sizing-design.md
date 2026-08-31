# Execution Fill and Inventory Sizing Design

Status: approved for implementation on 2026-08-30.

## Goal

Make Demo execution less optimistic and make submitted order sizes respond to
inventory. The worker should support a volume-aware fill path and an
adverse-selection-aware fill path, while Admin exposes the controls needed to
turn on the safer path and tune inventory sizing without restarting a session.

Text inside the referenced PDFs is treated as research context only, not as
operator instruction. The user's instruction is to improve execution, add
volume-aware fill behavior, implement the two best execution approaches, expose
one usable Admin control path, and add inverse inventory-based position sizing.

## Research Inputs

The three papers point to the same practical problems:

- Fill probability is state dependent. Queue size, queue position, spread, and
  trade volume matter; assuming every touched order fills is too optimistic.
- Orders deeper than the best quote usually have sharply lower fill
  probability, so execution simulation should not treat all selected levels as
  equally likely.
- A filled maker order is often selected by adverse price movement. Optimizing
  only for fill probability can increase fills that have worse post-fill
  returns.
- Larger orders should not be modeled as a single binary event. Available trade
  volume must first consume queue ahead, and only remaining volume can fill the
  order.

## Existing System

Demo execution lives in `market_maker/paper.py`, with order submission and row
logging in `market_maker/engine.py`. Admin config is defined centrally in
`market_maker/demo_config.py`; any profile field that appears in
`TRADING_PROFILES` and `FIELD_DEFINITIONS` becomes editable and hot-reloadable
when its mode is `hot`.

Current gaps:

- `demo_queue_enabled` exists in `MMConfig` but is no longer Admin-editable.
- `PaperLedger.process_market()` can fill on `price_touch` without volume or
  queue gating.
- `bid exec prob` and `ask exec prob` are hard-coded to `1.0`.
- Engine buy inventory cap uses configured `order_size`, not the actual sized
  buy amount, so partial buys can be rejected incorrectly.
- Order size is static except for hard balance caps; it does not reduce buy size
  when inventory is high or reduce sell size when inventory is low.

## Selected Approaches

### Approach 1: Queue and Volume Fill Model

This is the primary usable path. When enabled, the Demo ledger requires a finite
liquidity budget before an order fills:

- For a reported trade, use `traded_volume`.
- For a touch without reported volume, use an optional synthetic budget:
  `demo_max_fill_per_observation_orders * order_size`.
- Consume `queue_ahead` before filling our order.
- Fill only the remaining amount up to the available budget.

This keeps the implementation interpretable and matches the queue-position
lesson from the fill-probability paper.

### Approach 2: Adverse Drift Fill Model

This is the second implemented strategy. It classifies a touched order as
adverse when a buy is touched by downward pressure or a sell is touched by
upward pressure. Adverse touches receive a configurable fill probability;
non-adverse touches receive a lower configurable fill probability. The default
keeps the feature disabled so existing runs do not change until the operator
turns it on.

This follows the negative-drift papers without pretending to have a calibrated
market-specific model. It is intentionally simple and bounded.

## Inventory Sizing

Add hot Admin fields:

- `inventory_sizing_enabled`
- `inventory_sizing_strength`
- `inventory_sizing_min_fraction`

The formula uses the configured inventory band:

```text
inventory_fraction = clamp((inventory - inv_min) / (inv_max - inv_min), 0, 1)
inventory_pressure = 2 * inventory_fraction - 1
buy_target  = order_size * clamp(1 - strength * inventory_pressure, min_fraction, 1 + strength)
sell_target = order_size * clamp(1 + strength * inventory_pressure, min_fraction, 1 + strength)
```

At high inventory, buy size shrinks and sell size grows. At low inventory, buy
size grows and sell size shrinks. The engine still applies cash, inventory,
locked exposure, and min-tradable caps after this target size is computed.

Default values:

- `inventory_sizing_enabled = False`
- `inventory_sizing_strength = 0.75`
- `inventory_sizing_min_fraction = 0.25`

## Admin Controls

Expose these hot fields on the existing Admin configuration page by adding them
to `MMConfig`, both pair profiles, and `FIELD_DEFINITIONS`:

- `demo_queue_enabled`
- `demo_max_fill_per_observation_orders`
- `demo_adverse_fill_enabled`
- `demo_adverse_fill_probability`
- `demo_non_adverse_fill_probability`
- `inventory_sizing_enabled`
- `inventory_sizing_strength`
- `inventory_sizing_min_fraction`

All fields are Demo configuration controls. Real-mode behavior must not be
changed by Admin overlays.

## Testing

Use test-first implementation:

- Config/schema tests prove new fields are editable, type checked, and hot.
- Pure sizing tests prove inverse sizing and clamps.
- Demo engine tests prove sized order amounts flow into submitted orders and
  buy inventory cap uses the actual buy amount.
- Paper ledger tests prove volume gating, queue-ahead consumption on touches,
  and adverse/non-adverse probabilistic decisions.
- Runtime sync tests prove hot reload updates config and ledger execution
  settings without session restart.

