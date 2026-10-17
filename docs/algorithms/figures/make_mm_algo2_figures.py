"""Figures of docs/algorithms/MM_algo2.md, drawn from data/mm_algo2_doc_numbers.json (itself produced
by data/make_mm_algo2_doc_data.py from the real books in data/). Run after the data script:

    python docs/algorithms/figures/make_mm_algo2_figures.py
"""
import json
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

HERE = Path(__file__).resolve().parent
DATA = json.loads((HERE.parent / "data" / "mm_algo2_doc_numbers.json").read_text(encoding="utf-8"))

BID, ASK, ACCENT, MICRO, INK, MUTED, GRID = "#2a78d6", "#eb6834", "#1f5f9f", "#7b4bb7", "#1d2326", "#5d6572", "#e6e2d8"
plt.rcParams.update({
    "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white",
    "font.size": 10, "axes.titlesize": 11, "axes.titleweight": "bold", "axes.labelcolor": INK,
    "axes.edgecolor": "#b9b4a8", "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "xtick.color": MUTED, "ytick.color": MUTED, "legend.frameon": False,
})


def save(fig, name):
    fig.savefig(HERE / name, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("wrote", name)


def rial(v):
    return f"{v:,.0f}"


# ------------------------------------------------------------ 1. example 1: the book and the prices
m = DATA["main_example"]
e = m["evaluation"]
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.6), gridspec_kw={"width_ratios": [1, 1.25]})
levels = e["levels"]
y = np.arange(len(levels))[::-1]
wb = [lv["bid_volume"] * lv["weight"] for lv in levels]
wa = [lv["ask_volume"] * lv["weight"] for lv in levels]
ax1.barh(y + 0.18, wb, height=0.34, color=BID, label="bid volume × weight")
ax1.barh(y - 0.18, wa, height=0.34, color=ASK, label="ask volume × weight")
for yi, lv, b, a in zip(y, levels, wb, wa):
    ax1.text(b, yi + 0.18, f"  {lv['bid_volume']:,.2f} × {lv['weight']:g} = {b:,.3f}", va="center", fontsize=8.5, color=INK)
    ax1.text(a, yi - 0.18, f"  {lv['ask_volume']:,.2f} × {lv['weight']:g} = {a:,.3f}", va="center", fontsize=8.5, color=INK)
ax1.set_yticks(y, [f"level {lv['level']}\n{rial(lv['bid'])} / {rial(lv['ask'])}" for lv in levels], fontsize=8.5)
ax1.set_xlim(0, max(wa + wb) * 1.8)
ax1.set_xlabel("USDT")
ax1.set_title("What the imbalance reads (top 3 levels)")
ax1.text(0.5, -0.2, f"$V^b$ = {e['volume_bid']:,.2f},  $V^a$ = {e['volume_ask']:,.2f},  "
         f"$I$ = ({e['volume_bid']:,.2f} − {e['volume_ask']:,.2f}) / {e['volume_bid'] + e['volume_ask']:,.2f} = {e['imbalance']:+.3f}",
         transform=ax1.transAxes, fontsize=9, color=INK, va="top", ha="center",
         bbox=dict(boxstyle="round,pad=0.4", facecolor="#eef3f8", edgecolor="none"))
ax1.legend(loc="lower right", fontsize=8.5)
ax1.grid(axis="y", visible=False)

b0, a0 = levels[0]["bid"], levels[0]["ask"]
# (label, price, colour, x of the bar, vertical offset of the label in rial)
marks = [
    ("best bid $b_0$", b0, BID, 0.03, -170), ("best ask $a_0$", a0, ASK, 0.03, 0),
    ("mid $m$", e["mid"], MUTED, 0.27, 230), ("microprice", e["microprice"], MICRO, 0.27, -230),
    ("centre $c$", e["centre"], ACCENT, 0.53, -260), ("bid sent $B$", e["bid"], BID, 0.80, 0), ("ask sent $A$", e["ask"], ASK, 0.80, 0),
]
for label, price, color, x0, dy in marks:
    ax2.hlines(price, x0, x0 + 0.15, color=color, linewidth=2.6)
    text = f"{label}  {price:,.2f}" if price % 1 else f"{label}  {rial(price)}"
    ax2.text(x0 + 0.005 if dy else x0 + 0.17, price + dy, text, va="center", fontsize=8.2, color=color)
