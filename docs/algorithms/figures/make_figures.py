"""Regenerate the figures in this directory from the real strategy code.

    cd market_maker && python ../docs/algorithms/figures/make_figures.py

Every number in the figures comes from strategy/algo1.py and fill_model.py
applied to the running example of ALGORITHM_1_V2.md, so the pictures and
the text cannot drift apart.
"""
from __future__ import annotations

import math
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE = os.path.abspath(os.path.join(HERE, "..", "..", "..", "market_maker"))
sys.path.insert(0, PACKAGE)

from config import MMConfig, TRADING_PROFILES  # noqa: E402
from fill_model import formula_probability  # noqa: E402
from strategy.algo1 import Algo1Strategy  # noqa: E402

BLUE, ORANGE, TEAL, GREY, INK = "#2a78d6", "#eb6834", "#0f6e6e", "#9aa6aa", "#1d2326"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.spines.top": False,
                     "axes.spines.right": False, "figure.dpi": 130})


def example_book():
    bids = [[2_659_990, "20000"], [2_659_900, "300"], [2_658_000, "1500"], [2_655_000, "8000"],
            [2_650_000, "40000"], [2_640_000, "10000"], [2_630_000, "5000"], [2_620_000, "5000"],
            [2_610_000, "5000"], [2_600_000, "5000"], [2_590_000, "5000"]]
    asks = [[2_660_010, "5000"], [2_660_100, "400"], [2_662_000, "2000"], [2_665_000, "6000"],
            [2_670_000, "30000"], [2_680_000, "12000"], [2_690_000, "5000"], [2_700_000, "5000"],
            [2_710_000, "5000"], [2_720_000, "5000"], [2_730_000, "5000"]]
    return {"bids": bids, "asks": asks, "lastTradePrice": "2660000"}


def example_strategy(**overrides):
    cfg = MMConfig(**TRADING_PROFILES["usdt"])
    cfg.commission_perc = 0.0025
    cfg.exp_ret_min = 0.0025
    cfg.skew_perc = 0.0
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return Algo1Strategy(cfg)


def fig_grid_anchoring():
    strategy = example_strategy(grid_min_perc=0.001, grid_ratio=2.0, grid_max_perc=0.04)
    ob = example_book()
    centre = 2_660_000.0
    bids, asks = strategy.build_candidates(ob, centre)
    fig, ax = plt.subplots(figsize=(8.2, 5.2))
    levels = [(float(p), float(v)) for p, v in ob["bids"]]
    prices = [p for p, _ in levels]
    volumes = [v for _, v in levels]
    ax.barh(prices, volumes, height=1_800, color=BLUE, alpha=.35, label="bid volume (USDT)")
    for i, (p, v) in enumerate(levels):
        ax.text(max(volumes) * 1.02, p, f"level {i}: {p:,.0f}", va="center", fontsize=8, color=INK)
    for delta in strategy.distance_grid():
        target = centre * (1 - delta)
        ax.axhline(target, color=GREY, lw=.8, ls=":")
        ax.text(-max(volumes) * .02, target, f"target {delta*100:.1f}%", ha="right", va="center", fontsize=7.5, color=GREY)
    for c in bids:
        marker = "o" if c.anchored else "s"
        ax.plot([max(volumes) * .55], [c.price], marker=marker, ms=9, color=TEAL if c.anchored else ORANGE, zorder=5)
        ax.text(max(volumes) * .58, c.price, f"B = {c.price:,.0f}  ({c.distance*100:.2f}%)", va="center", fontsize=8,
                color=TEAL if c.anchored else ORANGE)
    ax.axhline(centre, color=INK, lw=1.2)
    ax.text(max(volumes) * .55, centre + 900, "centre c = mid = 2,660,000", fontsize=8.5, color=INK)
    ax.set_xlim(-max(volumes) * .35, max(volumes) * 1.35)
    ax.set_ylim(2_572_000, 2_664_000)
    ax.set_xlabel("resting volume")
    ax.set_ylabel("price (rial)")
    ax.set_title("Grid targets (dotted) anchored to book levels: circle = anchored, square = synthetic", fontsize=10)
    ax.legend(loc="lower right")
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    fig.tight_layout()
    fig.savefig(os.path.join(HERE, "fig1_grid_anchoring.png"))
    plt.close(fig)


