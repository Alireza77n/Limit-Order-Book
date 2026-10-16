# Algorithm 3 — order-book imbalance and microprice market maker

**Code:** [`market_maker/strategy/algo3.py`](../../market_maker/strategy/algo3.py) (`Algo3Strategy`),
shared arithmetic in [`market_maker/strategy/common.py`](../../market_maker/strategy/common.py),
the list of algorithms in [`market_maker/strategy/catalog.py`](../../market_maker/strategy/catalog.py).
**Tests:** `market_maker/tests/test_strategy_algo3.py`, `test_strategy_catalog.py`, `test_dashboard_algorithms.py`.
**Source design:** the note *"Algorithm 3 — Order-Book Imbalance and Microprice Market Maker"* (10 October 2026,
`Server_project/Suggested Algorithm/`). This document is the implemented version; section 3.3 corrects one
statement of the note.

Every number in section 5 is produced by the code and pinned by
`test_spec_worked_example_reproduces_every_number`.

---

## 0. What it does, in one paragraph

Once per round the engine hands the strategy the order book and the inventory. Algorithm 3 measures how
much volume rests on each side near the top of the book (the **imbalance**), starts its quote centre at the
**microprice** (the mid pulled toward the thinner side), leans the centre in the direction of the imbalance,
skews it against the inventory, and quotes **one symmetric half-spread** around it — the smallest half-spread
whose round trip clears the fee bar, plus one tick. Prices are rounded outward to the tick, never cross the
touch, and are checked against the fee bar once more. A resting pair is kept while both prices stay within a
few ticks. The engine then sizes, caps and sends the orders exactly as for Algorithm 1.

Algorithm 3 never reads a fill probability: the lean and the skew move the **centre** only, so the ratio of
the two prices — and therefore the profit of a filled round trip — is fixed by the half-spread alone.

---

## 1. Notation

| Symbol | Meaning | Config field (default) |
|---|---|---|
| $b_i, v^b_i$ | bid price and volume at book level $i$ ($i = 0$ is the best bid) | — |
| $a_i, v^a_i$ | ask price and volume at level $i$ | — |
| $m = (b_0 + a_0)/2$, $s = a_0 - b_0$ | mid-price and best spread | — |
| $\epsilon$ | tick: 10 rial on `…IRT` pairs | — |
| $k$ | book levels per side in the imbalance | `algo3_depth_k` (3) |
| $\rho$ | weight of each deeper level, $w_i = \rho^i$ | `algo3_decay_rho` (0.5) |
| $I$ | imbalance, clipped to $[-I_{\max}, I_{\max}]$ | `algo3_imbalance_clip` = $I_{\max}$ (0.8) |
| $V_{\min}$ | volume below which the top of the book carries no signal | `algo3_min_volume_orders` × `order_size` (20 × S) |
| $\beta$ | lean of the centre per unit of imbalance, fraction of price | `algo3_beta` (0) |
| $q, x$ | inventory and its position in $[q_{\min}, q_{\max}]$, clamped to $[0,1]$ | `inv_min`, `inv_max` |
| $\sigma_q$ | centre shift at the band edges, fraction of price (shared with Algorithm 1) | `skew_perc` (0.002) |
| $f, r_{\min}$ | fee per leg and required net return per round trip | `commission_perc`, `exp_ret_min` |
| $G = (1+r_{\min})/(1-f)^2 - 1$ | gross bar | — |
| $h$ | half-spread, fraction of the centre | `algo3_half_spread` (0 = automatic) |
| $n, T$ | hysteresis ticks and refresh time | `hysteresis_ticks` (5), `refresh_seconds` (300) |

---

## 2. The signals

### 2.1 Imbalance

$$
V^b = \sum_{i=0}^{k-1} \rho^{\,i}\, v^b_i,\qquad V^a = \sum_{i=0}^{k-1} \rho^{\,i}\, v^a_i,\qquad
I = \frac{V^b - V^a}{V^b + V^a} \in [-1, 1].
$$

$I > 0$: more resting demand than supply near the touch, so the ask side is the one likely to be consumed
and the next mid move is more likely up. Two guards: if $V^b + V^a < V_{\min}$ the book is too thin to carry
information and $I = 0$ (and the centre starts at the mid instead of the microprice); otherwise $I$ is clipped
to $\pm I_{\max}$ so one large (possibly spoofed) level cannot push the lean to its maximum.