ax2.annotate("", xy=(0.60, e["centre"]), xytext=(0.36, e["microprice"]),
             arrowprops=dict(arrowstyle="->", color=ACCENT, lw=1.3))
ax2.text(0.30, (e["centre"] + e["microprice"]) / 2 + 150, f"inventory skew × {e['skew_factor']:.6f}", fontsize=8, color=ACCENT,
         rotation=0)
for target in (e["bid"], e["ask"]):
    ax2.annotate("", xy=(0.80, target), xytext=(0.68, e["centre"]), arrowprops=dict(arrowstyle="->", color=INK, lw=1.0))
ax2.text(0.70, e["centre"] + 1150, f"± h = {e['h'] * 100:.4f} %,\nrounded outward to 10 rial", fontsize=8, color=INK)
ax2.set_xlim(0, 1.32)
lo, hi = min(b0, e["bid"]) - 700, e["ask"] + 700
ax2.set_ylim(lo, hi)
ax2.set_xticks([])
ax2.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
ax2.set_ylabel("price (rial per USDT)")
ax2.set_title("Where the prices go")
t = datetime.fromisoformat(m["t"])
fig.suptitle(f"Example 1: USDT/IRT book of {t:%d %b %Y %H:%M:%S} UTC, inventory {m['inventory']:.4f} of 0–300 USDT (x = {e['position']:.3f})",
             fontsize=10.5, color=INK, y=1.02)
save(fig, "mm2_example1.png")

# ------------------------------------------------------------ 2. the fee bar
fig, ax = plt.subplots(figsize=(8.6, 4.2))
h = np.linspace(0, 0.0035, 400)
for row, color in ((DATA["fee_bar"][1], BID), (DATA["fee_bar"][0], ASK)):
    f, r = row["f"], row["r_min"]
    R = (1 - f) ** 2 * (1 + h) / (1 - h) - 1
    ax.plot(h * 100, R * 100, color=color, linewidth=2.2, label=f"{row['label']}: f = {f * 100:.2f} %, $r_{{\\min}}$ = {r * 100:.2f} %")
    ax.plot([row["h_min"] * 100], [r * 100], "o", color=color, markersize=7, zorder=5)
    ax.annotate(f"$h_{{\\min}}$ = {row['h_min'] * 100:.4f} %", xy=(row["h_min"] * 100, r * 100),
                xytext=(row["h_min"] * 100 + 0.012, r * 100 - 0.16), fontsize=9, color=color,
                arrowprops=dict(arrowstyle="-", color=color, lw=0.8))
ax.axhline(0.01, color=INK, linewidth=1, linestyle="--")
ax.text(0.115, -0.045, "required net return $r_{\\min}$ = 0.01 % (dashed)", fontsize=8.5, color=INK)
ax.axhline(0, color="#b9b4a8", linewidth=0.8)
ax.set_xlabel("half-spread h (% of the centre)")
ax.set_ylabel("net return of a completed round trip R(h), %")
ax.set_title("A pair at c(1 − h) and c(1 + h) clears the bar exactly when h > G/(2 + G)")
ax.legend(loc="upper left", fontsize=8.5)
ax.set_xlim(0, 0.35)
ax.set_ylim(-0.55, 0.25)
save(fig, "mm2_fee_bar.png")


# ------------------------------------------------------------ 4. the 3 October signal
def times(series):
    return [datetime.fromisoformat(s["t"]) for s in series]


def with_breaks(ts, values, gap_s=30):
    """Insert a gap (NaN) where two books are more than gap_s apart, so no line is drawn across
    the minutes the collector did not record."""
    xs, ys = [], []
    for i, (t, v) in enumerate(zip(ts, values)):
        if i and (t - ts[i - 1]).total_seconds() > gap_s:
            xs.append(ts[i - 1])
            ys.append(float("nan"))
        xs.append(t)
        ys.append(v)
    return xs, ys


