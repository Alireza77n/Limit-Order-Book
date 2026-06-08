# Crypto Market-Making Project

## Project info

This repository contains implementations and experiments focused on **limit order book (LOB) dynamics** in cryptocurrency markets.

The project currently includes:

* **Algorithm 1:** a custom depth-aware, inventory-skewed quoting strategy
* **Algorithm 2:** an implementation of the Avellaneda–Stoikov optimal market-making model

The goal is to simulate, backtest, and compare different market-making approaches on cryptocurrency order book data.

---

## About the market-making logic

Market making consists of continuously placing bid and ask limit orders around the market price. A market maker earns the spread when orders are filled, but also faces several risks, including:

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

[
\text{inventory}
\rightarrow
\text{inventory index}
\rightarrow
\text{candidate bid/ask levels}
\rightarrow
\text{spread feasibility check}
\rightarrow
\text{final bid/ask quotes}
]

The final quotes are placed **one tick inside** the selected order book levels.

---

## Order book notation

Let the bid side of the order book be

[
\mathcal{B} = {B_0, B_1, \dots, B_D},
]

where

[
B_0 \ge B_1 \ge \dots \ge B_D.
]

Here, (B_0) is the best bid, and deeper bid levels have lower prices.

Similarly, let the ask side be

[
\mathcal{A} = {A_0, A_1, \dots, A_D},
]

where

[
A_0 \le A_1 \le \dots \le A_D.
]

Here, (A_0) is the best ask, and deeper ask levels have higher prices.

The maximum usable depth is denoted by

[
D.
]

The exchange tick size is denoted by

[
\epsilon.
]

The mid-price is

[
m = \frac{B_0 + A_0}{2}.
]

---

## Step 1: Inventory normalization

Let the current inventory be

[
q.
]

The strategy assumes an allowed inventory range:

[
q_{\min} \le q \le q_{\max}.
]

The current inventory is mapped into a discrete inventory imbalance index:

[
d_q =
\operatorname{int}
\left(
10
\frac{q-q_{\min}}{q_{\max}-q_{\min}}
\right)
-5.
]

This creates an integer-valued inventory signal.

Approximately:

* (d_q < 0): inventory is low or negative
* (d_q \approx 0): inventory is close to neutral
* (d_q > 0): inventory is high or positive

This index is then used to skew the bid and ask quote levels.

---

## Step 2: Inventory-dependent level-pair generation

The algorithm generates a sequence of candidate bid/ask level pairs:

[
\mathcal{P}(d_q,D)
==================

\left[
(b_1,a_1),
(b_2,a_2),
\dots,
(b_K,a_K)
\right].
]

Each pair ((b_k,a_k)) represents a candidate quote location:

* (b_k): candidate bid level
* (a_k): candidate ask level

The function `_leg_pairs(inv_d, depth_max_local)` creates these candidate pairs.

The exact sequence depends on the inventory imbalance index (d_q). The purpose is to adjust quote aggressiveness according to inventory.

If inventory is high, the algorithm should generally:

* quote less aggressively on the bid side
* quote more aggressively on the ask side

This discourages further buying and encourages selling.

If inventory is low or negative, the algorithm should generally:

* quote more aggressively on the bid side
* quote less aggressively on the ask side

This encourages buying and discourages selling.

If inventory is neutral, the algorithm tends to quote more symmetrically.

---

## Step 3: Spread-feasibility condition

For each candidate pair ((b_k,a_k)), the algorithm checks whether the selected bid and ask levels satisfy a spread condition.

The final bid quote will be placed one tick above the selected bid level:

[
p_b = B_{b_k}+\epsilon.
]

The final ask quote will be placed one tick below the selected ask level:

[
p_a = A_{a_k}-\epsilon.
]

To avoid crossed or invalid quotes, the algorithm requires:

[
B_{b_k}+\epsilon < A_{a_k}-\epsilon.
]

Equivalently,

[
A_{a_k}-B_{b_k} > 2\epsilon.
]

This condition ensures that after moving both quotes one tick inward, the bid quote still remains below the ask quote.

In the implementation, this check is handled by:

```python
self._spread_ok(ob, bidx, aidx)
```

The algorithm scans the candidate level pairs sequentially. It keeps updating the selected bid and ask levels while the spread condition remains valid. Once the condition fails, the search stops.

Therefore, the selected pair is the last feasible pair before the first infeasible one.

Mathematically:

[
(b^\star,a^\star)
=================

\text{last feasible pair in }
\mathcal{P}(d_q,D).
]

The selected levels are then clipped to remain inside the available order book depth:

[
b^\star = \min(\max(b^\star,0),D),
]

[
a^\star = \min(\max(a^\star,0),D).
]

---

## Step 4: Final quote construction

After selecting the final bid and ask levels, the algorithm extracts the corresponding raw order book prices:

[
B_{b^\star}
]

and

[
A_{a^\star}.
]

Then it places quotes one tick inside those levels:

[
\boxed{
p_b = B_{b^\star}+\epsilon
}
]

[
\boxed{
p_a = A_{a^\star}-\epsilon
}
]

This means the bid quote is slightly more aggressive than the selected bid level, and the ask quote is slightly more aggressive than the selected ask level.

---

## Step 5: Reported spreads

The algorithm also reports the distance of each quote from the mid-price.

The bid-side spread is

[
s_b = m - p_b.
]

Using the final bid quote,

[
s_b = m - \left(B_{b^\star}+\epsilon\right).
]

The ask-side spread is

[
s_a = p_a - m.
]

Using the final ask quote,

[
s_a = \left(A_{a^\star}-\epsilon\right) - m.
]

Therefore,

[
\boxed{
s_b = m - B_{b^\star} - \epsilon
}
]

and

[
\boxed{
s_a = A_{a^\star} - \epsilon - m.
}
]

---

## Algorithm 1 summary

The full algorithm can be summarized as:

[
q
\longrightarrow
d_q
\longrightarrow
\mathcal{P}(d_q,D)
\longrightarrow
(b^\star,a^\star)
\longrightarrow
(p_b,p_a).
]

More explicitly:

[
d_q =
\operatorname{int}
\left(
10
\frac{q-q_{\min}}{q_{\max}-q_{\min}}
\right)
-5,
]

[
(b^\star,a^\star)
=================

\text{last feasible candidate pair},
]

subject to

[
B_{b^\star}+\epsilon < A_{a^\star}-\epsilon.
]

The final quotes are

[
p_b = B_{b^\star}+\epsilon,
]

[
p_a = A_{a^\star}-\epsilon.
]

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

* The mid-price is modeled as a stochastic process.
* Order arrivals depend on quote distance from the mid-price.
* The market maker chooses bid and ask quotes to maximize expected utility.
* The utility function balances expected profit against inventory risk.

The model produces an optimal reservation price and optimal bid/ask spreads.

Compared with Algorithm 1:

* Algorithm 1 is deterministic and order-book based.
* Avellaneda–Stoikov is stochastic and model-based.
* Algorithm 1 is easier to implement and interpret.
* Avellaneda–Stoikov provides a stronger theoretical benchmark.

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

```sh
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