### 2.2 Microprice

$$
p_{\text{micro}} = \frac{a_0\, v^b_0 + b_0\, v^a_0}{v^b_0 + v^a_0}.
$$

It equals the mid shifted by half the spread times the imbalance of the best level, $I_0$:

$$
p_{\text{micro}} - m
= \frac{2a_0 v^b_0 + 2b_0 v^a_0 - (a_0+b_0)(v^b_0+v^a_0)}{2(v^b_0+v^a_0)}
= \frac{(a_0-b_0)(v^b_0 - v^a_0)}{2(v^b_0+v^a_0)}
= \frac{s}{2}\, I_0 .
$$

Since $|I_0| \le 1$, the microprice always lies between $b_0$ and $a_0$ (pinned by
`test_microprice_is_the_mid_shifted_by_half_spread_times_level0_imbalance` on random books).

---

## 3. The quote

### 3.1 Centre

$$
c = p_{\text{micro}} \,\bigl(1 + \beta I\bigr)\,\bigl(1 - \sigma_q (2x - 1)\bigr).
$$

Buying pressure ($I > 0$) raises both quotes; inventory above the middle of the band ($x > \tfrac12$) lowers
both, so selling becomes more likely than buying.

### 3.2 Half-spread from the fee bar

With $B = c(1-h)$ and $A = c(1+h)$ the round trip returns, on the capital $B$,

$$
R_{\text{net}} = (1-f)^2\,\frac{A}{B} - 1 = (1-f)^2\,\frac{1+h}{1-h} - 1 ,
$$

which depends on $h$ only. Requiring $R_{\text{net}} > r_{\min}$:

$$
\frac{1+h}{1-h} > 1 + G
\;\Longleftrightarrow\; 1 + h > (1+G)(1-h)
\;\Longleftrightarrow\; h\,(2 + G) > G
\;\Longleftrightarrow\; h > h_{\min} = \frac{G}{2+G}.
$$

The strategy uses $h = \max(h_{\text{config}},\; h_{\min} + \epsilon/c)$: one tick of margin above the bar.
At the live settings ($f = 0.25\%$, $r_{\min} = 0.01\%$): $G = 0.5119\%$ and $h_{\min} = 0.2553\%$. At the
profile defaults ($f = r_{\min} = 0.10\%$): $G = 0.3005\%$, $h_{\min} = 0.1500\%$.

### 3.3 Tick rounding, the touch, and the final check

$$
B = \epsilon \Big\lfloor \frac{\min(c(1-h),\ a_0 - \epsilon)}{\epsilon} \Big\rfloor,\qquad
A = \epsilon \Big\lceil \frac{\max(c(1+h),\ b_0 + \epsilon)}{\epsilon} \Big\rceil .
$$

The bid never reaches the ask side and the ask never reaches the bid side. **Correction of the design note:**
the note says the clip to the touch "can narrow" the quote. It cannot: both the clip and the rounding only
*lower* the bid and *raise* the ask, so $B \le c(1-h) < c(1+h) \le A$ and

$$
\frac{A}{B} \;\ge\; \frac{1+h}{1-h} \;>\; 1 + G \quad\Longrightarrow\quad R_{\text{net}}(B, A) > r_{\min}.
$$

The code still re-checks $R_{\text{net}} > r_{\min}$ on the exact prices sent (reason `net_return_below_bar`
if it ever failed); with $h > h_{\min}$ that check cannot fail
(`test_every_quote_clears_the_fee_bar_never_crosses_and_sits_on_the_tick` runs 2,000 random books and
settings; `test_clipping_at_the_touch_only_widens_the_quote` shows a clipped case).

### 3.4 Hysteresis

The previous pair $(B^\circ, A^\circ)$ is sent again while $|B - B^\circ| \le n\epsilon$ **and**
$|A - A^\circ| \le n\epsilon$ and it is younger than $T$ (and still clears the bar); otherwise the new pair
replaces it. The engine's resting-order guard (`hysteresis_ticks`) then avoids re-posting an order that already
rests within $n$ ticks, and stale orders are cancelled after `cancel_after_minutes`, as for Algorithm 1.

