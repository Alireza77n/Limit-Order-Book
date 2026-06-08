# Crypto Market-Making Project

## Project info

This repository contains implementations and experiments focused on **limit order book (LOB) dynamics** in cryptocurrency markets.

The project currently includes:

* **Algorithm 1:** a custom depth-aware, inventory-skewed quoting strategy
* **Algorithm 2:** an implementation of the Avellaneda–Stoikov optimal market-making model

The goal is to simulate, backtest, and compare different market-making approaches on cryptocurrency order book data.

---

## About the market-making logic

Market making consists of continuously placing bid and ask limit orders around the market price. A market maker attempts to earn the bid-ask spread, but also faces several risks, including:

* **Inventory risk:** accumulating too much long or short inventory
* **Adverse selection risk:** being filled just before the market moves against the quote
* **Execution risk:** quotes may not be filled
* **Order book risk:** available liquidity changes across depth levels
* **Fee and tick-size constraints:** quoted prices must remain valid and economically meaningful

This project studies two different approaches:

1. A **deterministic depth-aware strategy**, called Algorithm 1
2. A **model-based optimal strategy**, based on the Avellaneda–Stoikov framework

Algorithm 1 is designed as a practical baseline that directly uses order book depth and inventory information. Algorithm 2 provides a theoretical benchmark based on stochastic control.

---

# Algorithm 1: Depth-Aware Inventory-Skewed Quoting Strategy

## Main idea

Algorithm 1 is a rule-based market-making strategy.

Instead of always quoting at the best bid and best ask, the algorithm looks deeper into the order book and selects bid and ask levels according to the current inventory.

The strategy follows the logic:

```text
inventory
    -> inventory index
    -> candidate bid/ask levels
    -> spread feasibility check
    -> final bid/ask quotes
```

The final quotes are placed **one tick inside** the selected order book levels.

---

## Order book notation

Let the bid side of the order book be:

```math
\mathcal{B} = \{B_0, B_1, \dots, B_D\}
```

where:

```math
B_0 \geq B_1 \geq \dots \geq B_D
```

Here, `B_0` is the best bid, and deeper bid levels have lower prices.

Similarly, let the ask side of the order book be:

```math
\mathcal{A} = \{A_0, A_1, \dots, A_D\}
```

where:

```math
A_0 \leq A_1 \leq \dots \leq A_D
```

Here, `A_0` is the best ask, and deeper ask levels have higher prices.

The maximum usable depth is:

```math
D
```

The exchange tick size is:

```math
\epsilon
```

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

The strategy assumes an allowed inventory range:

```math
q_{\min} \leq q \leq q_{\max}
```

The current inventory is mapped into a discrete inventory imbalance index:

```math
d_q =
\operatorname{int}
\left(
10
\frac{q - q_{\min}}{q_{\max} - q_{\min}}
\right)
- 5
```

This creates an integer-valued inventory signal.

Approximately:

* `d_q < 0`: inventory is low or negative
* `d_q ≈ 0`: inventory is close to neutral
* `d_q > 0`: inventory is high or positive

This index is then used to skew the bid and ask quote levels.

---

## Step 2: Inventory-dependent level-pair generation

The algorithm generates a sequence of candidate bid/ask level pairs:

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

Each pair `(b_k, a_k)` represents a candidate quote location:

* `b_k`: candidate bid level
* `a_k`: candidate ask level

The function below creates these candidate pairs:

```python
pairs = self._leg_pairs(inv_d, depth_max_local)
```

The exact sequence depends on the inventory imbalance index `d_q`.

The purpose is to adjust quote aggressiveness according to inventory.

If inventory is high, the algorithm generally tries to:

* quote less aggressively on the bid side
* quote more aggressively on the ask side

This discourages further buying and encourages selling.

If inventory is low or negative, the algorithm generally tries to:

* quote more aggressively on the bid side
* quote less aggressively on the ask side

This encourages buying and discourages selling.

If inventory is neutral, the algorithm tends to quote more symmetrically.

---

## Step 3: Spread-feasibility condition

For each candidate pair `(b_k, a_k)`, the algorithm checks whether the selected bid and ask levels satisfy a spread condition.

The final bid quote will be placed one tick above the selected bid level:

```math
p_b = B_{b_k} + \epsilon
```

The final ask quote will be placed one tick below the selected ask level:

```math
p_a = A_{a_k} - \epsilon
```

To avoid crossed or invalid quotes, the algorithm requires:

```math
B_{b_k} + \epsilon < A_{a_k} - \epsilon
```

Equivalently:

```math
A_{a_k} - B_{b_k} > 2\epsilon
```

This condition ensures that, after moving both quotes one tick inward, the bid quote still remains below the ask quote.

In the implementation, this check is handled by:

```python
self._spread_ok(ob, bidx, aidx)
```

The algorithm scans the candidate level pairs sequentially. It keeps updating the selected bid and ask levels while the spread condition remains valid. Once the condition fails, the search stops.

Therefore, the selected pair is the last feasible pair before the first infeasible one.

```math
(b^\star, a^\star)
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

This means the bid quote is slightly more aggressive than the selected bid level, and the ask quote is slightly more aggressive than the selected ask level.

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
q -> d_q -> P(d_q,D) -> (b*,a*) -> (p_b,p_a)
```