fig, axes = plt.subplots(2, 2, figsize=(11, 5.6), sharex="col")
for col, key, title in ((0, "usdt_3oct", "USDT/IRT"), (1, "btc_3oct", "BTC/IRT")):
    b = DATA["behaviour"][key]
    ts = times(b["series"])
    imb = [s["I"] for s in b["series"]]
    off = [s["micro_off"] * 100 for s in b["series"]]
    ax = axes[0][col]
    ax.plot(*with_breaks(ts, imb), color=ACCENT, linewidth=0.9)
    for lim in (0.8, -0.8):
        ax.axhline(lim, color=MUTED, linewidth=0.8, linestyle="--")
    ax.set_ylim(-1.05, 1.05)
    ax.set_title(f"{title}: imbalance I (clipped at ±0.8)")
    ax = axes[1][col]
    ax.plot(*with_breaks(ts, off), color=MICRO, linewidth=0.9)
    hmin = DATA["fee_bar"][1]["h_min"] * 100
    for lim in (hmin, -hmin):
        ax.axhline(lim, color=ASK, linewidth=0.9, linestyle="--")
    ax.text(ts[3], hmin + 0.006, f"± demo half-spread {hmin:.3f} %", fontsize=8, color=ASK)
    ax.set_title(f"{title}: microprice − mid, % of the mid")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.set_xlabel("3 October 2026, UTC")
save(fig, "mm2_signal_3oct.png")

# ------------------------------------------------------------ 5. beta
fig, axes = plt.subplots(1, 2, figsize=(11, 4.3))
for ax, pair, title in ((axes[0], "USDTIRT", "USDT/IRT"), (axes[1], "BTCIRT", "BTC/IRT")):
    fit = next(x for x in DATA["beta"][pair]["clipped"] if x["tau_s"] == 600)
    pts = np.array(fit["points"])
    ax.scatter(pts[:, 0], pts[:, 1] * 100, s=9, color=ACCENT, alpha=0.45, edgecolors="none")
    xs = np.linspace(-0.8, 0.8, 10)
    ax.plot(xs, fit["beta"] * xs * 100, color=ASK, linewidth=2.2)
    ax.axhline(0, color="#b9b4a8", linewidth=0.8)
    ax.axvline(0, color="#b9b4a8", linewidth=0.8)
    ax.set_xlabel("imbalance $I_t$")
    ax.set_ylabel("mid change over the next 10 minutes, %")
    ax.set_title(f"{title}\n$\\hat\\beta$ = {fit['beta']:.2e},  t = {fit['t_hac']:.2f} (Newey–West),  {fit['n']} pairs", fontsize=10)
save(fig, "mm2_beta.png")

# ------------------------------------------------------------ 6. where the prices sit against the touch
fig, axes = plt.subplots(1, 2, figsize=(11, 4.1), sharey=True)
for ax, key, title in ((axes[0], "usdt_3oct", "USDT/IRT"), (axes[1], "btc_3oct", "BTC/IRT")):
    b = DATA["behaviour"][key]
    behind_bid = [-s["bid_vs_b0_pct"] * 100 for s in b["series"]]
    behind_ask = [s["ask_vs_a0_pct"] * 100 for s in b["series"]]
    bins = np.linspace(-0.12, 0.12, 49)
    ax.hist(np.clip(behind_bid, -0.12, 0.12), bins=bins, color=BID, alpha=0.65, label="bid: (b₀ − B)/m")
    ax.hist(np.clip(behind_ask, -0.12, 0.12), bins=bins, color=ASK, alpha=0.55, label="ask: (A − a₀)/m")
    ax.axvline(0, color=INK, linewidth=1.2)
    ax.text(-0.118, ax.get_ylim()[1] * 0.92, "← improves\nthe best price", fontsize=8.5, color=INK, va="top")
    ax.text(0.118, ax.get_ylim()[1] * 0.92, "behind the\nbest price →", fontsize=8.5, color=INK, va="top", ha="right")
    ax.set_title(f"{title}, 3 October\n{b['bid_improves'] * 100:.1f} % of bids and {b['ask_improves'] * 100:.1f} % of asks improve the best price", fontsize=10)
    ax.legend(loc="center left", fontsize=8.5)
axes[0].set_ylabel("books")
fig.supxlabel("distance behind the best price on its own side, % of the mid (values beyond ±0.12 % are put in the end bins)",
              fontsize=9.5, color=INK)
save(fig, "mm2_quote_position.png")