---

## 4. The procedure (as implemented)

1. Mid from the best bid/ask; no quote if the book is missing, non-positive or crossed
   (`invalid_or_single_level_order_book`), or has fewer than $k$ levels on a side (`book_shallower_than_depth_k`).
2. $V^b, V^a, I$ over the top $k$ levels; $I = 0$ and centre on the mid if $V^b + V^a < V_{\min}$; clip $I$.
3. Microprice $p_{\text{micro}}$.
4. Inventory position $x$.
5. Centre $c$ (§3.1).
6. Half-spread $h = \max(h_{\text{config}}, h_{\min} + \epsilon/c)$ (§3.2).
7. $B$, $A$ rounded outward, clipped at the touch (§3.3).
8. Final check $R_{\text{net}} > r_{\min}$ on $B, A$ (`net_return_below_bar` otherwise).
9. Hysteresis (§3.4).
10. Book levels for logging: $i^* = \max\{i : b_i \ge B\}$, $j^* = \max\{j : a_j \le A\}$.

The strategy hands the engine the same keys as Algorithm 1 (`bid`, `ask`, `bid level`, `ask level`, mid,
returns, `quote mode` = `pair`, `selection reason` = `microprice_pair` or `microprice_pair_kept`) plus
`imbalance` and `microprice`; the fill-probability and expected-value keys are empty.

---

## 5. Worked example (the design note's example, run through `Algo3Strategy`)

USDT/IRT book: best bid 2,658,990 (20,000 USDT), best ask 2,659,010 (5,000 USDT); top three levels bids
20,000 / 8,000 / 6,000 and asks 5,000 / 4,000 / 3,000. Settings: $k = 3$, $\rho = 1$, $\beta = 0.001$,
$\sigma_q = 0.002$, $h_{\text{config}} = 0.38\%$, $f = r_{\min} = 0.25\%$; inventory 10 of $[0, 50]$.

| Step | Value |
|---|---|
| $V^b,\ V^a$ | 34,000 and 12,000 |
| $I$ | $22{,}000 / 46{,}000 = 0.478$ |
| $p_{\text{micro}}$ | $(2{,}659{,}010 \cdot 20{,}000 + 2{,}658{,}990 \cdot 5{,}000)/25{,}000 = 2{,}659{,}006$ ($= m + 10 \times 0.6$) |
| $x$, skew factor | $0.2$, $1 - 0.002\,(0.4 - 1) = 1.0012$ |
| lean factor | $1 + 0.001 \times 0.478 = 1.000478$ |
| $c$ | $2{,}663{,}470$ |
| $h_{\min}$, $h$ | $0.3752\%$, $\max(0.38\%, 0.3752\% + 0.0004\%) = 0.38\%$ |
| $B$, $A$ | $2{,}653{,}348.8 \to$ **2,653,340**; $2{,}673{,}591.2 \to$ **2,673,600** |
| $R_{\text{net}}$ | $0.9975^2 \times 2{,}673{,}600/2{,}653{,}340 - 1 = 0.2604\% > 0.25\%$: quote sent |

Symmetric quotes around the mid with the same $h$ would be 2,648,890 / 2,669,110; Algorithm 3's bid is 4,450 rial
and its ask 4,490 rial higher: the book shows buying pressure and the inventory is low, so the strategy positions
to buy before a rise and to sell only at a better price.

---

## 6. Properties (each pinned by a test)

* **Profitability:** every quote returned clears $R_{\text{net}} > r_{\min}$ on the prices sent (§3.3).
* **No crossing, on the tick:** $B \le a_0 - \epsilon$, $A \ge b_0 + \epsilon$, both multiples of $\epsilon$.
* **Spread invariance:** before rounding $A/B = (1+h)/(1-h)$ whatever $I$ and $x$ are.
* **Monotonicity:** more buying pressure moves both quotes up; more inventory moves both down.
* **Reduction:** $\beta = 0$, $k = 1$, a balanced top of book and $x = \tfrac12$ give the fixed-spread market
  maker centred on the mid.

---

## 7. Settings and calibration