More explicitly, the inventory index is:

```math
d_q =
\operatorname{int}
\left(
10
\frac{q - q_{\min}}{q_{\max} - q_{\min}}
\right)
- 5
```

The selected level pair is:

```math
(b^\star,a^\star)
=
\text{last feasible candidate pair}
```

subject to:

```math
B_{b^\star} + \epsilon < A_{a^\star} - \epsilon
```

The final bid and ask quotes are:

```math
p_b = B_{b^\star} + \epsilon
```

```math
p_a = A_{a^\star} - \epsilon
```

---

## Algorithm 1 pseudocode

```text
Input:
    order book ob
    current inventory q
    inventory limits q_min, q_max
    tick size epsilon
    maximum usable depth D

Step 1:
    Compute inventory index:

    d_q = int(10 * (q - q_min) / (q_max - q_min)) - 5

Step 2:
    Generate candidate level pairs:

    pairs = P(d_q, D)

Step 3:
    Initialize:

    bid_level = D
    ask_level = D

Step 4:
    For each candidate pair (b_k, a_k):

        If spread condition is valid:

            B_{b_k} + epsilon < A_{a_k} - epsilon

        then:

            bid_level = b_k
            ask_level = a_k

        else:

            stop the search

Step 5:
    Clip selected levels:

    bid_level = min(max(bid_level, 0), D)
    ask_level = min(max(ask_level, 0), D)

Step 6:
    Construct final quotes:

    bid_price = B_{bid_level} + epsilon
    ask_price = A_{ask_level} - epsilon

Step 7:
    Compute mid-price:

    mid_price = (B_0 + A_0) / 2

Step 8:
    Compute reported spreads:

    bid_spread = mid_price - bid_price
    ask_spread = ask_price - mid_price

Output:
    bid_price
    ask_price
    bid_level
    ask_level
    bid_spread
    ask_spread
```

---

## Why Algorithm 1 is useful

Algorithm 1 has several practical advantages:

* It is **fast and deterministic**
* It directly uses **order book depth**
* It does not require calibration of a stochastic price model
* It adjusts quotes according to **current inventory**
* It avoids invalid or crossed quotes through a spread-feasibility check
* It provides a simple baseline for comparison with more advanced market-making models

Unlike Avellaneda–Stoikov, Algorithm 1 does not solve an optimization problem. It is a heuristic strategy that converts inventory and order book information into practical bid and ask quotes.

---

## Important implementation note

In the current implementation, the returned execution probabilities are placeholders:

```python
"bid exec prob": 1.0,
"ask exec prob": 1.0
```

This means Algorithm 1 currently does not estimate execution probability using an arrival-intensity model.

Future versions could replace these placeholders with empirical fill probabilities based on:

* quote distance from the mid-price
* order book imbalance
* recent trade direction
* market volatility
* historical fill statistics

---

# Algorithm 2: Avellaneda–Stoikov Model

This repository also includes an implementation of the Avellaneda–Stoikov optimal market-making model.

In the Avellaneda–Stoikov framework:

* The mid-price is modeled as a stochastic process
* Order arrivals depend on quote distance from the mid-price
* The market maker chooses bid and ask quotes to maximize expected utility
* The utility function balances expected profit against inventory risk

The model produces an optimal reservation price and optimal bid/ask spreads.

Compared with Algorithm 1:

* Algorithm 1 is deterministic and order-book based
* Avellaneda–Stoikov is stochastic and model-based
* Algorithm 1 is easier to implement and interpret
* Avellaneda–Stoikov provides a stronger theoretical benchmark

---

# Project structure

The main files in this repository are:

* **Algo1.py** — implementation of the custom depth-aware inventory-skewed quoting strategy
* **Config.py** — configuration file containing inventory limits, tick size, depth caps, and other parameters
* **demo_btc.py** — runs the market-making engine using BTC order book data
* **demo_usdt.py** — runs the market-making engine using USDT order book data
* **live_plot.py** — displays real-time portfolio and PnL visualization
* **Order_Status.py** — tracks filled and pending orders
* **Portfolio.py** — manages balance, inventory, and PnL calculations

---

# How to run the project

You can run market-making simulations directly using the demo scripts.

For the BTC demo:

```sh
python demo_btc.py
```

For the USDT demo:

```sh
python demo_usdt.py
```

Before running the demos, check the configuration file:

```text
Config.py
```

Important parameters include:

* inventory minimum and maximum
* tick size
* maximum order book depth
* trading symbol
* initial balance
* fee assumptions
* strategy-specific parameters

---

# Output of Algorithm 1

For each order book snapshot, Algorithm 1 returns a dictionary containing:

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
* **bid spread:** distance between the mid-price and the bid quote
* **ask spread:** distance between the ask quote and the mid-price
* **lastTradePrice:** most recent traded price

---

# Research purpose

This project is designed for experimentation with crypto market-making strategies.

Possible experiments include:

* comparing Algorithm 1 with Avellaneda–Stoikov
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

It is not financial advice, and it should not be used for live trading without careful testing, validation, and risk management.
