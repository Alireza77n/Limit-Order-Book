# Crypto Market-Making Project — branch MM_algo2

> **This branch runs MM_algo2**, an order-book imbalance and microprice market maker for the Nobitex
> exchange. Everything below describes MM_algo2. Other branches:
> [`MM_algo1v2`](https://github.com/Alireza77n/Limit-Order-Book/tree/MM_algo1v2) holds Algorithm 1
> version 2 with its own documentation, and `MM_algo1v1` the old version 1.

## Project info

This repository is a market-making bot for the Nobitex exchange, trading **BTC/IRT** and **USDT/IRT**.
Every round, a worker reads the order book, places one limit buy below the market and one limit sell above
it, and earns the difference when both fill, after the exchange fee on each. It runs in two modes:

* **demo** (paper trading): real order books, simulated fills in a local ledger;
* **live** (the forward test): real orders with real money.

A results dashboard and an authenticated admin panel go with it.

**MM_algo2** decides the two prices. Once per round it:

* measures the **imbalance** of the resting volumes in the top levels of the book;
* centres its quotes on the **microprice**, the mid pulled toward the thinner side;
* **skews** that centre against the inventory, and **leans** it with the imbalance once the lean $\beta$ is
  estimated (it is 0 for now);
* quotes **one symmetric spread**, the smallest that clears the fee bar.

Its code is `market_maker/strategy/algo3.py`.

| | MM_algo2 |
|---|---|
| Status | Implemented and tested. Running on the **BTC demo** since 17 October 2026, 11:15 UTC. The saved **live BTC** settings select it; the live BTC worker is stopped. |
| Code | `market_maker/strategy/algo3.py` (`Algo3Strategy`), shared arithmetic in `strategy/common.py` |
| Selected by | the `algorithm` setting = `algo3`, per pair: admin panel → **Algorithm** (or the first field of each config page), or `run.py --algorithm algo3` |
| Full specification | [`docs/algorithms/MM_algo2.md`](docs/algorithms/MM_algo2.md): every formula with its proof, three worked examples, behaviour on real books, the calibration of $\beta$, the live settings, the test map |
| The whole system explained | [`docs/guide/MARKET_MAKER_EXPLAINED.md`](docs/guide/MARKET_MAKER_EXPLAINED.md) (also a standalone [HTML page](docs/guide/MARKET_MAKER_EXPLAINED.html)) |

**Why the code says `algo3`.** MM_algo2 is the third algorithm designed for this project (design note
*"Algorithm 3 — Order-Book Imbalance and Microprice Market Maker"*), so its key in the code, the settings and
the recorded rounds is `algo3`. It is shown everywhere else as **MM_algo2**, the name of this branch.
`strategy/algo2.py` is unrelated: an empty placeholder for the Avellaneda–Stoikov model.

**Other algorithms in the code** ([details](#other-algorithms-in-the-code)): **MM_algo1v2** (Algorithm 1
version 2, `strategy/algo1.py`) stays selectable here, so the two algorithms can be compared on the dashboard;
its documentation lives on its own branch. The Avellaneda–Stoikov placeholder (`strategy/algo2.py`) is not
implemented.

---

## Table of contents

- [Limit order book and market-making fundamentals](#limit-order-book-and-market-making-fundamentals) — start here if you're new to the topic
  - [What is a limit order book?](#what-is-a-limit-order-book)
  - [Key order book terms](#key-order-book-terms)
  - [What is market making?](#what-is-market-making)
  - [Core risks a market maker faces](#core-risks-a-market-maker-faces)
- [MM_algo2: order-book imbalance and microprice](#mm_algo2-order-book-imbalance-and-microprice)
  - [Main idea](#main-idea)
  - [Notation](#notation)
  - [Step 1: the book signal](#step-1-the-book-signal)
  - [Step 2: the microprice](#step-2-the-microprice)
  - [Step 3: the quote centre](#step-3-the-quote-centre)
  - [Step 4: the half-spread from the fee bar](#step-4-the-half-spread-from-the-fee-bar)
  - [Step 5: prices on the tick, never crossing](#step-5-prices-on-the-tick-never-crossing)
  - [Step 6: keeping a pair that barely moved](#step-6-keeping-a-pair-that-barely-moved)
  - [MM_algo2 summary](#mm_algo2-summary)
  - [Output of MM_algo2](#output-of-mm_algo2)
  - [MM_algo2 in practice: a real book](#mm_algo2-in-practice-a-real-book)
  - [MM_algo2 in practice: a real round of the BTC demo](#mm_algo2-in-practice-a-real-round-of-the-btc-demo)
  - [How MM_algo2 behaves on real books](#how-mm_algo2-behaves-on-real-books)
  - [Why the lean β is still 0](#why-the-lean-β-is-still-0)
- [MM_algo2 pipeline: code trace](#mm_algo2-pipeline-code-trace)
  - [Pipeline overview](#pipeline-overview)
  - [Stage 6 — the strategy, function by function](#stage-6--the-strategy-function-by-function)
  - [Stage 7 — engine-side checks](#stage-7--engine-side-checks)
- [Choosing and comparing algorithms](#choosing-and-comparing-algorithms)
- [Other algorithms in the code](#other-algorithms-in-the-code)
- [Architecture](#architecture)
- [Project structure](#project-structure)
- [Tracked vs. local-only files](#tracked-vs-local-only-files)
- [Setup, running, and testing](#setup-running-and-testing)
- [Known limitations / next steps](#known-limitations--next-steps)
- [Extending](#extending)
- [Research purpose](#research-purpose)
- [Disclaimer](#disclaimer)

---

# Limit order book and market-making fundamentals

This section is a from-scratch primer. If you already know what a bid, an
ask, and a spread are, skip to [MM_algo2](#mm_algo2-order-book-imbalance-and-microprice).

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

These are used throughout this README and the codebase; MM_algo2's
notation below builds directly on them.

| Term | Meaning |
|---|---|
| **Best bid** ($b_0$) | The highest price any buyer is currently willing to pay. |
| **Best ask** ($a_0$) | The lowest price any seller is currently willing to accept. |
| **Spread** | $a_0 - b_0$, the gap between the best bid and the best ask. A tighter spread generally means a more liquid, more competitively traded market. |
| **Mid-price** | $(b_0 + a_0)/2$, a common single-number reference for "the price" of the asset right now, even though no trade necessarily happens exactly there. |
| **Depth / level** | Orders don't all sit at the best price. Level 0 is the best price on a side, level 1 the next price that has orders, and so on. "Depth" is how much volume rests at or near the top of the book. |
| **Tick size** | The smallest allowed price increment: 10 rial on Nobitex's rial pairs. Prices are multiples of it. |
| **Order book imbalance** | The relative amount of resting volume on the bid side against the ask side. Heavy bid-side volume can signal upward pressure (more people want in than out), and vice versa — a signal, not a guarantee. MM_algo2 measures it every round. |
| **Microprice** | The mid shifted toward the side with *less* volume at the best level: if far more waits to buy than to sell, the ask is likely to be taken first, so the "fair" price sits nearer the ask. MM_algo2 centres its quotes on it. |
| **Inventory** | How much of the asset a market maker currently holds, as a result of its own past fills. |

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
  whatever spread it earned getting into it. MM_algo2 manages it twice:
  its quote centre is skewed against the inventory
  ([Step 3](#step-3-the-quote-centre)), and the engine sizes each side by
  the inventory (more selling when full, more buying when empty).
* **Adverse selection risk.** A market maker's quotes are filled
  precisely when someone *wants* to trade against them — and the people
  most eager to trade right now are often the ones who know the price is
  about to move (news, a large incoming order, a faster/better-informed
  trader). Getting filled isn't automatically good news; it can mean
  being picked off just before the market moves against the new
  position. Centring on the microprice, rather than on the mid, is a
  first step against it: the quotes move toward where the price is more
  likely to go next.
* **Execution risk.** The reverse problem: a quote sitting in the book is
  not guaranteed to be filled at all. If the market moves away before
  anyone trades against it, the market maker earns nothing on that quote
  and has to reprice — repeatedly, if the market keeps moving.
  Quoting too conservatively (too far from the touch) reduces adverse
  selection risk but increases execution risk; quoting too aggressively
  does the opposite. There is no setting that eliminates both.
* **Order book / liquidity risk.** Depth is not static — it can thin out
  or disappear during volatile periods, exactly when a market maker most
  needs to unwind inventory. MM_algo2 treats a top of book thinner than a
  set volume as carrying no signal ([Step 1](#step-1-the-book-signal)).
* **Tick-size and fee constraints.** A quote isn't valid or meaningful
  just because it looks profitable on paper — it has to land on an
  allowed tick, and it has to clear the exchange's trading commission
  *and* whatever minimum profit margin the strategy requires, or the
  "profit" is actually a loss once fees are paid. MM_algo2 derives its
  spread from exactly this condition ([Step 4](#step-4-the-half-spread-from-the-fee-bar))
  and proves that rounding to the tick cannot break it
  ([Step 5](#step-5-prices-on-the-tick-never-crossing)).

---

# MM_algo2: order-book imbalance and microprice

> Full specification, with every formula proved, three worked examples, the behaviour on 1,276 real books,
> the calibration of $\beta$, what the live settings would send, and the map from each property to the test
> that pins it: [`docs/algorithms/MM_algo2.md`](docs/algorithms/MM_algo2.md).
> Every number below was produced by running this branch's code on real Nobitex books:
> [`docs/algorithms/data/make_mm_algo2_doc_data.py`](docs/algorithms/data/make_mm_algo2_doc_data.py).

## Main idea

A market maker earns the spread between a buy it fills and a sell it fills. A quoting rule can look only at the
prices in the order book; MM_algo2 also reads the **volumes** resting at them. When much more volume waits to buy
than to sell near the top of the book, the ask side is the one likely to be consumed and the next move of the
price is more likely up. So, once per round, MM_algo2:

1. measures that **imbalance** over the top $k$ levels;
2. starts its quote centre at the **microprice**, the mid pulled toward the side with less volume;
3. **leans** the centre in the direction of the imbalance (by $\beta$, which is 0 until it is estimated) and
   **skews** it against the inventory;
4. quotes **one symmetric half-spread** $h$ around the centre: the smallest that clears the fee bar, plus one tick;
5. rounds the bid down and the ask up to the tick, never crossing the best price on the other side, and
   checks the fee bar again on the prices actually sent;
6. keeps the previous pair while both prices stay within 5 ticks of it.

It never estimates a fill probability. The lean and the skew move the **centre** only, so the ratio of the
two prices, and with it the profit of a completed round trip, depends on $h$ alone. The engine then sizes,
caps and sends the orders.

## Notation

| Symbol | Meaning | Setting (default) |
|---|---|---|
| $b_i, v^b_i$ / $a_i, v^a_i$ | bid / ask price and volume at level $i$; level 0 is the best | — |
| $m = (b_0 + a_0)/2$, $s = a_0 - b_0$ | mid-price, market spread | — |
| $\epsilon$ | tick, 10 rial | — |
| $k$, $\rho$ | levels in the imbalance; weight of level $i$ is $\rho^{\,i}$ | `algo3_depth_k` (3), `algo3_decay_rho` (0.5) |
| $I_{\max}$, $V_{\min}$ | imbalance clip; thin-book threshold $= n_V \times$ `order_size` | `algo3_imbalance_clip` (0.8), `algo3_min_volume_orders` $= n_V$ (20) |
| $\beta$ | lean per unit of imbalance, a fraction of price | `algo3_beta` (0) |
| $q$, $x$ | inventory; its position in the band $[q_{\min}, q_{\max}]$, $0 \le x \le 1$ | `inv_min`, `inv_max` |
| $\sigma_q$ | largest inventory shift of the centre | `skew_perc` (0.002) |
| $f$, $r_{\min}$ | fee per filled order; required net return of a round trip | `commission_perc`, `exp_ret_min` |
| $G = (1+r_{\min})/(1-f)^2 - 1$ | gross bar | — |
| $h$ | half-spread, a fraction of the centre | `algo3_half_spread` (0 = automatic) |
| $n$, $T$ | hysteresis ticks and refresh time | `hysteresis_ticks` (5), `refresh_seconds` (300) |
| $\omega$ | requote band, as a fraction of price | `requote_band_perc` (0.0003 = 0.03 %) |

## Step 1: the book signal

$$
V^b = \sum_{i=0}^{k-1} \rho^{\,i}\, v^b_i,\qquad
V^a = \sum_{i=0}^{k-1} \rho^{\,i}\, v^a_i,\qquad
I_{\text{raw}} = \frac{V^b - V^a}{V^b + V^a} \in [-1, 1].
$$

$I_{\text{raw}} > 0$ means more volume waits to buy than to sell. Two guards turn it into the $I$ that is used:

* **thin book:** if $V^b + V^a < V_{\min}$, then $I = 0$ and the centre starts at the mid (in demo, $V_{\min}$ is
  100 USDT or 0.00124 BTC);
* **clip:** otherwise $I$ is clipped to $\pm I_{\max}$, so one large level, possibly placed only to be pulled,
  cannot push the lean to its maximum.

A book with no usable best price, a crossed top, or fewer than $k$ levels on a side gets no quote.

**While $\beta = 0$, which is the setting in force, $I$ does not move the quotes.** It enters the rule only
through the lean factor $1 + \beta I$. $I$ is still computed, recorded and charted every round, and $V^b + V^a$
still decides the thin-book test. Until $\beta$ is switched on, the book moves MM_algo2's prices through the
best level (the microprice) and the touch (Step 5) only.

## Step 2: the microprice

$$
p_{\text{micro}} = \frac{a_0\, v^b_0 + b_0\, v^a_0}{v^b_0 + v^a_0}
= m + \frac{s}{2}\, I_0,\qquad I_0 = \frac{v^b_0 - v^a_0}{v^b_0 + v^a_0}.
$$

The identity is proved in the specification. Because $\lvert I_0\rvert \le 1$, the microprice always lies
between the best bid and the best ask. When more volume rests at the best bid, it sits nearer the ask.

## Step 3: the quote centre

$$
c = p_{\text{micro}}\,\bigl(1 + \beta I\bigr)\,\bigl(1 - \sigma_q\,(2x - 1)\bigr),
\qquad x = \operatorname{clamp}\!\left(\frac{q - q_{\min}}{q_{\max} - q_{\min}}, 0, 1\right).
$$

A half-full band leaves the centre at the microprice. A full band moves it $\sigma_q$ = 0.2 % down, so selling
becomes more likely; an empty band moves it 0.2 % up. The inventory is the whole holding, including coins
already locked in the bot's open sells.

## Step 4: the half-spread from the fee bar

Buying at $B$ and selling the coins received at $A$ returns $R(B, A) = (1-f)^2 A/B - 1$, since the fee takes
$f$ of what is received on each side. With $B = c(1-h)$ and $A = c(1+h)$:

$$
R(h) = (1-f)^2\,\frac{1+h}{1-h} - 1 \;>\; r_{\min}
\iff \frac{1+h}{1-h} > 1 + G
\iff h > h_{\min} = \frac{G}{2+G}.
$$

$R$ depends on $h$ alone: the centre, and with it the signal and the inventory, cancels out. MM_algo2 uses
$h = \max(h_{\text{cfg}},\ h_{\min} + \epsilon/c)$, one tick of margin above the bar.

| Settings | $f$ | $r_{\min}$ | $h_{\min}$ |
|---|---:|---:|---:|
| demo (both pairs, 17 Oct 2026) | 0.08 % | 0.01 % | 0.0850 % |
| live (both pairs) | 0.25 % | 0.01 % | 0.2553 % |
| profile defaults in `config.py` | 0.10 % | 0.10 % | 0.1500 % |

## Step 5: prices on the tick, never crossing

$$
B = \epsilon \left\lfloor \frac{\min\bigl(c(1-h),\ a_0 - \epsilon\bigr)}{\epsilon} \right\rfloor,\qquad
A = \epsilon \left\lceil \frac{\max\bigl(c(1+h),\ b_0 + \epsilon\bigr)}{\epsilon} \right\rceil .
$$

The bid never reaches the ask side and the ask never reaches the bid side. Both the clip at the touch and the
rounding only *lower* the bid and *raise* the ask, so $A/B \ge (1+h)/(1-h) > 1 + G$: **every pair sent clears
the fee bar.** The code still re-checks the bar on the exact prices sent (`net_return_below_bar`); that check
cannot fail while $h > h_{\min}$. This corrects the design note, which said the clip could make a quote
unprofitable.

## Step 6: keeping a pair that barely moved

The previous pair is sent again while both new prices are within the **requote band** of it, the pair is
younger than 300 s, and it still clears the bar. The band is the wider of 5 ticks (50 rial) and 0.03 % of
the price (`requote_band_perc`): about 790 rial on USDT and 64 million rial on BTC. Its age runs from its
first issue, so after 300 s a new pair is always issued. The engine uses the same band: it skips sending an
order when one of its own already rests within the band of the price, and it cancels a resting order of ours
that the new price has left outside the band, so at most one order rests per side (since 20 October 2026;
before, the band was 50 rial on both pairs and orders piled up for 10 minutes — see
[`MM_algo2.md` §19](docs/algorithms/MM_algo2.md#19-changes-of-20-october-2026-one-order-per-side-shorter-orders-higher-target-lanes)).

## MM_algo2 summary

```text
inputs : the order book {bids, asks} (best first), the inventory q, the settings
if the book has no usable best price, a crossed top or fewer than k levels a side: no quote (reason)
V_b, V_a  = sum over levels i < k of rho^i * volume;      I = (V_b - V_a) / (V_b + V_a)
if V_b + V_a < V_min:  I = 0;  start = mid         else:  I = clip(I, -I_max, I_max);  start = microprice
x = clamp((q - q_min) / (q_max - q_min), 0, 1)
c = start * (1 + beta * I) * (1 - skew * (2x - 1))
h = max(algo3_half_spread, G / (2 + G) + tick / c)
B = floor_tick(min(c(1 - h), a0 - tick));   A = ceil_tick(max(c(1 + h), b0 + tick))
if (1 - f)^2 * A / B - 1 <= r_min: no quote                      (cannot happen while h > h_min)
if both prices are within 5 ticks of the last pair and it is under 300 s old: send the last pair again
return {bid B, ask A, levels, returns, centre, imbalance, microprice, reason}
```

## Output of MM_algo2

The quote carries `bid`, `ask`, `bid level`, `ask level`, `mid price`, `centre`, `inventory position`, `gross
return`, `net return`, `relative quoted spread`, `min required return`, `bid spread`, `ask spread`, `bid
distance`, `ask distance`, `imbalance` and `microprice`. Its `quote mode` is always `pair`, and its `selection
reason` is `microprice_pair` or `microprice_pair_kept`. The fill-probability and expected-value keys are
empty. A round with no quote records one of five reasons: `invalid_or_single_level_order_book`,
`book_shallower_than_depth_k`, `invalid_centre`, `half_spread_not_attainable`, `net_return_below_bar`. Every
round row carries `Strategy` = `algo3`, `Imbalance` and `Microprice`. Every round's whole evaluation goes to
the diagnostics file; that file keeps its historical name `<pair>irt_algo1_diagnostics.jsonl` for both
algorithms, and each document says which algorithm wrote it.

## MM_algo2 in practice: a real book

A USDT/IRT book captured from the server on 17 October 2026 at 11:40:34 UTC, with the USDT demo's settings
that day ($f$ = 0.08 %, $r_{\min}$ = 0.01 %, band 0–300 USDT, order size 5 USDT) and its holding of
92.4712 USDT:

| Level | Bid | Bid volume (USDT) | Ask | Ask volume (USDT) |
|---:|---:|---:|---:|---:|
| 0 | 2,633,020 | 384.6 | 2,634,000 | 698.87 |
| 1 | 2,633,010 | 138.45 | 2,634,200 | 1,054.44 |
| 2 | 2,633,000 | 281.9 | 2,634,390 | 47 |

| Step | Value |
|---|---|
| mid; spread | 2,633,510; 980 rial |
| $V^b$; $V^a$ | $384.6 + 0.5 \cdot 138.45 + 0.25 \cdot 281.9$ = 524.3; $698.87 + 0.5 \cdot 1{,}054.44 + 0.25 \cdot 47$ = 1,237.84 |
| $I$ | $(524.3 - 1{,}237.84)/1{,}762.14$ = −0.40493: more volume waiting to sell |
| microprice | 2,633,367.87 = $m + 490 \times I_0$, with $I_0$ = −0.29006 (142.13 rial below the mid) |
| $x$; skew | $92.4712/300$ = 0.30824; $1 - 0.002\,(2 \cdot 0.30824 - 1)$ = 1.00076705 |
| centre | 2,635,387.80, 0.0713 % above the mid: the low inventory outweighs the sell-side book |
| $h$ | 0.0850317 % + one tick (0.0003795 %) = 0.0854112 % |
| raw prices | 2,633,136.88 / 2,637,638.71 |
| **prices sent** | **bid 2,633,130**, 110 rial above the best bid (the new best bid); **ask 2,637,640**, 3,640 rial behind the best ask |
| net return | 0.011069 % > 0.01 % |
| sizes (no open orders) | buy $2 \cdot 5 \cdot (1 - 0.30824)$ = 6.9176 USDT; sell $2 \cdot 5 \cdot 0.30824$ = 3.0824 USDT |

On the same book, a full band ($x = 1$) moves the centre 0.2 % down. The raw ask would cross the best bid, so
the clip lifts it to 2,633,030, and the pair's net return rises to 0.1131 %: the clip widens, it never
narrows.

![The USDT book of 17 October and MM_algo2's prices](docs/algorithms/figures/mm2_example1.png)

## MM_algo2 in practice: a real round of the BTC demo

The BTC demo switched to MM_algo2 on 17 October 2026 at 11:15 UTC. Its round 2761, at 14:58:04 Tehran time, as
recorded:

* **the book:** best bid 213,870,000,000 with 0.04582 BTC, best ask 214,189,998,500 with 0.00088 BTC, a
  spread of 0.1495 %;
* **the signal:** $I_{\text{raw}}$ = 0.93260, clipped to 0.8; the microprice sits 0.0719 % above the mid,
  near the ask, because 52 times more BTC waits at the best bid than at the best ask;
* **the centre:** the inventory 0.0025009 of 0.0037 BTC ($x$ = 0.676) lowers it, to 0.0015 % above the mid;
* **the prices:** with $h$ = 0.0850318 %, **bid 213,851,262,480** and **ask 214,215,254,940**, both behind the
  touch, with a net return of 0.0100000 %;
* **the orders:** a buy of 0.00003018 BTC and a sell of 0.00006459 BTC, sent to the paper ledger.

Rerunning `Algo3Strategy` on the recorded levels and inventory reproduces both prices exactly. So does every
one of the 15 MM_algo2 rounds recorded at the time of writing.

## How MM_algo2 behaves on real books

The strategy was run with the demo settings on 1,276 real books: the 3 October 2026 sample (578 per pair) and
60 books per pair captured on 17 October. The inventory was held at mid-band:

| | USDT, 3 Oct | BTC, 3 Oct | USDT, 17 Oct | BTC, 17 Oct |
|---|---:|---:|---:|---:|
| books quoted | 578 of 578 | 578 of 578 | 60 of 60 | 60 of 60 |
| market spread, median | 0.0294 % | 0.0616 % | 0.0258 % | 0.1495 % |
| microprice offset from the mid, median | 0.0080 % | 0.0130 % | 0.0043 % | 0.0455 % |
| bid / ask distance from the mid, median | 0.0859 % / 0.0853 % | 0.0850 % / 0.0850 % | 0.0861 % / 0.0851 % | 0.0849 % / 0.0852 % |
| bid that improves the best bid | 6.1 % | 18.2 % | 0 % | 40.0 % |
| ask that improves the best ask | 10.9 % | 11.6 % | 0 % | 15.0 % |
| imbalance clipped at ±0.8 | 25.4 % | 22.7 % | 23.3 % | 45.0 % |

MM_algo2 needs at least $2h \approx 0.17\%$ between its prices, while the market spread is usually narrower.
So **its orders usually wait behind the best prices**, and a fill needs the price to come to them. With the
live fee ($2h \approx 0.51\%$) they wait further away. Every quote cleared the bar.

## Why the lean β is still 0

The lean is worth switching on only if the imbalance predicts the move of the mid over an order's lifetime. On
the 3 October sample, regressing the 10-minute forward return on $I$ gives
$\hat\beta = 2.64 \times 10^{-4}$ for USDT ($t$ = 1.10) and $4.87 \times 10^{-4}$ for BTC ($t$ = 0.90). The
$t$-statistics use standard errors that allow for the overlapping windows. The rule requires $t > 3$. The
best case, BTC at 60 s ($t$ = 2.70), would move the centre by about 0.009 %, a tenth of the demo half-spread.
Two hours of books contain only about 11 independent 10-minute windows; days of data are needed.

---

# MM_algo2 pipeline: code trace

## Pipeline overview

```text
run.py main() ── make_cfg(profile, mode) ── strategy/catalog.build_strategy(cfg) ── SimpleMarketMaker.run()
                                                                    │
   every time_step seconds:  (A) settle fills, cancel orders > 10 min│ engine.py, paper.py (demo)
                             (B) exchange.get_orderbook               │ exchange/nobitex.py
                             (C) demo: re-read the admin settings;     │ demo_config.py
                                 switch strategy if `algorithm` changed│ engine._sync_strategy_with_config
                             (D) strategy.decide_order(book, q)  ◄────┼── strategy/algo3.py (MM_algo2)
                                 + diagnostics document               │ <pair>irt_algo1_diagnostics.jsonl
                             (E) sizes from the inventory band        │ _inventory_sized_order_targets
                             (F) sell leg → send (unless resting / caps)
                             (G) buy leg  → send (unless resting / caps)
                             (H) round row (Strategy, Imbalance, Microprice, ...), Telegram, wait
```

## Stage 6 — the strategy, function by function

| `strategy/algo3.py` (`Algo3Strategy`) | Step | What it returns |
|---|---|---|
| `depth_k()`, `decay_rho()`, `beta()`, `imbalance_clip()`, `min_volume()`, `skew()`, `configured_half_spread()` | settings | the settings, clamped to their valid ranges ($V_{\min}$ in base units) |
| `minimum_half_spread()` | 4 | $h_{\min} = G/(2+G)$ |
| `imbalance(bids, asks)` | 1 | $(V^b, V^a, I_{\text{raw}})$ |
| `microprice(bids, asks)` | 2 | $p_{\text{micro}}$ (the mid if level 0 is empty) |
| `evaluate(ob, q)` | 1–5 | `Algo3Evaluation`: every intermediate value, or the no-quote reason; pure |
| `_apply_hysteresis(B, A)` | 6 | the pair to send, and whether it is the kept one |
| `decide_order(ob, q)` | 1–6 | the quote, or `None` (which also forgets the remembered pair) |
| `diagnostics(ob, q, round_no=, session_id=)` | — | the JSON document of the same decision; the memory is left alone |
| `no_quote_reason(ob, q)` | — | `quote_available` or the reason |
| `reset_memory()` | 6 | forgets the remembered pair |

Inherited from `QuoteArithmetic` (`strategy/common.py`, shared with MM_algo1v2 so that "profitable" means the
same in both): `tick_size()`, `mid_price()`, `inventory_position()`, `gross_return()`, `net_return()`,
`min_required_return()`, `gross_bar()`, `relative_quoted_spread()`, `depth_cap()`, and the rounding helpers.

## Stage 7 — engine-side checks

The engine sizes the two orders by the inventory band: with no open orders, buy $= 2S(1-x)$ and sell $= 2Sx$.
Resting orders count as if already filled. It then cancels a resting order of ours that the new price has
left outside the requote band (one order per side), skips a side when one of its own orders already rests
within the band of the price, when the trend pause blocks it (0.3 % over 10 minutes by default), when the size is below $0.1S$
or not affordable, when the buy would take the inventory above `inv_max`, and, in live, when the 25 %
exposure guards would be exceeded. MM_algo2 always returns both prices; at the band edges it is the sizing
that leaves one side out. Orders are cancelled after 3 minutes. Details: [`PROJECT_GUIDE.md` §7](PROJECT_GUIDE.md#7-how-order-sizes-are-decided) and
[`docs/algorithms/MM_algo2.md` §6](docs/algorithms/MM_algo2.md#6-what-the-engine-does-with-the-quote).

---

# Choosing and comparing algorithms

**Names.** The dashboard and the admin panel name each algorithm after the git branch it was built on:
**MM_algo2** (this branch's algorithm; key `algo3`) and **MM_algo1v2** (Algorithm 1 version 2; key `algo1`).

**In the admin panel** (`https://37.32.31.162/admin/`). Open **Algorithm** (second item of the menu; the
same drop-down is on *Overview* and *Live Forward Test*) and choose "MM_algo2 — …" or "MM_algo1v2 — …". Two
tables follow, each with its active session ID next to it:

* **1. Demo (paper trading):** per pair, the worker, the algorithm it runs now and **Activate**. The worker
  switches at its next round and keeps the same session, ledger and inventory; the new strategy starts with
  no memory. Both pairs share one demo session, so on the dashboard a session that switched lists both
  algorithms (`MM_algo1v2 → MM_algo2 · <session id>`). **Start a new demo session** (same page) gives the
  algorithm a session of its own: running demo workers restart in it, and each pair's paper account starts
  again from its starting balances.
* **2. Live forward test (real money):** per pair, the worker, the algorithm it runs now, the one saved for
  its next start, and **Activate**, which saves the algorithm in the pair's live configuration. Nothing is
  started or stopped there: a stopped worker uses it when it is started on *Live Forward Test* (typed
  confirmation), and a running worker keeps the algorithm it started with until it is stopped and started
  again. Every start is a new live session.
* The same `algorithm` setting is the first field of *Config BTC/USDT* and *Live Config BTC/USDT*, next to
  MM_algo2's own settings (`algo3_*`). **Since 20 October 2026 the demo runs every algorithm at once:** a
  pair's demo worker runs each enabled algorithm (`algo1_enabled`, `algo3_enabled`) in its own lane, with its
  own paper account and result files (`<pair>_track_demo_<lane>.*`); the Demo table of the Algorithm page
  shows each lane's state (Running / Starting / Off) with its session as `MM_algo2 · <session id>`, and
  *Activate* / *Switch off* start or stop one lane without touching the others. The live worker keeps one
  algorithm per pair: two on one real wallet would trade against each other.

**On the results dashboard** (`http://37.32.31.162/`). The first tab is **Algorithm**: **MM_algo1v2**,
**MM_algo2**, or **All algorithms — side by side**.

* Choosing MM_algo2 shows its description, the settings in force, its headline numbers (rounds, quote rate,
  fills per hour, realised against computed spread, adverse selection, net PnL) and two charts of its own:
  the imbalance over time, and where the centre sat relative to the mid. Every other tab then shows MM_algo2's
  rounds only. The Quote decision tab draws an MM_algo2 round as its book levels, a price ladder and a
  step-by-step trace.
* *All algorithms* shows a comparison table, bars and the running net PnL of each algorithm.
* The choice is remembered per browser tab; deep link `?tab=algorithm&algo=algo3`. Each session in the
  session list starts with the algorithm(s) it ran, in the order they ran (`MM_algo1v2 → MM_algo2 · demo-…`
  for a demo session that switched); a live session id is its start time, shown with the date.
* When the chosen algorithm has no rounds in the view, the page names the tracks and sessions where it has
  run.

**Command line.** `python run.py --profile btc --mode demo --algorithm algo3` overrides the setting.

**A fair comparison** needs the same market at the same time. One pair runs one algorithm at a time, so on one
track the two algorithms cover different periods (for example, the BTC demo session that switched on
17 October). The dashboard says so under its comparison table.

---

# Other algorithms in the code

* **MM_algo1v2** (Algorithm 1 version 2, `strategy/algo1.py`, key `algo1`). It places candidate prices at fixed
  distances from an inventory-skewed centre and scores each by the chance it fills (from a measured table)
  times its profit after fees; the best pair that clears the fee bar is quoted. It stays selectable in this
  branch, and it is the code default of the `algorithm` setting, so that the two algorithms can be compared on
  the same dashboard. Its documentation, the fill model and its history are on the branch
  [`MM_algo1v2`](https://github.com/Alireza77n/Limit-Order-Book/tree/MM_algo1v2); a one-page summary is in
  [`docs/algorithms/MM_algo1v2.md`](docs/algorithms/MM_algo1v2.md).
* **Avellaneda–Stoikov** (`strategy/algo2.py`, key `algo2`): a placeholder whose `decide_order()` raises
  `NotImplementedError`. Its name has nothing to do with MM_algo2. The notebooks in `market_maker/research/`
  estimate two of its parameters; the model itself is documented on the `MM_algo1v2` branch.

---

# Architecture

```text
run.py  --algorithm  --exchange  --profile  --mode
  │
  ▼
SimpleMarketMaker (engine.py)       the round loop, inventory / cash / PnL bookkeeping,
  │                                 CSV + SQLite + MySQL persistence, optional Telegram
  │
  ├── Strategy (strategy/base.py)   built by strategy/catalog.py from cfg.algorithm
  │     ├── Algo3Strategy (algo3.py)   MM_algo2: this branch's algorithm (key algo3)
  │     ├── Algo1Strategy (algo1.py)   MM_algo1v2: Algorithm 1 version 2 (key algo1), for comparison
  │     └── Algo2Strategy (algo2.py)   Avellaneda-Stoikov placeholder, raises NotImplementedError
  │
  └── ExchangeClient (exchange/base.py)
        ├── NobitexExchangeClient (nobitex.py)   get_orderbook, send_order, wallet_balance,
        │                                        order status / cancellation via order_status.py
        └── DemoExchangeClient (demo.py)         real market data from Nobitex, simulated
                                                 fills (--mode demo)
```

* **`Strategy`** (`market_maker/strategy/base.py`): the only method the engine relies on is `decide_order(orderbook, inventory) -> quote_dict | None`. Anything implementing this can be plugged into `SimpleMarketMaker` via its optional `strategy=` constructor argument; if it is omitted, `strategy/catalog.py` builds the algorithm named by `cfg.algorithm` (`algo3` for MM_algo2, `algo1` for MM_algo1v2), and a demo worker switches at the next round when that setting changes.
* **`ExchangeClient`** (`market_maker/exchange/base.py`): `get_orderbook`, `send_order`, `wallet_balance`, `get_order_list_status`, `cancel_order`, `cancel_all_open_orders`, `cancel_old_orders`. `SimpleMarketMaker` takes an `exchange=` constructor argument (required) implementing this. Two implementations exist: `NobitexExchangeClient` (real) and `DemoExchangeClient` (real market data, simulated fills — see usage.md §1b).
* **`portfolio.py`** (`Nobitex_portfolio`): read-only wallet/portfolio valuation helper, independent of the trading engine. **Still Nobitex-specific** — not behind `ExchangeClient` (see [Known limitations](#known-limitations--next-steps)).
* **`order_status.py`** (`OrderStatusChecker`, `OrderCanceller`): raw Nobitex order-status/cancellation HTTP calls, composed inside `NobitexExchangeClient` rather than called directly by the engine.
* **`config.py`** (`MMConfig`): a single dataclass holding both strategy knobs (`inv_max`, `commission_perc`, the `algo3_*` settings, ...) and exchange config (`url_send_order`, ...). Not yet split — see [Known limitations](#known-limitations--next-steps). Also defines `TRADING_PROFILES` (every currency pair's full config, including its demo starting inventory) and `RESULTS_DIR`/`demo_csv_path` (where `run.py` and `dashboard.py` both write/read CSV output).
* **`run.py`**: the single entry point. `STRATEGIES` (the `--algorithm` choices, from `strategy/catalog.py`; without the flag the configured `algorithm` runs) and `EXCHANGES` plus `config.TRADING_PROFILES` select what actually runs; `--mode real|demo` selects real vs. simulated order placement.
* **Telegram notifications & cost tracking** (`engine.py` + `telegram_lifecycle.py`, optional): a startup message naming the algorithm (for example "MM_algo2"), a per-round summary, and threaded order-lifecycle replies (opened/partial/filled/cancelled, each reason-tagged), if `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are set. Cumulative fee totals persist across restarts in `results/paper_fee_scopes.json`. Entirely best-effort — never raises, never required. See usage.md §1.7/§6.8.

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
  engine.py             SimpleMarketMaker: the round loop and engine glue
  config.py              MMConfig dataclass; TRADING_PROFILES (per-pair
                          config incl. the algo3_* settings and demo_inv_initial);
                          RESULTS_DIR / demo_csv_path; loads secrets from .env
  strategy/
    algo3.py                MM_algo2: imbalance + microprice centre, fee-bar spread (pure, no I/O)
    common.py               Order-book arithmetic shared by the algorithms (tick, mid, returns, fee bar)
    catalog.py              The algorithms: keys, display names, the selectable ones, build_strategy()
    base.py                 Strategy interface (decide_order contract)
    algo1.py                MM_algo1v2 (Algorithm 1 version 2), selectable for comparison
    algo2.py                Avellaneda-Stoikov placeholder (raises NotImplementedError)
  fill_model.py         fill-probability table + fallback, used by MM_algo1v2 only
  tools/build_fill_table.py  builds fill_table_<pair>.json for MM_algo1v2 (nightly job)
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
  dashboard.py             Dash results dashboard (Algorithm tab first), reads SQLite (falls back to CSV)
  admin_app.py             Authenticated admin panel (algorithm page, config pages, workers, live forward test, ...)
  exchange/
    base.py                 ExchangeClient interface
    nobitex.py                NobitexExchangeClient (Nobitex HTTP calls)
    demo.py                    DemoExchangeClient (real data, simulated fills
                                driven by the canonical paper.py ledger)
  research/                 notebooks estimating Avellaneda-Stoikov parameters
  tests/                   pytest suite (about 960 deterministic tests; run from repo root)
  results/                 runtime state (round stores, paper ledgers, fee scopes) --
                            gitignored; the directory itself is tracked via .gitkeep
  .env.example              Template for required environment variables
  .env                      Real secrets -- gitignored, never committed

docs/
  algorithms/MM_algo2.md     MM_algo2, the full specification
  algorithms/data/           the real order books, settings and demo rounds behind every number,
                             and make_mm_algo2_doc_data.py, which computes them with this branch's code
  algorithms/figures/        the specification's figures and make_mm_algo2_figures.py
  algorithms/PNL_ATTRIBUTION.md  the dashboard's split of each round's PnL into price and trading
  algorithms/MM_algo1v2.md   one page on the comparison algorithm, with links to its branch
  guide/                     the whole system explained, as Markdown and as a standalone HTML page
  deployment/                admin panel and deployment notes
```

---

# Tracked vs. local-only files

Everything under `market_maker/` and this README are committed and visible to the whole team (and anyone with repo access). These are deliberately gitignored and never pushed:

| Path | What it is | Why it's local-only |
|---|---|---|
| `market_maker/.env` | Real Nobitex keys, MySQL credentials, optional Telegram bot token/chat id | Secrets — `market_maker/.env.example` is the committed template |
| `market_maker/results/*.csv` | Every `run.py` session's logged output (real and demo, both pairs) | Runtime data, machine-specific, not a project deliverable — the `results/` directory itself is tracked (via `.gitkeep`) so it always exists |
| `market_maker/results/*.sqlite3(-wal/-shm)` | Canonical bounded Demo round history behind the CSV export (usage.md §6.4) | Local runtime database, machine-specific |
| `market_maker/results/paper_*.json`, `paper_*_archive*.jsonl` | Canonical Demo paper-order ledger, cumulative fee scopes, and rotated terminal-order archives | Local runtime state, regenerated per machine, not meaningful to share |

`docs/` is ignored by default in `.gitignore`; the documents and data files listed above are committed
explicitly (`git add -f`). If you're missing `market_maker/.env`, copy `market_maker/.env.example` and fill in
real values — see **[usage.md](usage.md)**.

---

# Setup, running, and testing

Full details — environment setup, every way to run the bot (which modes touch a real account/real money and which don't), and how to run the test suite — are in **[usage.md](usage.md)**.

Quick summary: `run.py --profile btc|usdt --algorithm algo3` places **real orders on Nobitex with real money**
and needs a live MySQL server. `--mode demo` trades against the same real market data with a deterministic
simulated paper-order lifecycle instead — no real orders, no MySQL required (see usage.md §1b). The test suite
(`python3 -m pytest -q` from the repo root) needs neither a real account, MySQL, nor network/Telegram access —
all of its tests (about 960) are deterministic (mocks and replay fixtures).

The results dashboard (`python dashboard.py`) shows, for every real/demo × BTC/USDT track and for the
algorithm chosen on its first tab, the headline numbers, one round's quote decision, the order book, the
demo-to-real validation gate, the PnL attribution and every round as a table — see usage.md §3. The
authenticated admin panel (`python admin_app.py`) adds worker, session, configuration (including the
algorithm of each pair), backup and live-forward control — see usage.md §3 and
`docs/deployment/ADMIN_PAGE.md`.

`docker compose up -d --build --wait` (repo root) runs a shared session initializer, both pairs in continuous demo mode, the dashboard, and the authenticated Admin service, sharing bind-mounted runtime data. Admin mutations use the separate local host agent (`python3 deploy/control_agent.py --mode local`) rather than a Docker socket — see usage.md §6–7.

---

# Known limitations / next steps

MM_algo2:

* **The lean $\beta$ is 0 until it is estimated.** The imbalance does not move the quotes yet (see
  [Why the lean β is still 0](#why-the-lean-β-is-still-0)); days of book data with volumes are needed.
* **The half-spread is the fee bar, not a choice.** Every completed round trip earns about $r_{\min}$
  (0.01 %). Nothing weighs the chance of a fill, or the cost of only one side filling.
* **Its orders usually wait behind the best prices**, so fills are fewer than for a quote at the touch, and
  far fewer at the live fee.
* **One side filling is not priced.** The skew and the sizing slow an accumulation; the band caps it.
* **The requote band is one fraction of the price for both pairs** (0.03 %): a narrower band replaces orders
  more often, a wider one leaves a staler order; it replaced the 5-tick window on 20 October 2026.
* **Demo results so far are hours, not days**: run MM_algo2 in demo for at least five days and check the
  validation gate before real money ([`MM_algo2.md` §17](docs/algorithms/MM_algo2.md#17-the-demo-to-real-gate)).

The engine and the project:

* **Any API error other than a failed order-book read ends a live worker** (after it cancels its own orders).
  This stopped the BTC live worker on 16 October 2026.
* **Live order amounts are sent as `str(amount)`**, which for small BTC sizes is scientific notation
  (`2.378e-05`). The test suite does not check it against Nobitex, and MM_algo2 sends such a buy from its first
  live BTC round ([`MM_algo2.md` §13](docs/algorithms/MM_algo2.md#13-what-mm_algo2-would-send-with-the-live-settings)).
* **`config.py` mixes strategy and exchange config** in one `MMConfig` dataclass. Splitting it (e.g. `StrategyConfig` + `ExchangeConfig`) was deferred; do it before adding a second exchange.
* **Persistence (`save_to_db`, the MySQL connection in `__init__`) is not behind an interface** — `SimpleMarketMaker` still requires a live MySQL server to be constructed in real mode.
* **`portfolio.py`'s `Nobitex_portfolio` is not behind `ExchangeClient`** — `run.py`'s `--exchange` flag today only changes order execution/quotes, not portfolio valuation.
* **Git history contains the original hardcoded secrets** (from commits predating the `.env` migration). Rotating those credentials is recommended and is a team/repo-owner decision.
* **Demo mode's fills are an approximation, not a matching engine**: a resting paper order fills when the real market price reaches it, at its own limit price. It does not model Nobitex's real queue priority or slippage. See usage.md §1b.
* **The diagnostics file keeps the name `…_algo1_diagnostics.jsonl`** for both algorithms (each document carries `algorithm`).

---

# Extending

**Add a new algorithm:** implement `Strategy` (`decide_order`, plus `diagnostics` / `no_quote_reason` for the dashboard and alerts) in `market_maker/strategy/` with a class attribute `key`, and add one `AlgorithmInfo` entry to `strategy/catalog.py` (its `label` is the name shown everywhere, by convention the git branch it is built on); add its key to `demo_config.ALGORITHM_CHOICES` (pinned equal by a test) and its settings to both `TRADING_PROFILES`, `demo_config.FIELD_DEFINITIONS` and `forward_config.FORWARD_FIELDS`. The CLI, the admin drop-down and the dashboard's Algorithm tab pick it up from the catalog. No engine or exchange changes needed.

**Add a new exchange:** implement `ExchangeClient` (`market_maker/exchange/base.py`) in `market_maker/exchange/`, add it to `run.py`'s `EXCHANGES` dict. No engine or strategy changes needed.

**Add a new currency pair:** add an entry to `config.py`'s `TRADING_PROFILES` dict with the pair's `MMConfig` values (including its own `demo_inv_initial`). No new files, and `run.py`/`dashboard.py` pick it up automatically.

---

# Research purpose

This project is designed for experimentation with cryptocurrency market-making strategies. With MM_algo2 in
particular:

* estimating $\beta$ on days of order books with volumes, and switching the lean on only if it predicts;
* comparing MM_algo2 with MM_algo1v2 on the same market (the dashboard's *All algorithms* view);
* testing wider half-spreads ($h_{\text{cfg}} > h_{\min}$) against fill rate and realised spread;
* studying the clip, the decay $\rho$ and the depth $k$ once $\beta > 0$;
* measuring adverse selection after fills, and the PnL split into price and trading.

---

# Disclaimer

This project is for research and educational purposes only.

It is not financial advice and should not be used for live trading without careful testing, validation, and risk management.
