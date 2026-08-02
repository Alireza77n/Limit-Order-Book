# Crypto Market-Making Project

## Project info

This repository contains implementations and experiments focused on **limit order book (LOB) dynamics** in cryptocurrency markets.

The project currently includes:

* **Algorithm 1:** a custom depth-aware, inventory-skewed quoting strategy — **implemented, tested, and runnable live** against Nobitex.
* **Algorithm 2:** the Avellaneda-Stoikov optimal market-making model — **not implemented**. Only documented below and explored in parameter-estimation notebooks (`market_maker/research/`). See [Algorithm 2](#algorithm-2-avellaneda-stoikov-model) for the full model and [Known limitations / next steps](#known-limitations--next-steps) for the honest picture of what's missing.

The goal of this project is to simulate, backtest, and compare different market-making approaches on cryptocurrency order book data.

| | Algorithm 1 | Algorithm 2 |
|---|---|---|
| Status | Implemented, tested | **Not implemented** (stub only) |
| Code | `market_maker/strategy/algo1.py` | `market_maker/strategy/algo2.py` (raises `NotImplementedError`) |
| Runnable live | Yes, via `run.py --algorithm algo1` | No |

---

## Table of contents

- [Limit order book and market-making fundamentals](#limit-order-book-and-market-making-fundamentals) — start here if you're new to the topic
  - [What is a limit order book?](#what-is-a-limit-order-book)
  - [Key order book terms](#key-order-book-terms)
  - [What is market making?](#what-is-market-making)
  - [Core risks a market maker faces](#core-risks-a-market-maker-faces)
  - [Two approaches in this repository](#two-approaches-in-this-repository)
- [Algorithm 1: Depth-Aware Inventory-Skewed Quoting Strategy](#algorithm-1-depth-aware-inventory-skewed-quoting-strategy)
  - [Main idea](#main-idea)
  - [Order book notation](#order-book-notation)
  - [Step 1: Inventory normalization](#step-1-inventory-normalization)
  - [Step 2: Inventory-dependent level-pair generation](#step-2-inventory-dependent-level-pair-generation)
  - [Step 3: Spread-feasibility condition](#step-3-spread-feasibility-condition)
  - [Step 4: Final quote construction](#step-4-final-quote-construction)
  - [Step 5: Reported spreads](#step-5-reported-spreads)
  - [Algorithm 1 summary](#algorithm-1-summary)
  - [Algorithm 1 pseudocode](#algorithm-1-pseudocode)
  - [Output of Algorithm 1](#output-of-algorithm-1)
  - [Why Algorithm 1 is useful](#why-algorithm-1-is-useful)
  - [Algorithm 1 in practice: three scenarios](#algorithm-1-in-practice-three-scenarios)
- [Algorithm 2: Avellaneda-Stoikov Model](#algorithm-2-avellaneda-stoikov-model)
  - [The idea](#the-idea)
  - [The formulas](#the-formulas)
  - [Reading the formulas in plain English](#reading-the-formulas-in-plain-english)
  - [What's missing to implement it](#whats-missing-to-implement-it)
  - [Algorithm 1 vs. Algorithm 2](#algorithm-1-vs-algorithm-2)
- [Architecture](#architecture)
- [Project structure](#project-structure)
- [Tracked vs. local-only files](#tracked-vs-local-only-files)
- [Setup, running, and testing](#setup-running-and-testing)
- [Known limitations / next steps](#known-limitations--next-steps)
- [Extending](#extending)
- [Example workflow](#example-workflow)
- [Research purpose](#research-purpose)
- [Disclaimer](#disclaimer)

---

# Limit order book and market-making fundamentals

This section is a from-scratch primer. If you already know what a bid, an
ask, and a spread are, skip to [Algorithm 1](#algorithm-1-depth-aware-inventory-skewed-quoting-strategy).

## What is a limit order book?

Almost every modern exchange — crypto or otherwise — matches buyers and
sellers through a **limit order book (LOB)**. Instead of negotiating
directly with a counterparty, every participant submits a **limit order**:
an instruction of the form "buy up to X units at price P or better" (or
the sell equivalent). The exchange collects every open order into two
ordered lists:

* The **bid side** — every outstanding *buy* order, sorted from the
  highest offered price (the most eager buyer) down to the lowest.
* The **ask side** (also called the *offer* side) — every outstanding
  *sell* order, sorted from the lowest offered price (the most eager
  seller) up to the highest.

A trade happens the instant a new order's price crosses an order already
resting on the other side — the exchange matches them automatically, at
the resting order's price. Everything that doesn't cross yet simply
*rests* in the book, visible to everyone, until it's matched or cancelled.

Two orders at the exact same price are typically matched in **price-time
priority**: the better price is always filled first, and among orders at
the same price, the one that arrived first is filled first. This is why
being an early, well-priced order in the book matters — it's queued ahead
of everyone who arrives later at the same price.

## Key order book terms

These are used throughout this README and the codebase; Algorithm 1's
notation below builds directly on them.

| Term | Meaning |
|---|---|
| **Best bid** (`B0`) | The highest price any buyer is currently willing to pay. |
| **Best ask** (`A0`) | The lowest price any seller is currently willing to accept. |
| **Spread** | `A0 - B0` — the gap between the best bid and best ask. A tighter spread generally means a more liquid, more competitively traded market. |
| **Mid-price** | `(B0 + A0) / 2` — a common single-number reference for "the price" of the asset right now, even though no trade necessarily happens exactly there. |
| **Depth / level** | Orders don't all sit at the best price — some rest one tick below the best bid (`B1`), one tick below that (`B2`), and so on (mirrored on the ask side as `A1`, `A2`, ...). "Depth" is how much volume is available at or near the top of the book; a "level" is one specific price tier away from the touch. |
| **Tick size** | The smallest allowed price increment on the exchange — prices can only take values that are multiples of the tick size. |
| **Order book imbalance** | The relative amount of resting volume on the bid side vs. the ask side. Heavy bid-side volume can signal upward pressure (more people want in than out), and vice versa — though it's a signal, not a guarantee. |
| **Inventory** | How much of the asset a market maker currently holds (positive = long, negative = short), as a result of its own past fills. |

## What is market making?

A **market maker** is a participant who continuously quotes *both* sides
of the book at once — a bid to buy and an ask to sell, usually straddling
the current mid-price — rather than taking a directional view on where
price is headed. Their business is the **spread**: buy low (at their bid,
when someone sells to them), sell high (at their ask, when someone buys
from them), and pocket the difference, over and over, at high volume and
thin margin per trade.

This is a genuine service to the market — it's what lets any other trader
buy or sell *immediately* at a reasonable price instead of waiting for a
matching counterparty to show up — and exchanges often reward market
makers with lower fees (sometimes rebates) in exchange for it. But it is
not free money: every quote a market maker posts is a standing offer that
*anyone* can trade against, including someone who knows something the
market maker doesn't yet. That asymmetry is where most of the risk below
comes from.

## Core risks a market maker faces

* **Inventory risk.** Every fill on one side leaves the market maker
  holding more or less inventory than before. If the price then moves
  against that position before it can be worked back to neutral, the
  market maker loses money on the position itself — separately from
  whatever spread it earned getting into it. This is *the* central
  tension a market-making strategy has to manage: quote tightly and
  symmetrically to maximize fill rate and spread capture, or skew quotes
  to manage inventory and accept fewer/worse fills. Both algorithms in
  this repo exist specifically to answer that tradeoff — Algorithm 1 by
  a direct, rule-based inventory skew (see [Step 2](#step-2-inventory-dependent-level-pair-generation)); Algorithm 2 by solving
  it as a formal utility-maximization problem (see [The formulas](#the-formulas)).
* **Adverse selection risk.** A market maker's quotes are filled
  precisely when someone *wants* to trade against them — and the people
  most eager to trade right now are often the ones who know the price is
  about to move (news, a large incoming order, a faster/better-informed
  trader). Getting filled isn't automatically good news; it can mean
  being picked off just before the market moves against the new
  position.
* **Execution risk.** The reverse problem: a quote sitting in the book is
  not guaranteed to be filled at all. If the market moves away before
  anyone trades against it, the market maker earns nothing on that quote
  and has to reprice — repeatedly, if the market keeps moving.
  Quoting too conservatively (too far from the touch) reduces adverse
  selection risk but increases execution risk; quoting too aggressively
  does the opposite. There is no setting that eliminates both.
* **Order book / liquidity risk.** Depth is not static — it can thin out
  or disappear during volatile periods, exactly when a market maker most
  needs to unwind inventory. A strategy that assumes yesterday's depth
  will still be there today can end up quoting into a much thinner book
  than intended.
* **Tick-size and fee constraints.** A quote isn't valid or meaningful
  just because it looks profitable on paper — it has to land on an
  allowed tick, and it has to clear the exchange's trading commission
  *and* whatever minimum profit margin the strategy requires, or the
  "profit" is actually a loss once fees are paid. Both algorithms enforce
  this explicitly rather than assuming it away — see Algorithm 1's
  [Step 3](#step-3-spread-feasibility-condition) and this project's `commission_perc`/`exp_ret_min` config fields.

## Two approaches in this repository

1. A **deterministic depth-aware strategy** ([Algorithm 1](#algorithm-1-depth-aware-inventory-skewed-quoting-strategy)):
   a rule-based heuristic that reads inventory and order book depth
   directly and picks quotes from a fixed decision procedure — no
   probability model, no calibration required.
2. A **model-based optimal strategy** ([Algorithm 2](#algorithm-2-avellaneda-stoikov-model)):
   the Avellaneda-Stoikov framework, which models the market
   stochastically and *solves* for the utility-maximizing quotes given
   that model, at the cost of needing calibrated parameters for how the
   market actually behaves.

Algorithm 1 is a practical baseline that works today with no data-fitting
step. Algorithm 2 is a theoretical benchmark — more principled in theory,
but only as good as its calibration in practice, and not yet implemented
in this repository (see [Known limitations / next steps](#known-limitations--next-steps)).

---

# Algorithm 1: Depth-Aware Inventory-Skewed Quoting Strategy

## Main idea

Algorithm 1 is a rule-based market-making strategy.

Instead of always quoting at the best bid and best ask, the algorithm looks deeper into the order book and selects bid and ask levels according to the current inventory.

The strategy follows this logic:

```text
inventory
    -> inventory imbalance index
    -> candidate bid/ask level pairs
    -> spread-feasibility check
    -> final bid and ask quotes
```

The final bid and ask quotes are placed **one tick inside** the selected order book levels.

---

## Order book notation

Let the bid side of the order book be:

```math
\mathcal{B} = \{B_0, B_1, \dots, B_D\}
```

where:

```math
B_0 \ge B_1 \ge \dots \ge B_D
```

Here, (B_0) is the best bid price, and deeper bid levels have lower prices.

Let the ask side of the order book be:

```math
\mathcal{A} = \{A_0, A_1, \dots, A_D\}
```

where:

```math
A_0 \le A_1 \le \dots \le A_D
```

Here, (A_0) is the best ask price, and deeper ask levels have higher prices.

The maximum usable order book depth is:

```math
D
```

The tick size is:

```math
\epsilon
```

In this codebase, `Algo1Strategy.tick_size()` sets `epsilon = 10` for
IRT-quoted pairs (BTCIRT, USDTIRT) and `epsilon = 0.01` for everything
else — the actual project only ever runs the IRT pairs, so `epsilon = 10`
in practice today.

The mid-price is defined as:

```math
m = \frac{B_0 + A_0}{2}
```

---

## Step 1: Inventory normalization

Let the current inventory be:

```math
q
```

The strategy assumes that inventory is bounded by:

```math
q_{\min} \le q \le q_{\max}
```

The current inventory is converted into a discrete inventory imbalance index:

```math
d_q =
\operatorname{int}
\left(
10
\frac{q-q_{\min}}{q_{\max}-q_{\min}}
\right)
-5
```

This creates an integer-valued inventory signal.

Approximately:

* (d_q < 0): inventory is low or negative
* (d_q \approx 0): inventory is close to neutral
* (d_q > 0): inventory is high or positive

This inventory index is used to skew the bid and ask quote levels.

---

## Step 2: Inventory-dependent level-pair generation

The algorithm generates a sequence of candidate bid and ask level pairs:

```math
\mathcal{P}(d_q,D)
=
\left[
(b_1,a_1),
(b_2,a_2),
\dots,
(b_K,a_K)
\right]
```

Each candidate pair is:

```math
(b_k,a_k)
```

where:

* (b_k) is the candidate bid level
* (a_k) is the candidate ask level

In the code, this is generated by `Algo1Strategy.leg_pairs()` (`market_maker/strategy/algo1.py`):

```python
pairs = self.leg_pairs(inv_d, depth_max_local)
```

The exact sequence depends on the inventory imbalance index (d_q).

The purpose of this step is to adjust quote aggressiveness according to inventory.

When inventory is high, the strategy should generally:

* quote less aggressively on the bid side
* quote more aggressively on the ask side

This discourages further buying and encourages selling.

When inventory is low or negative, the strategy should generally:

* quote more aggressively on the bid side
* quote less aggressively on the ask side

This encourages buying and discourages selling.

When inventory is close to neutral, the strategy tends to quote more symmetrically.

---

## Step 3: Spread-feasibility condition

For each candidate pair ((b_k,a_k)), the algorithm checks whether quoting at those levels would actually be **profitable after fees** — not just whether the two prices don't cross.

This is the exact condition `Algo1Strategy.spread_ok()` checks:

```math
\frac{A_{a_k} - B_{b_k}}{L} \; > \; c + r_{\min}
```

where:

* (L) is the last traded price (the denominator that turns an absolute price gap into a *fractional* spread, comparable across assets and price levels)
* (c) is `commission_perc` — the exchange's round-trip trading commission, as a fraction
* (r_{\min}) is `exp_ret_min` — the minimum extra profit margin required *beyond* covering fees. If not explicitly configured, it defaults to `commission_perc` itself (i.e. the required gross spread becomes at least `2 * commission_perc`) — see `SimpleMarketMaker.__init__` in `market_maker/engine.py`.

In code:

```python
return (ask - bid) / last > (self.cfg.commission_perc + self.cfg.exp_ret_min)
```

In plain terms: a candidate pair is only feasible if the fractional gap between its ask and bid levels is wide enough to pay the trading fee on both legs *and* still leave the configured minimum profit on top. A pair that would merely break even, or lose money after fees, is rejected — this is what keeps the strategy from quoting itself into a guaranteed loss just to win a fill.

(Separately, once a pair is *selected*, [Step 4](#step-4-final-quote-construction) moves each quote one tick further inward before sending it — that's a placement rule for slightly better fill priority, not part of this feasibility check.)

The algorithm scans the candidate pairs sequentially, starting from the widest/most conservative pair and walking toward tighter ones (`Algo1Strategy.leg_pairs()` generates them in that order). It keeps updating the selected bid and ask levels while the condition holds. Once it fails, the search stops — so a *later*, tighter pair is never selected once an earlier, wider one has already failed.

Therefore, the selected pair is the last feasible pair before the first infeasible pair:

```math
(b^\star,a^\star)
=
\text{last feasible pair in }
\mathcal{P}(d_q,D)
```

The selected levels are then clipped to remain inside the available order book depth:

```math
b^\star = \min(\max(b^\star,0),D)
```

```math
a^\star = \min(\max(a^\star,0),D)
```

---

## Step 4: Final quote construction

After selecting the final bid and ask levels, the algorithm extracts the corresponding raw order book prices:

```math
B_{b^\star}
```

and:

```math
A_{a^\star}
```

Then it places quotes one tick inside those levels.

The final bid quote is:

```math
p_b = B_{b^\star} + \epsilon
```

The final ask quote is:

```math
p_a = A_{a^\star} - \epsilon
```

This means that:

* the bid quote is slightly more aggressive than the selected bid level
* the ask quote is slightly more aggressive than the selected ask level

---

## Step 5: Reported spreads

The algorithm also reports the distance of each quote from the mid-price.

The bid-side spread is defined as:

```math
s_b = m - p_b
```

Since the final bid quote is:

```math
p_b = B_{b^\star} + \epsilon
```

we obtain:

```math
s_b = m - \left(B_{b^\star} + \epsilon\right)
```

Therefore:

```math
s_b = m - B_{b^\star} - \epsilon
```

The ask-side spread is defined as:

```math
s_a = p_a - m
```

Since the final ask quote is:

```math
p_a = A_{a^\star} - \epsilon
```

we obtain:

```math
s_a = \left(A_{a^\star} - \epsilon\right) - m
```

Therefore:

```math
s_a = A_{a^\star} - \epsilon - m
```

So the reported spreads are:

```math
s_b = m - B_{b^\star} - \epsilon
```

and:

```math
s_a = A_{a^\star} - \epsilon - m
```

---

## Algorithm 1 summary

The full algorithm can be summarized as:

```text
current inventory q
    -> inventory index d_q
    -> candidate level pairs P(d_q, D)
    -> selected feasible pair (b*, a*)
    -> final quotes (p_b, p_a)
```

Mathematically, the inventory index is:

```math
d_q =
\operatorname{int}
\left(
10
\frac{q-q_{\min}}{q_{\max}-q_{\min}}
\right)
-5
```

The selected pair is:

```math
(b^\star,a^\star)
=
\text{last feasible candidate pair}
```

subject to:

```math
B_{b^\star} + \epsilon < A_{a^\star} - \epsilon
```

The final quotes are:

```math
p_b = B_{b^\star} + \epsilon
```

and:

```math
p_a = A_{a^\star} - \epsilon
```

---

## Algorithm 1 pseudocode

```text
Input:
    order book ob
    current inventory q
    inventory limits q_min and q_max
    tick size epsilon
    maximum usable depth D

Step 1:
    Compute inventory imbalance index:

        d_q = int(10 * (q - q_min) / (q_max - q_min)) - 5

Step 2:
    Generate candidate bid/ask level pairs:

        pairs = P(d_q, D)

Step 3:
    Initialize:

        bid_level = D
        ask_level = D

Step 4:
    For each candidate pair (b, a):

        If spread condition is satisfied:
            bid_level = b
            ask_level = a
        Else:
            stop searching

Step 5:
    Clip selected levels into valid order book range:

        bid_level = min(max(bid_level, 0), D)
        ask_level = min(max(ask_level, 0), D)

Step 6:
    Read selected raw prices from the order book:

        raw_bid = B_bid_level
        raw_ask = A_ask_level

Step 7:
    Place quotes one tick inside selected levels:

        bid_price = raw_bid + epsilon
        ask_price = raw_ask - epsilon

Step 8:
    Compute mid-price:

        mid_price = (best_bid + best_ask) / 2

Step 9:
    Compute reported spreads:

        bid_spread = mid_price - bid_price
        ask_spread = ask_price - mid_price

Output of this stage (algo1_bid_ask_prices):
    bid price
    ask price
    selected bid level
    selected ask level
    last traded price
    bid spread
    ask spread

Step 10 (the actual entry point the trading engine calls -- decide_order):
    Re-check the spread condition (Step 3) on the FINAL selected pair,
    the same way Step 4's scan checked every candidate:

        If spread condition holds on (bid_level, ask_level):
            return the Step 1-9 output above -- place this quote
        Else:
            return "no quote this round" (None) -- do not place any order

    This matters specifically when Step 4 never found a feasible pair at
    all: bid_level/ask_level are still sitting at their Step 3 initial
    value (D, D -- the widest, most conservative pair) rather than a pair
    that was actually confirmed profitable. Step 10 is what stops that
    unconfirmed fallback from ever being sent as a real order -- if even
    the widest possible spread doesn't clear the profitability bar, the
    round produces no quote instead of a guaranteed-unprofitable one.
```

---

## Output of Algorithm 1

For each order book snapshot where a quote **is** placed, Algorithm 1 returns a dictionary:

```python
{
    "bid": bid_price,
    "ask": ask_price,
    "bid level": bid_level,
    "ask level": ask_level,
    "lastTradePrice": last,
    "bid spread": mid_price - bid_price,
    "ask spread": ask_price - mid_price,
    "bid exec prob": 1.0,
    "ask exec prob": 1.0
}
```

The most important outputs are:

* **bid:** final bid quote
* **ask:** final ask quote
* **bid level:** selected bid depth level
* **ask level:** selected ask depth level
* **lastTradePrice:** most recent traded price
* **bid spread:** distance between the mid-price and the bid quote
* **ask spread:** distance between the ask quote and the mid-price
* **bid exec prob:** placeholder execution probability for the bid side
* **ask exec prob:** placeholder execution probability for the ask side

When no candidate pair clears the profitability bar (Step 10), `decide_order()`
returns `None` instead of this dictionary — the engine places no order that
round. In demo/paper mode this is logged as a `no_quote` round with the
specific reason (`invalid_or_single_level_order_book`,
`inventory_skew_unexpressible_in_shallow_book`, or the general
`profitability_threshold` case) rather than silently doing nothing — see
usage.md §1b and `market_maker/no_quote.py`.

---

## Important implementation note

In the current implementation, the execution probabilities are placeholders:

```python
"bid exec prob": 1.0,
"ask exec prob": 1.0
```

This means that Algorithm 1 does not currently estimate fill probability using an order-arrival or intensity model.

Future versions could replace these placeholders with empirical execution probabilities based on:

* quote distance from the mid-price
* order book imbalance
* recent trade direction
* recent volatility
* historical fill statistics
* queue position
* trade arrival intensity

---

## Why Algorithm 1 is useful

Algorithm 1 has several practical advantages:

* It is fast and deterministic.
* It directly uses order book depth.
* It does not require calibration of a stochastic price model.
* It adjusts quotes according to current inventory.
* It avoids crossed quotes through a spread-feasibility check.
* It provides a practical baseline for comparison with more advanced models.

Unlike the Avellaneda-Stoikov model, Algorithm 1 does not solve an optimization problem. It is a heuristic strategy that converts inventory and order book information into practical bid and ask quotes.

---

## Algorithm 1 in practice: three scenarios

These are the exact scenarios pinned by `market_maker/tests/test_algo1_baseline.py` (mirrored more lightly in `test_strategy_algo1.py`), shown here to make the abstract steps above concrete. All three use an 11-level synthetic order book (bids at 100, 99, 98, ... ; asks at 101, 102, 103, ...; last trade price 100), pair `"BTCUSDT"` (tick size 0.01 — the project's real pairs, BTCIRT/USDTIRT, use tick size 10 instead), and vary only `commission_perc` (with `exp_ret_min` defaulting to it).

**Scenario A — spread condition always holds (`commission_perc = 0.0025`, a loose threshold).** The scan walks all the way to the shallowest candidate pair without ever hitting an infeasible one. At neutral inventory (`q = 0.5`, `d_q = 0`), the result is bid level 1 / ask level 1 — quoting close to the touch. At maximum inventory (`q = 1.0`), the ask level is pulled to 0 (most aggressive sell) while the bid level backs off to 5 (least aggressive buy) — exactly the "discourage buying, encourage selling" behavior Step 2 describes. `decide_order()` places this quote every time — the re-check in Step 10 always passes here.

**Scenario B — spread condition never holds (`commission_perc = 0.3`, a deliberately strict threshold no real spread clears).** Every candidate pair fails the feasibility check on the very first iteration, so `algo1_bid_ask_prices()` (the helper) never moves the levels off their Step 3 initial value (`depth_max`, the deepest/widest level) regardless of inventory, and still returns a priced-but-unconfirmed dict for that pair. **`decide_order()` — what the engine actually calls — returns `None` in this scenario**, because Step 10's re-check applies the exact same failing condition to that fallback pair. This is a real `no_quote` round (reason `profitability_threshold`), not a quote at the widest available levels; the helper's own return value is only meaningful directly for diagnostics/analysis, not for what gets sent to the exchange.

**Scenario C — the scan breaks partway through (`commission_perc = exp_ret_min = 0.05`, a mid-range threshold).** At neutral inventory the deepest five pairs are feasible and the sixth is not, so the search stops at level 5/5 — a real, confirmed pair, so `decide_order()` places this quote. This is the general case: neither always-feasible nor never-feasible, the "last feasible pair before the first infeasible pair" rule from Step 3 actually deciding the outcome.

Together these three cases cover the full range of what `spread_ok` can do to the scan (never stops, stops immediately, stops partway) — which is why they're the baseline characterization tests: any future change to Algorithm 1's internals that alters one of these three outcomes is a behavior change, not a refactor. Only Scenario B additionally demonstrates the Step 10 gate, since it's the only one of the three where the helper's own fallback value is unprofitable.

---

# Algorithm 2: Avellaneda-Stoikov Model

**Current status: not implemented.** This section documents the model this repository intends to implement, not a working feature. `market_maker/strategy/algo2.py` (`Algo2Strategy`) is a placeholder whose `decide_order()` raises `NotImplementedError`; `market_maker/research/` holds notebooks that estimate two of the model's parameters (see below) but do not implement the quoting decision itself.

## The idea

In the Avellaneda-Stoikov framework:

* the mid-price is modeled as a stochastic process (typically arithmetic Brownian motion with volatility (\sigma))
* order arrivals depend on quote distance from the mid-price, with intensity governed by a decay parameter (\kappa)
* the market maker chooses bid and ask quotes to maximize expected utility over a finite time horizon
* the utility function balances expected profit against inventory risk, controlled by a risk-aversion parameter (\gamma)

The model produces two things: a **reservation price** (a mid-price adjusted for current inventory) and an **optimal spread** around it.

## The formulas

The reservation price, given current mid-price (s), inventory (q), risk aversion (\gamma), volatility (\sigma), and time remaining (T-t):

```math
r(s, q, t) = s - q \, \gamma \, \sigma^2 \, (T - t)
```

Positive inventory ((q > 0)) shifts the reservation price down (encouraging sells); negative inventory shifts it up (encouraging buys) — the same qualitative goal as Algorithm 1's inventory skew, reached through a different, stochastic-control derivation.

The optimal total spread around the reservation price:

```math
\delta_b + \delta_a = \gamma \, \sigma^2 \, (T - t) + \frac{2}{\gamma} \ln\left(1 + \frac{\gamma}{\kappa}\right)
```

giving bid/ask quotes:

```math
p_b = r(s,q,t) - \delta_b, \qquad p_a = r(s,q,t) + \delta_a
```

(In the symmetric formulation above, half the total spread sits on each
side: (\delta_b = \delta_a = \frac{1}{2}(\delta_b+\delta_a)), so the bid
and ask are placed equally far from the reservation price.)

## Reading the formulas in plain English

The reservation price is what Algorithm 1's inventory skew ([Step 2](#step-2-inventory-dependent-level-pair-generation))
is doing informally, made precise: instead of picking a *level* to shift
toward, Avellaneda-Stoikov computes an actual *price* — a "fair value"
that has already been shifted away from the true mid-price (`s`) to
account for inventory risk, and centers both quotes on that adjusted
price instead of on the raw mid-price.

* **`-q * gamma * sigma^2 * (T-t)`** is the size of that shift. It grows
  with three things, each for an intuitive reason:
  * **`q`** (inventory): more inventory, more risk, more urgency to shift
    the price to encourage trading it off.
  * **`sigma^2`** (volatility): in a calmer market, holding inventory
    for a while is not very risky, so the model barely needs to shift
    away; the more volatile the price, the more that same inventory
    could swing in value before it's unwound, so the shift grows.
  * **`(T-t)`** (time remaining): far from the end of the trading
    horizon, there's more time left for the price to move against an
    open position, so the model shifts more; as `t -> T`, this term
    shrinks toward zero and the reservation price converges back to the
    plain mid-price.
  * **`gamma`** (risk aversion) scales all of the above — a more
    risk-averse market maker shifts its price further for the same
    inventory/volatility/time.
* **The optimal spread** `delta_b + delta_a` has two additive pieces with
  different jobs:
  * **`gamma * sigma^2 * (T-t)`** — the same "risk penalty" term as the
    reservation-price shift, widening the spread to compensate for
    holding inventory risk over the remaining horizon.
  * **`(2/gamma) * ln(1 + gamma/kappa)`** — a separate term driven by
    **`kappa`**, how quickly order arrivals fall off as a quote moves
    away from the mid-price (a market where orders arrive readily even
    far from mid has a *small* effect from widening — low `kappa`
    sensitivity — while a market where arrivals drop off sharply as you
    move away punishes a wide quote with far fewer fills — high `kappa`
    sensitivity). This term balances "quote wide enough to earn a good
    spread per fill" against "don't quote so wide that fills stop
    arriving at all."

The practical difference from Algorithm 1: Algorithm 1 answers "which
*existing order-book level* should I quote at?" using today's real book
depth. Avellaneda-Stoikov answers "what *price* is optimal?" using a
model of how the market behaves — which is more principled if that model
is correctly calibrated to the real market ((\sigma) and (\kappa) fit from
data), but produces nothing meaningful if it isn't.

## What's missing to implement it

* Coding the formulas above into `Algo2Strategy.decide_order()`.
* A source for (\sigma) (volatility) and (\kappa) (order-arrival intensity decay) — `market_maker/research/sigma_estimation.ipynb` and `market_maker/research/kappa_estimation.ipynb` estimate these from historical trade/order-book data, but as of today are exploratory notebooks with hardcoded local file paths, not a callable estimation module.
* A decision on (\gamma) (risk aversion) and (T) (time horizon) as configuration.
* Characterization tests analogous to Algorithm 1's, once there's real behavior to pin.

## Algorithm 1 vs. Algorithm 2

* Algorithm 1 is deterministic and order-book based; Avellaneda-Stoikov is stochastic and model-based.
* Algorithm 1 is easier to implement, interpret, and test; Avellaneda-Stoikov requires calibrating (\sigma) and (\kappa) from data.
* Algorithm 1 is implemented and running; Avellaneda-Stoikov is a documented target, not yet built.

---

# Architecture

```
                  ┌─────────────────────┐
   run.py  ─────► │  SimpleMarketMaker   │  (the engine: run() loop,
  --algorithm     │      (engine.py)      │   inventory/cash/PnL bookkeeping,
  --exchange      └──────────┬───────────┘   CSV + MySQL persistence,
  --profile                  │               optional Telegram notify)
  --mode         ┌───────────┼───────────┐
                 ▼                       ▼
        ┌─────────────────┐      ┌──────────────────┐
        │     Strategy      │      │   ExchangeClient   │
        │ (strategy/base.py) │      │ (exchange/base.py)  │
        └────────┬─────────┘      └─────────┬────────┘
                 │                           │
     ┌───────────┴───────────┐    ┌──────────┼──────────────┐
     ▼                       ▼    ▼                          ▼
Algo1Strategy          Algo2Strategy   NobitexExchangeClient   DemoExchangeClient
(implemented,           (stub, raises  (get_orderbook,          (wraps a real
 fully tested)           NotImplementedError) send_order,        NobitexExchangeClient for
                                              wallet_balance,     market data; send_order is
                                              order status/       simulated -- --mode demo)
                                              cancellation via
                                              order_status.py)
```

* **`Strategy`** (`market_maker/strategy/base.py`): the only method the engine relies on is `decide_order(orderbook, inventory) -> quote_dict | None`. Anything implementing this can be plugged into `SimpleMarketMaker` via its optional `strategy=` constructor argument (defaults to `Algo1Strategy` if omitted).
* **`ExchangeClient`** (`market_maker/exchange/base.py`): `get_orderbook`, `send_order`, `wallet_balance`, `get_order_list_status`, `cancel_order`, `cancel_all_open_orders`, `cancel_old_orders`. `SimpleMarketMaker` takes an `exchange=` constructor argument (required) implementing this. Two implementations exist: `NobitexExchangeClient` (real) and `DemoExchangeClient` (real market data, simulated fills — see usage.md §1b).
* **`portfolio.py`** (`Nobitex_portfolio`): read-only wallet/portfolio valuation helper, independent of the trading engine. **Still Nobitex-specific** — not behind `ExchangeClient` (see [Known limitations](#known-limitations--next-steps)).
* **`order_status.py`** (`OrderStatusChecker`, `OrderCanceller`): raw Nobitex order-status/cancellation HTTP calls, composed inside `NobitexExchangeClient` rather than called directly by the engine.
* **`config.py`** (`MMConfig`): a single dataclass holding both strategy knobs (`inv_max`, `depth_max`, `commission_perc`, ...) and exchange config (`token`, `url_send_order`, ...). Not yet split — see [Known limitations](#known-limitations--next-steps). Also defines `TRADING_PROFILES` (every currency pair's full config, including its demo starting inventory) and `RESULTS_DIR`/`demo_csv_path` (where `run.py` and `dashboard.py` both write/read CSV output) — the single source of truth both modules import from, so they can never disagree about a pair's settings or file locations.
* **`run.py`**: the single entry point. `STRATEGIES`/`EXCHANGES` registries (algorithm/exchange choices) plus `config.TRADING_PROFILES` (currency-pair configs) select what actually runs; `--mode real|demo` selects real vs. simulated order placement.
* **Telegram notifications & cost tracking** (`engine.py` + `telegram_lifecycle.py`, optional): `SimpleMarketMaker` can push a startup message, a per-round summary (quote, market data, inventory/cash/wealth, buy/sell activity, transaction cost this round, and cumulative cost per-symbol and combined), and threaded order-lifecycle replies (opened/partial/filled/cancelled, each reason-tagged) to a Telegram chat if `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are set. Each round's summary is the parent message that lifecycle updates reply to. Cumulative fee totals persist across restarts in `results/paper_fee_scopes.json`. Entirely best-effort — never raises, never required. See usage.md §1.7/§6.8.

---

# Project structure

Everything lives under one top-level package, `market_maker/`, with three
small Docker files at the repo root for containerized runs:

```text
Dockerfile                       One image (Python + deps + market_maker/), used
                                  by every docker-compose service below
docker-compose.yml                 session-init / btc-demo / usdt-demo / dashboard
docker-compose.validation.yml        credential-empty override for audits (usage.md §6.6)
.dockerignore                       Excludes .env, results/, .git, tests/, etc.

market_maker/
  engine.py             SimpleMarketMaker: the run() loop and engine glue
  config.py              MMConfig dataclass; TRADING_PROFILES (per-pair
                          config incl. demo_inv_initial); RESULTS_DIR /
                          demo_csv_path; loads secrets from .env
  safety.py               Demo/Real mode guards (TradingMode, DemoSafetyError)
  session.py               Shared stack-session file + WorkerLease (Docker)
  paper.py                  Canonical Demo paper-order ledger: open-first
                             lifecycle, partial fills, locks, fee scopes,
                             cancellation reasons/context, archive rotation
  result_store.py            DemoResultStore: bounded SQLite round history
                              behind the CSV export dashboard.py reads
  no_quote.py                 NoQuoteMonitor: sustained no-quote episode
                               tracking/alerting, no strategy change
  telegram_lifecycle.py        PaperTelegramReporter: round-summary-threaded
                                order opened/partial/filled/cancelled messages
  portfolio.py            Nobitex_portfolio: wallet/portfolio valuation
  order_status.py         OrderStatusChecker, OrderCanceller (raw Nobitex calls)
  run.py                  Entry point: --algorithm / --exchange / --profile /
                           --mode / --continuous; installs shutdown signal handlers
  dashboard.py             Dash history dashboard, reads SQLite (falls back to CSV)
  strategy/
    base.py                Strategy interface (decide_order contract)
    algo1.py                Algorithm 1's quoting logic (pure, no I/O)
    algo2.py                Algorithm 2 stub (raises NotImplementedError)
  exchange/
    base.py                 ExchangeClient interface
    nobitex.py                NobitexExchangeClient (Nobitex HTTP calls)
    demo.py                    DemoExchangeClient (real data, simulated fills
                                driven by the canonical paper.py ledger)
  research/
    kappa_estimation.ipynb    Estimates Algorithm 2's kappa parameter
    sigma_estimation.ipynb     Estimates Algorithm 2's sigma parameter
    algo2.ipynb                 Empty -- was meant to hold Algorithm 2's implementation
  tests/                   pytest suite (171 deterministic tests; run from repo root)
  results/                 stack_session.json, *_track_demo.sqlite3 (canonical),
                            *_track_demo.csv (bounded export), paper_<session>_<symbol>.json
                            (hot ledger) + *_archive*.jsonl (rotated terminal orders),
                            paper_fee_scopes.json (cumulative fees) --
                            gitignored runtime state; directory itself tracked via .gitkeep
  .env.example              Template for required environment variables
  .env                      Real secrets -- gitignored, never committed
```

No other top-level code directory exists — there used to be a separate `algo2_/` folder holding only notebooks; it's gone, and its contents now live in `market_maker/research/` above, renamed to consistent `snake_case` (`Alg2.ipynb` → `algo2.ipynb`, `Kapa Estimation.ipynb` → `kappa_estimation.ipynb`, fixing a typo in the filename along the way; `sigma_estimation.ipynb` was already correctly named).

---

# Tracked vs. local-only files

Everything under `market_maker/` and this README are committed and visible to the whole team (and anyone with repo access). These are deliberately gitignored and never pushed:

| Path | What it is | Why it's local-only |
|---|---|---|
| `market_maker/.env` | Real Nobitex token, MySQL credentials, optional Telegram bot token/chat id | Secrets — `market_maker/.env.example` is the committed template |
| `market_maker/results/*.csv` | Every `run.py` session's logged output (real and demo, both pairs) | Runtime data, machine-specific, not a project deliverable — the `results/` directory itself is tracked (via `.gitkeep`) so it always exists |
| `market_maker/results/*.sqlite3(-wal/-shm)` | Canonical bounded Demo round history behind the CSV export (usage.md §6.4) | Local runtime database, machine-specific |
| `market_maker/results/paper_*.json`, `paper_*_archive*.jsonl` | Canonical Demo paper-order ledger, cumulative fee scopes, and rotated terminal-order archives | Local runtime state, regenerated per machine, not meaningful to share |
| `docs/reports/` | This author's own working notes on the refactor (audit/fix reports, decisions, deferred items) | Personal analysis notes, not a project deliverable |

If you're missing `market_maker/.env`, copy `market_maker/.env.example` and fill in real values — see **[usage.md](usage.md)** for full setup, running, and testing instructions. `reports/` isn't needed to build, test, or run anything — it exists only as a paper trail for this refactor.

---

# Setup, running, and testing

Full details — environment setup, every way to run the bot (which modes touch a real account/real money and which don't), and how to run the test suite — are in **[usage.md](usage.md)**, kept as its own document so this README stays focused on what the project *is* rather than how to operate it.

Quick summary: `run.py --profile btc|usdt` places **real orders on Nobitex with real money** and needs a live MySQL server. `--mode demo` trades against the same real market data with a deterministic simulated paper-order lifecycle instead (open-first, partial fills, queue/stale/shutdown cancellation) — no real orders, no MySQL required (see usage.md §1b). The test suite (`python3 -m pytest -q` from the repo root) needs neither a real account, MySQL, nor network/Telegram access — all 171 tests are deterministic (mocks and replay fixtures).

A comprehensive, live-updating history dashboard (`python dashboard.py`) covers wealth, inventory, cash, depth levels, spreads, and no-quote episode state for every real/demo × BTC/USDT combination — see usage.md §3. A separate, authenticated admin control page (`python admin_app.py`, Demo-only, no Real Mode surface) adds worker/session/config/backup control on top of that read-only view — see usage.md §3 and `docs/deployment/ADMIN_PAGE.md`.

`docker compose up -d --build --wait` (repo root) runs a shared session initializer, both pairs in continuous demo mode, and the dashboard, sharing one results volume — see usage.md §6.

---

# Known limitations / next steps

* **`config.py` mixes strategy and exchange config** in one `MMConfig` dataclass. Splitting it (e.g. `StrategyConfig` + `ExchangeConfig`) was deferred to keep each refactor step small; do it before adding a second exchange, since exchange-specific fields (`url_send_order`, ...) would otherwise leak into every strategy's config.
* **Persistence (`save_to_db`, the MySQL connection in `__init__`) is not behind an interface** — `SimpleMarketMaker` still requires a live MySQL server to be constructed at all. Extracting a `TradeLogStore` interface (mirroring `ExchangeClient`) is the natural next seam.
* **`portfolio.py`'s `Nobitex_portfolio` is not behind `ExchangeClient`** — `run.py`'s `--exchange` flag today only changes order execution/quotes, not portfolio valuation.
* **Git history contains the original hardcoded secrets** (the Nobitex token and MySQL password, from commits predating the `.env` migration). Moving them to `.env` stops new exposure but doesn't erase history — rotating both credentials is recommended and is a team/repo-owner decision.
* **Demo mode's fills are still an approximation, not a matching engine** — `--mode demo` now runs a deterministic open-first paper-order lifecycle (level-1 delay/fraction, price touch, explicit trade evidence, optional queue-ahead, and partial fills) against real market data, rather than instant full fills. It does not model Nobitex's real slippage or true queue priority. Transaction cost *is* simulated realistically from `commission_perc`, tracked per-symbol and combined across symbols, and reported via optional Telegram notifications. See usage.md §1b/§1.7/§6.8.
* **Algorithm 2 has no real implementation**, only a stub and this README's math — see [Algorithm 2](#algorithm-2-avellaneda-stoikov-model).

---

# Extending

**Add a new algorithm:** implement `Strategy` (`decide_order`) in `market_maker/strategy/`, add it to `run.py`'s `STRATEGIES` dict. No engine or exchange changes needed.

**Add a new exchange:** implement `ExchangeClient` (`market_maker/exchange/base.py`) in `market_maker/exchange/`, add it to `run.py`'s `EXCHANGES` dict. No engine or strategy changes needed.

**Add a new currency pair:** add an entry to `config.py`'s `TRADING_PROFILES` dict with the pair's `MMConfig` values (including its own `demo_inv_initial`). No new files, and `run.py`/`dashboard.py` pick it up automatically.

---

# Example workflow

A typical experiment follows this workflow:

```text
1. Load cryptocurrency order book data.
2. Initialize the portfolio and configuration.
3. For each order book snapshot:
       a. Compute bid and ask quotes using Algorithm 1 or Algorithm 2.
       b. Submit or simulate limit orders.
       c. Track order status.
       d. Update portfolio inventory and cash balance.
       e. Compute PnL and performance metrics.
4. Compare the performance of different strategies.
```

---

# Research purpose

This project is designed for experimentation with cryptocurrency market-making strategies.

Possible experiments include:

* comparing Algorithm 1 with Avellaneda-Stoikov (once implemented)
* studying how inventory limits affect performance
* testing different depth caps
* evaluating the effect of tick size
* measuring PnL and inventory stability
* studying quote aggressiveness under different market conditions
* adding empirical execution-probability models
* testing the strategy on different crypto assets

---

# Disclaimer

This project is for research and educational purposes only.

It is not financial advice and should not be used for live trading without careful testing, validation, and risk management.
