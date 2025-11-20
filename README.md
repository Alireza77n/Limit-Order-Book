# Welcome to the Crypto Market-Making Project

## Project info

This repository contains implementations and experiments focused on **limit order book (LOB) dynamics** in cryptocurrency markets.

It currently includes:
- A **custom depth-aware quoting strategy** (Algorithm 1)
- An implementation of the **Avellaneda–Stoikov optimal market-making model**

You can use this project to simulate, backtest, and compare different market-making approaches on crypto LOB data.

## About the market-making logic

The core idea of this project is to study how a market maker should place limit orders in a live order book while:
- Managing **inventory risk**
- Accounting for **exchange fees**
- Reacting to **order book depth** and **short-term price movements**

The project provides:
- A **heuristic, deterministic strategy** (Algorithm 1) based on order book depth and inventory
- A **model-based strategy** using the Avellaneda–Stoikov framework

These two approaches can be compared side by side in simulations.

## Algorithm 1: Depth-Aware Quoting Strategy

**What it does**

Algorithm 1 is a simple, rule-based market-making strategy that:
- Looks at multiple **depth levels** in the order book (not only best bid/ask)
- Selects levels where quoting is potentially **profitable after fees**
- Adjusts how aggressive or conservative the quotes are based on the **current inventory**

Instead of always quoting at the top of the book, the algorithm:
1. Checks how deep the book is (maximum usable depth).
2. Maps the current inventory to a small **shift in levels**:
   - Positive inventory → more conservative on the bid side.
   - Negative inventory → more conservative on the ask side.
   - Neutral inventory → symmetric quoting.
3. Forms candidate `(bid_level, ask_level)` pairs and filters them by a **spread condition** (to avoid unprofitable or unrealistic quotes).
4. Once valid levels are found, it places quotes **one tick inside** those levels to improve fill probability while keeping a positive expected edge.

**Why this is useful**

This algorithm:
- Is **fast and deterministic**
- Uses **order book depth** instead of only the top of book
- Naturally pushes the inventory back toward a target range
- Provides a **practical baseline** to compare with more advanced models like Avellaneda–Stoikov

## Algorithm 2: Avellaneda–Stoikov model

This repository also includes an implementation of the **Avellaneda–Stoikov optimal market-making model**, adapted to cryptocurrency order books.

In this framework:
- The mid-price is modeled as a stochastic process.
- Order arrivals depend on how far quotes are from the mid-price.
- The market maker chooses bid/ask quotes to maximize a utility function that trades off:
  - **Expected profit**
  - **Inventory risk**

This gives a **theoretical benchmark** to compare against Algorithm 1.

## Project structure

The main files in this repository are:

- **Algo1.py** — Implementation of the custom depth-aware quoting strategy  
- **Config.py** — Configuration (inventory limits, depth caps, tick size, etc.)  
- **demo_btc.py** — Runs the market-making engine using BTC order book data  
- **demo_usdt.py** — Runs the market-making engine using USDT order book data  
- **live_plot.py** — Displays real-time portfolio and PnL visualization  
- **Order_Status.py** — Tracks filled and pending orders  
- **Portfolio.py** — Manages balance, inventory, and PnL calculations  

## How to run the project

You can execute market-making simulations directly using the demo scripts:

```sh
python demo_btc.py        # Runs the BTC market-making demo
python demo_usdt.py       # Runs the USDT market-making demo
