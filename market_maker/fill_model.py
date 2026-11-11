"""Fill-probability model for MM_algo1v2, and the fill-intensity fit behind
MM_algo3's kappa (``FillTable.fit_intensity``, docs/MM_algo3.md section 6.1).

Answers one question for the quote-selection rule: *if an order rests at
relative distance ``delta`` from the quote centre, what is the probability
that it is filled within one order lifetime (``cancel_after_minutes``)?*

Two sources, used in this order:

1. A measured table ``fill_table_<pair>.json`` written by
   ``tools/build_fill_table.py`` from the project's own price history and
   order outcomes (see the MM_algo1v2 document,
   https://github.com/Alireza77n/Limit-Order-Book/blob/MM_algo1v2/docs/MM_algo1v2.md). It is reloaded
   whenever the file changes, so the daily rebuild reaches a running worker
   without a restart.
2. A parametric fallback when the table is missing, stale, or a bucket has
   too few observations: fills arrive as a Poisson process whose intensity
   decays exponentially with depth,

       p(delta) = 1 - exp(-lambda_tau * exp(-kappa * delta)),

   with ``kappa`` the decay per unit of relative distance and ``lambda_tau``
   the expected number of fills at zero distance during one lifetime.

The model is pure: no network, no clock other than the table file's mtime.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

SIDES = ("bid", "ask")


@dataclass(frozen=True)
class FillBucket:
    distance: float      # relative distance from the centre (fraction of price)
    p_bid: float         # P(bid at this distance fills within the horizon)
    p_ask: float
    n_bid: int           # observations behind p_bid (0 = none)
    n_ask: int


@dataclass(frozen=True)
class FillTable:
    pair: str
    horizon_seconds: float
    updated_at: Optional[datetime]
    method: str
    buckets: List[FillBucket]

    @classmethod
    def from_document(cls, document: Dict[str, Any]) -> "FillTable":
        if not isinstance(document, dict):
            raise ValueError("fill table must be a JSON object")
        raw_buckets = document.get("buckets")
        if not isinstance(raw_buckets, list) or not raw_buckets:
            raise ValueError("fill table has no buckets")
        buckets = []
        for raw in raw_buckets:
            try:
                bucket = FillBucket(
                    distance=float(raw["distance"]),
                    p_bid=_clamp01(float(raw.get("p_bid", raw.get("p", 0.0)))),
                    p_ask=_clamp01(float(raw.get("p_ask", raw.get("p", 0.0)))),
                    n_bid=max(0, int(raw.get("n_bid", raw.get("n", 0)))),
                    n_ask=max(0, int(raw.get("n_ask", raw.get("n", 0)))),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid fill-table bucket: {raw!r}") from exc
            if not math.isfinite(bucket.distance) or bucket.distance <= 0:
                raise ValueError(f"invalid fill-table distance: {bucket.distance!r}")
            buckets.append(bucket)
        buckets.sort(key=lambda b: b.distance)
        updated_at = document.get("updated_at")
        parsed = None
        if isinstance(updated_at, str) and updated_at:
            try:
                parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                parsed = None
        return cls(
            pair=str(document.get("pair") or ""),
            horizon_seconds=float(document.get("horizon_seconds") or 0.0),
            updated_at=parsed,
            method=str(document.get("method") or "unknown"),
            buckets=buckets,
        )

    def fit_intensity(self, min_samples: int) -> Optional[Dict[str, float]]:
        """The fill-intensity parameters of the table: with
        P(fill within tau) = 1 - exp(-lambda e^{-kappa d} tau), the regression
        ln(-ln(1 - P) / tau) = ln lambda - kappa d over every side and bucket
        with at least ``min_samples`` observations and 0 < P < 1 gives
        ``kappa`` (decay per unit of relative distance, minus the slope) and
        ``lambda`` (fills per second at zero distance). None when fewer than
        two distinct distances qualify or the slope is not negative."""
        tau = float(self.horizon_seconds or 0.0)
        if tau <= 0:
            return None
        xs, ys = [], []
        for bucket in self.buckets:
            for p, n in ((bucket.p_bid, bucket.n_bid), (bucket.p_ask, bucket.n_ask)):
                if n >= min_samples and 0.0 < p < 1.0:
                    xs.append(bucket.distance)
                    ys.append(math.log(-math.log(1.0 - p) / tau))
        if len(xs) < 2 or len(set(xs)) < 2:
            return None
        xbar, ybar = sum(xs) / len(xs), sum(ys) / len(ys)
        sxx = sum((x - xbar) ** 2 for x in xs)
        sxy = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys))
        if sxx <= 0:
            return None
        slope = sxy / sxx
        kappa = -slope
        if not (math.isfinite(kappa) and kappa > 0):
            return None
        return {"lambda": math.exp(ybar - slope * xbar), "kappa": kappa,
                "points": float(len(xs)), "horizon_seconds": tau}

    def lookup(self, distance: float, side: str, min_samples: int) -> Optional[float]:
        """Linear interpolation between bucket centres; ``None`` when the
        nearest buckets do not carry ``min_samples`` observations."""
        if side not in SIDES:
            raise ValueError(f"unknown side: {side!r}")
        usable = [
            b for b in self.buckets
            if (b.n_bid if side == "bid" else b.n_ask) >= min_samples
        ]
        if not usable:
            return None
        value = lambda b: b.p_bid if side == "bid" else b.p_ask  # noqa: E731
        if distance <= usable[0].distance:
            return value(usable[0])
        if distance >= usable[-1].distance:
            return value(usable[-1])
        for lower, upper in zip(usable, usable[1:]):
            if lower.distance <= distance <= upper.distance:
                span = upper.distance - lower.distance
                if span <= 0:
                    return value(lower)
                weight = (distance - lower.distance) / span
                return value(lower) + weight * (value(upper) - value(lower))
        return value(usable[-1])


def _clamp01(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


def formula_probability(distance: float, kappa: float, lambda_tau: float) -> float:
    """Parametric fallback: Poisson fills with exponentially decaying intensity."""
    distance = max(0.0, float(distance))
    kappa = max(0.0, float(kappa))
    lambda_tau = max(0.0, float(lambda_tau))
    return _clamp01(1.0 - math.exp(-lambda_tau * math.exp(-kappa * distance)))


class FillModel:
    """Fill probabilities for one pair, from the measured table when it is
    fresh and well populated, else from the parametric formula.

    Reads these ``cfg`` fields (all admin-editable, hot):

    * ``fill_table_enabled`` -- use the table at all (False = formula only)
    * ``fill_table_max_age_hours`` -- older tables are treated as missing
    * ``fill_table_min_samples`` -- buckets with fewer observations fall back
    * ``fill_kappa``, ``fill_lambda_tau`` -- the fallback formula
    * ``cancel_after_minutes`` -- the horizon the probabilities refer to

    and the optional ``fill_table_path`` (default
    ``<results dir>/fill_table_<pair>.json``).
    """

    def __init__(self, cfg: Any, *, results_dir: Optional[str] = None,
                 now_provider=None) -> None:
        self.cfg = cfg
        self._results_dir = results_dir
        self._now = now_provider or (lambda: datetime.now(timezone.utc))
        self._table: Optional[FillTable] = None
        self._table_mtime: Optional[float] = None
        self._table_error: Optional[str] = None

    # ---------- table location / loading ----------
    def table_path(self) -> Optional[str]:
        explicit = getattr(self.cfg, "fill_table_path", None)
        if explicit:
            return str(explicit)
        directory = self._results_dir
        if directory is None:
            try:
                from config import RESULTS_DIR
                directory = RESULTS_DIR
            except Exception:  # pragma: no cover - config import never fails in-package
                return None
        pair = str(getattr(self.cfg, "pair", "") or "").lower()
        if not pair:
            return None
        return os.path.join(directory, f"fill_table_{pair}.json")

    def reload_if_changed(self) -> None:
        path = self.table_path()
        if not path or not os.path.exists(path):
            self._table, self._table_mtime, self._table_error = None, None, None
            return
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            self._table, self._table_mtime, self._table_error = None, None, "unreadable"
            return
        if self._table is not None and mtime == self._table_mtime:
            return
        try:
            with open(path, "r", encoding="utf-8") as stream:
                document = json.load(stream)
            self._table = FillTable.from_document(document)
            self._table_error = None
        except (OSError, ValueError) as exc:
            self._table = None
            self._table_error = f"{type(exc).__name__}: {exc}"
        self._table_mtime = mtime

    def _table_is_fresh(self) -> bool:
        if self._table is None or not bool(getattr(self.cfg, "fill_table_enabled", True)):
            return False
        max_age_hours = float(getattr(self.cfg, "fill_table_max_age_hours", 48.0))
        if max_age_hours <= 0 or self._table.updated_at is None:
            return max_age_hours <= 0
        age = self._now() - self._table.updated_at
        return age.total_seconds() <= max_age_hours * 3600.0

    # ---------- the two numbers the strategy asks for ----------
    def probability(self, distance: float, side: str) -> float:
        """P(fill within one lifetime) for an order at relative ``distance``."""
        self.reload_if_changed()
        if self._table_is_fresh():
            value = self._table.lookup(
                float(distance), side,
                int(getattr(self.cfg, "fill_table_min_samples", 50)),
            )
            if value is not None:
                return value
        return formula_probability(
            distance,
            getattr(self.cfg, "fill_kappa", 400.0),
            getattr(self.cfg, "fill_lambda_tau", 1.0),
        )

    def source(self, distance: float, side: str) -> str:
        """``"table"`` or ``"formula"`` for the same inputs as ``probability``."""
        self.reload_if_changed()
        if self._table_is_fresh():
            value = self._table.lookup(
                float(distance), side,
                int(getattr(self.cfg, "fill_table_min_samples", 50)),
            )
            if value is not None:
                return "table"
        return "formula"

    def intensity(self) -> Optional[Dict[str, float]]:
        """``FillTable.fit_intensity`` of the fresh table (lambda, kappa), or
        None when there is no usable table."""
        self.reload_if_changed()
        if not self._table_is_fresh():
            return None
        return self._table.fit_intensity(int(getattr(self.cfg, "fill_table_min_samples", 50)))

    def describe(self) -> Dict[str, Any]:
        """Status for diagnostics / the dashboard; no secrets, JSON-safe."""
        self.reload_if_changed()
        table = self._table
        return {
            "table_path": self.table_path(),
            "table_loaded": table is not None,
            "table_fresh": self._table_is_fresh(),
            "table_error": self._table_error,
            "table_updated_at": (
                table.updated_at.isoformat() if table is not None and table.updated_at else None
            ),
            "table_method": table.method if table is not None else None,
            "table_buckets": len(table.buckets) if table is not None else 0,
            "formula_kappa": float(getattr(self.cfg, "fill_kappa", 400.0)),
            "formula_lambda_tau": float(getattr(self.cfg, "fill_lambda_tau", 1.0)),
        }


def write_fill_table(path: str, *, pair: str, horizon_seconds: float, method: str,
                     buckets: Sequence[Dict[str, Any]], updated_at: Optional[datetime] = None,
                     extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Serialise a table atomically (``tools/build_fill_table.py`` uses it)."""
    stamp = updated_at or datetime.now(timezone.utc)
    document = {
        "pair": pair,
        "horizon_seconds": float(horizon_seconds),
        "updated_at": stamp.isoformat(),
        "method": method,
        "buckets": [dict(bucket) for bucket in buckets],
    }
    if extra:
        document.update(extra)
    FillTable.from_document(document)  # validate before writing
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temp = f"{path}.{os.getpid()}.tmp"
    with open(temp, "w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
    os.replace(temp, path)
    return document