All Algorithm 3 fields are in both trading profiles and editable in the admin panel (Demo config and Live
config). Defaults: $k = 3$, $\rho = 0.5$, $\beta = 0$, automatic half-spread, $V_{\min} = 20$ order sizes,
$I_{\max} = 0.8$; `skew_perc`, `hysteresis_ticks` and `refresh_seconds` are shared with Algorithm 1.

**$\beta$ starts at 0** (microprice centre plus inventory skew, no lean) until it is estimated. The note's
procedure: regress the forward mid return over the order lifetime on the imbalance,
$r_{t,\tau} = \beta I_t + e_t$, $\hat\beta = \sum_t I_t r_{t,\tau} / \sum_t I_t^2$, and use $\hat\beta$ only if
its $t$-statistic exceeds 3 (with standard errors that account for overlapping windows). On the only order-book
sample with volumes available so far (3 October 2026, 578 snapshots per pair, about two hours, $k = 3$,
$\rho = 0.5$, $\tau = 600$ s): USDT $\hat\beta = 2.5 \times 10^{-4}$ ($t = 1.1$), BTC
$\hat\beta = 4.4 \times 10^{-4}$ ($t = 0.9$) — below the threshold, so $\beta = 0$ stays. On the same sample the
microprice moved the centre by a median of 0.008 % (USDT) and 0.013 % (BTC) of price, small next to
$h_{\min} = 0.255\%$ at live fees. Days of book data with volumes are needed before $\beta$ is switched on.

---

## 8. Compared with Algorithm 1

| | Algorithm 1 | Algorithm 3 |
|---|---|---|
| Centre | mid × inventory skew | microprice × imbalance lean × the same inventory skew |
| Prices | best pair of a grid of candidates, each one tick inside a book level | one symmetric pair at ±$h$ around the centre |
| Choice rule | largest expected value $J = p_b p_a R_{\text{net}} - \eta\,P(\text{one leg})$ above `ev_min` | none: $h$ is the smallest half-spread that clears the bar |
| Fill probabilities | measured table (formula fallback) | not used |
| One-sided quotes | at the band edges and as a fallback | never; the engine's linear sizing makes a side zero at the band edges |
| Fee bar, sizing, caps, cancels, hysteresis | the same | the same |

---

## 9. Choosing and comparing algorithms

* **Admin panel:** the `Algorithm` field (first on *Config BTC/USDT* and *Live Config BTC/USDT*) takes `algo1` or
  `algo3`. A Demo worker switches at its next round (same session, same ledger; its strategy memory starts
  empty). A live worker reads the value when it is started: the forward agent starts
  `run.py --algorithm <value>`; a running live worker keeps the algorithm it started with. The admin overview
  shows the algorithm in force for each pair, Demo and live.
* **Command line:** `run.py --algorithm algo3` overrides the configured value.
* **Results:** every round row carries `Strategy` (`algo1` / `algo3`; older rows count as `algo1`) and the
  Algorithm 3 numbers `Imbalance` and `Microprice`; the diagnostics JSONL documents carry `algorithm` and, for
  Algorithm 3, the levels, the imbalance, the microprice, the centre and $h$.
* **Dashboard:** the **Algorithm** tab lists the algorithms. Choosing one shows its description, the settings in
  force, its headline numbers and its own charts (Algorithm 3: imbalance and centre shift over time; Algorithm 1:
  expected value and fill probabilities), and every other tab then shows that algorithm's rounds only (the Quote
  decision tab draws an Algorithm 3 round as its book levels, a price ladder and a step-by-step trace).
  **All algorithms** shows them side by side: a comparison table, headline bars and the running net PnL of each
  algorithm. Deep link: `?tab=algorithm&algo=algo3`.

## 10. Failure modes

* **Spoofing** (large volume placed and pulled): clip $I_{\max}$, $\rho < 1$, $\beta$ estimated on data that
  contains such episodes.
* **Thin book:** $V_{\min}$ sets $I = 0$ and centres on the mid.
* **HTTP 403 / no book:** no quote, as for Algorithm 1; resting orders are cancelled by the stale-order rule.
* **Trend:** a lasting one-sided imbalance fills one side repeatedly; the inventory skew, the linear sizing and
  `inv_max` bound the position.