def fig_fill_probability():
    d = np.linspace(0.0, 0.02, 400)
    fig, ax = plt.subplots(figsize=(7.6, 4.4))
    for kappa, lam, style, label in ((400, 1.0, "-", "formula: kappa = 400, lambda*tau = 1"),
                                     (200, 1.0, "--", "kappa = 200 (slower decay)"),
                                     (400, 3.0, ":", "kappa = 400, lambda*tau = 3 (busier)")):
        ax.plot(d * 100, [formula_probability(x, kappa, lam) for x in d], style, color=TEAL, lw=1.8, label=label)
    table_d = [0.10, 0.15, 0.20, 0.30, 0.40, 0.60, 0.80, 1.20]
    table_p = [.61, .47, .38, .26, .19, .09, .04, .01]
    ax.step(table_d, table_p, where="mid", color=ORANGE, lw=1.6, label="measured table (example, busy hours)")
    ax.plot(table_d, table_p, "o", color=ORANGE, ms=5)
    ax.set_xlabel("distance from the quote centre, % of price")
    ax.set_ylabel("P(fill within one lifetime)")
    ax.set_ylim(0, 1)
    ax.set_title("Fill probability: parametric fallback vs a measured table")
    ax.legend(fontsize=8)
    ax.grid(alpha=.3)
    fig.tight_layout()
    fig.savefig(os.path.join(HERE, "fig2_fill_probability.png"))
    plt.close(fig)


def fig_ev_heatmap():
    strategy = example_strategy()
    ob = example_book()
    evaluation = strategy.evaluate(ob, 25.0)
    bids = sorted({r.bid.price for r in evaluation.pairs}, reverse=True)
    asks = sorted({r.ask.price for r in evaluation.pairs})
    matrix = np.full((len(bids), len(asks)), np.nan)
    passed = np.zeros_like(matrix, dtype=bool)
    nets = np.full_like(matrix, np.nan)
    for r in evaluation.pairs:
        i, j = bids.index(r.bid.price), asks.index(r.ask.price)
        matrix[i, j] = r.expected_value * 100
        nets[i, j] = r.net_return * 100
        passed[i, j] = r.passed
    fig, ax = plt.subplots(figsize=(9, 6.2))
    shown = np.where(passed, matrix, np.nan)
    im = ax.imshow(shown, cmap="Greens", aspect="auto")
    for i in range(len(bids)):
        for j in range(len(asks)):
            if np.isnan(matrix[i, j]):
                continue
            if passed[i, j]:
                ax.text(j, i, f"{matrix[i, j]:.4f}%\nR {nets[i, j]:.2f}%", ha="center", va="center", fontsize=7.5, color=INK)
            elif nets[i, j] <= strategy.min_required_return() * 100:
                ax.text(j, i, f"R {nets[i, j]:.2f}%\nfails bar", ha="center", va="center", fontsize=7.5, color=GREY)
            else:
                ax.text(j, i, f"R {nets[i, j]:.2f}%\nJ below floor", ha="center", va="center", fontsize=7.5, color=GREY)
    best = evaluation.chosen_pair
    bi, bj = bids.index(best.bid.price), asks.index(best.ask.price)
    ax.add_patch(plt.Rectangle((bj - .5, bi - .5), 1, 1, fill=False, ec=INK, lw=2.5))
    bid_probs = {r.bid.price: r.bid.probability for r in evaluation.pairs}
    ask_probs = {r.ask.price: r.ask.probability for r in evaluation.pairs}
    ax.set_xticks(range(len(asks)))
    ax.set_xticklabels([f"{a:,.0f}\n{(a-2_660_000)/2_660_000*100:.2f}%  p={ask_probs[a]:.2f}" for a in asks], fontsize=7.5)
    ax.set_yticks(range(len(bids)))
    ax.set_yticklabels([f"{b:,.0f}  {(2_660_000-b)/2_660_000*100:.2f}%  p={bid_probs[b]:.2f}" for b in bids], fontsize=7.5)
    ax.set_xlabel("ask candidates (price, distance, fill probability)")
    ax.set_ylabel("bid candidates")
    ax.set_title("Expected value J of every pair (% of capital per lifetime); grey = rejected; box = winner", fontsize=10)
    fig.colorbar(im, ax=ax, label="J (%)")
    fig.tight_layout()
    fig.savefig(os.path.join(HERE, "fig3_ev_heatmap.png"))
    plt.close(fig)


