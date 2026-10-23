"""The figures of docs/algorithms/MM_algo3.md, drawn from mm_algo3_doc_numbers.json and the recorded
mid series. Run from the repository root with market_maker on the path:
  PYTHONPATH=market_maker python docs/algorithms/figures/make_mm_algo3_figures.py"""
from __future__ import annotations

import gzip
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from strategy.algo4 import ou_fit  # noqa: E402

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
numbers = json.loads((DATA / "mm_algo3_doc_numbers.json").read_text(encoding="utf-8"))
BID, ASK, GREY, WARN = "#1f5f9f", "#c25e12", "#5d6572", "#a3271a"


def series(name):
    with gzip.open(DATA / name, "rt", encoding="utf-8") as stream:
        df = pd.read_csv(stream)
    df["t"] = pd.to_datetime(df["timestamp"]).astype("datetime64[ns]").astype("int64") / 1e9
    df["mid"] = (df["best_bid"] + df["best_ask"]) / 2.0
    return df


# ---- 1. the worked example's window: mids, the fitted mean, the lean and the quotes
usdt = series("mids_usdtirt_20261015_17.csv.gz")
example = numbers["example"]
end = pd.Timestamp(example["timestamp"]).timestamp()
window = usdt[(usdt["t"] > end - 1800) & (usdt["t"] <= end)]
case = example["cases"]["inventory_150"]
estimate = case["estimate"]
fig, ax = plt.subplots(figsize=(9, 4.2))
minutes = (window["t"] - end) / 60.0
ax.plot(minutes, window["mid"], color=GREY, lw=1.2, label="mid (10-s samples)")
ax.axhline(estimate["mu"], color=WARN, ls="--", lw=1.5, label=f"fitted mean μ̂ = {estimate['mu']:,.0f}")
ax.axhline(case["centre"], color="#1d7a4f", ls=":", lw=1.5, label=f"centre = mid + lean = {case['centre']:,.0f}")
ax.scatter([0], [case["mid"]], color="black", zorder=5, label=f"mid now {case['mid']:,.0f}")
ax.scatter([0, 0], [case["bid"], case["ask"]], color=[BID, ASK], marker="_", s=400, lw=3, zorder=5,
           label=f"quotes sent {case['bid']:,.0f} / {case['ask']:,.0f}")
ax.set_xlabel("minutes before 20:44:29, 15 October 2026 (USDT/IRT demo)")
ax.set_ylabel("rial")
ax.set_title(f"The window the model was fitted on: b̂ {estimate['b']:.4f}, t {estimate['t_stat']:.2f}, "
             f"half-life {estimate['half_life']:.0f} s → lean +{case['lean'] * 100:.3f} %")
ax.legend(loc="lower left", fontsize=8)
ax.grid(alpha=0.3)
fig.tight_layout()
fig.savefig(HERE / "mm3_window_fit.png", dpi=130)
plt.close(fig)


# ---- 2. the forecast against what happened: lean vs realised move over admissible windows (USDT)
rows = []
times, mids = usdt["t"].to_numpy(), usdt["mid"].to_numpy()
next_end = times[0] + 1800
i0 = 0
for i in range(len(times)):
    if times[i] < next_end:
        continue
    next_end = times[i] + 60
    while times[i0] < times[i] - 1800:
        i0 += 1
    estimate_i = ou_fit(list(zip(times[i0:i + 1], mids[i0:i + 1])), halflife_min=60.0, halflife_max=900.0)
    j = i
    while j + 1 < len(times) and times[j + 1] <= times[i] + 180:
        j += 1
    if times[j] - times[i] < 144 or not estimate_i.ok:
        continue
    lean = (estimate_i.mu - mids[i]) * (1 - math.exp(-estimate_i.theta * 180)) / mids[i] * 100
    rows.append((lean, (mids[j] - mids[i]) / mids[i] * 100))
fig, ax = plt.subplots(figsize=(5.6, 5))
ax.scatter([r[0] for r in rows], [r[1] for r in rows], s=12, color=BID, alpha=0.7)
lim = max(abs(v) for r in rows for v in r) * 1.05
ax.plot([-lim, lim], [-lim, lim], color=GREY, lw=1, ls="--", label="a perfect forecast")
ax.axhline(0, color="black", lw=0.6)
ax.axvline(0, color="black", lw=0.6)
ax.set_xlim(-lim, lim)
ax.set_ylim(-lim, lim)
skill = numbers["series"]["usdt"]["skill_corr_admissible"]
ax.set_xlabel("lean m: expected move over 180 s (% of price)")
ax.set_ylabel("realised move of the mid over 180 s (% of price)")
ax.set_title(f"USDT/IRT, 15–17 Oct 2026: {len(rows)} admissible windows, correlation {skill:+.3f}")
ax.legend(fontsize=8)
ax.grid(alpha=0.3)
fig.tight_layout()
fig.savefig(HERE / "mm3_forecast_skill.png", dpi=130)
plt.close(fig)


# ---- 3. the anatomy of the two depths in the worked example
fig, axes = plt.subplots(1, 2, figsize=(9, 3.8), sharey=True)
for ax, key, title in zip(axes, ("inventory_150", "inventory_50"), ("150 USDT held (x = 0.50)", "50 USDT held (x = 0.17)")):
    c = example["cases"][key]
    fee = example["settings"]["commission_perc"] * 100
    parts_bid = [("1/κ", c["base_depth"] * 100), ("− lean", -c["lean"] * 100), ("+ skew", c["skew"] * 100), ("+ fee", fee)]
    parts_ask = [("1/κ", c["base_depth"] * 100), ("+ lean", c["lean"] * 100), ("− skew", -c["skew"] * 100), ("+ fee", fee)]
    for x, parts, color in ((0, parts_bid, BID), (1, parts_ask, ASK)):
        bottom = 0.0
        for name, value in parts:
            ax.bar(x, value, bottom=bottom if value >= 0 else bottom + value, color=color,
                   alpha=0.35 if "fee" in name else (0.6 if "lean" in name or "skew" in name else 0.9),
                   edgecolor="white", width=0.55)
            ax.text(x + 0.33, bottom + value / 2, f"{name} {value:+.3f}", va="center", fontsize=7.5)
            bottom += value
        ax.text(x, bottom + 0.01, f"{bottom:.3f} %", ha="center", fontsize=9, fontweight="bold")
    ax.set_xticks([0, 1])
    ax.set_xticklabels(["bid depth δ⁻", "ask depth δ⁺"])
    ax.set_title(title, fontsize=10)
    ax.grid(axis="y", alpha=0.3)
axes[0].set_ylabel("% of the price")
fig.suptitle("The depths of the worked example: 1/κ ± lean ∓ skew + fee (relative to the mid)", fontsize=10)
fig.tight_layout()
fig.savefig(HERE / "mm3_depths.png", dpi=130)
plt.close(fig)
print("figures written:", [p.name for p in HERE.glob("mm3_*.png")])