def fig_centre_skew():
    x = np.linspace(0, 1, 200)
    fig, ax = plt.subplots(figsize=(7.6, 4.2))
    for skew, style in ((0.002, "-"), (0.004, "--")):
        ax.plot(x, -(skew * (2 * x - 1)) * 100, style, color=TEAL, lw=1.8, label=f"skew_perc = {skew*100:.1f}%")
    ax.axvspan(0, 0.1, color=BLUE, alpha=.12)
    ax.axvspan(0.9, 1.0, color=ORANGE, alpha=.12)
    ax.text(0.05, 0.33, "bids only\n(x <= one_sided_lo)", ha="center", fontsize=8.5, color=BLUE)
    ax.text(0.95, 0.33, "asks only\n(x >= one_sided_hi)", ha="center", fontsize=8.5, color=ORANGE)
    ax.axhline(0, color=GREY, lw=.8)
    ax.set_xlabel("inventory position x = (q - inv_min) / (inv_max - inv_min)")
    ax.set_ylabel("centre shift (c - mid) / mid, %")
    ax.set_title("Item 2/5 - inventory moves the quote centre; band edges switch to one side")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=.3)
    fig.tight_layout()
    fig.savefig(os.path.join(HERE, "fig4_centre_skew.png"))
    plt.close(fig)


def fig_single_leg_bar():
    fee, r_min = 0.0025, 0.0025
    G = (1 + r_min) / (1 - fee) ** 2 - 1
    ref = 2_660_000
    fig, ax = plt.subplots(figsize=(7.6, 3.6))
    ax.axvline(ref, color=INK, lw=1.4)
    ax.text(ref, 1.05, "reference price\n(centre or cost basis)", ha="center", fontsize=8.5)
    ax.axvspan(ref * (1 + G), 2_700_000, color=ORANGE, alpha=.15)
    ax.axvspan(2_620_000, ref / (1 + G), color=BLUE, alpha=.15)
    ax.text(ref * (1 + G) + 3_000, .5, f"lone SELL allowed\nA > ref x (1+G)\n= {ref*(1+G):,.0f}", fontsize=8.5, color=ORANGE, va="center")
    ax.text(ref / (1 + G) - 3_000, .5, f"lone BUY allowed\nB < ref / (1+G)\n= {ref/(1+G):,.0f}", fontsize=8.5, color=BLUE, va="center", ha="right")
    ax.annotate("", xy=(ref * (1 + G), .2), xytext=(ref, .2), arrowprops=dict(arrowstyle="<->", color=GREY))
    ax.text(ref * (1 + G / 2), .25, f"G = {G*100:.3f}%", ha="center", fontsize=8.5, color=GREY)
    ax.set_xlim(2_620_000, 2_700_000)
    ax.set_ylim(0, 1.3)
    ax.set_yticks([])
    ax.set_xlabel("price (rial)")
    ax.set_title("Item 5 - a single leg must clear the whole gross bar G against its reference price")
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    fig.tight_layout()
    fig.savefig(os.path.join(HERE, "fig5_single_leg_bar.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_grid_anchoring()
    fig_fill_probability()
    fig_ev_heatmap()
    fig_centre_skew()
    fig_single_leg_bar()
    print("figures written to", HERE)
