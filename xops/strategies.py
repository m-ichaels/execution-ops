"""Three systematic strategies that generate the fund's order flow from the daily data.  They are deliberately simple
(cross-sectional momentum, short-term reversal, inverse-volatility long-only): the point is realistic order sizes,
timing and turnover, not alpha.  Weights are in fractions of the strategy's capital; a positive weight is a long."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from . import calendar as cal


def panel(prices: pd.DataFrame, field: str = "adjclose") -> pd.DataFrame:
    p = prices.pivot(index="date", columns="symbol", values=field).sort_index()
    p.index = pd.to_datetime(p.index)
    return p


def momentum_weights(adj: pd.DataFrame, d: pd.Timestamp, region: pd.Series, top: float = 0.3) -> pd.Series:
    """12-1 momentum, long the top and short the bottom `top` fraction within each region, equal weight, gross 2."""
    hist = adj.loc[:d]
    if len(hist) < 260:
        return pd.Series(dtype=float)
    r = hist.iloc[-22] / hist.iloc[-252] - 1.0
    r = r.dropna()
    w = pd.Series(0.0, index=r.index)
    for reg in region.unique():
        names = [s for s in r.index if region.get(s) == reg]
        if len(names) < 6:
            continue
        rr = r[names].sort_values(); k = max(1, int(len(rr) * top))
        w[rr.index[-k:]] = 1.0 / k; w[rr.index[:k]] = -1.0 / k
    return w[w != 0]


def reversal_weights(adj: pd.DataFrame, d: pd.Timestamp, region: pd.Series, tail: float = 0.2) -> pd.Series:
    """5-day reversal: long the worst, short the best `tail` fraction within region; gross 1 (smaller book, daily)."""
    hist = adj.loc[:d]
    if len(hist) < 30:
        return pd.Series(dtype=float)
    r = (hist.iloc[-1] / hist.iloc[-6] - 1.0).dropna()
    w = pd.Series(0.0, index=r.index)
    for reg in region.unique():
        names = [s for s in r.index if region.get(s) == reg]
        if len(names) < 6:
            continue
        rr = r[names].sort_values(); k = max(1, int(len(rr) * tail))
        w[rr.index[:k]] = 0.5 / k; w[rr.index[-k:]] = -0.5 / k
    return w[w != 0]


def lowvol_weights(adj: pd.DataFrame, d: pd.Timestamp, region: pd.Series, n_hold: int = 60) -> pd.Series:
    """Long-only inverse-volatility weights on the `n_hold` least volatile names (60-day), gross 1."""
    hist = adj.loc[:d]
    if len(hist) < 70:
        return pd.Series(dtype=float)
    vol = np.log(hist.iloc[-61:]).diff().std().dropna(); vol = vol[vol > 0]
    pick = vol.sort_values().index[:n_hold]
    w = 1.0 / vol[pick]; w = w / w.sum()
    return w


STRATEGIES = {
    "MOM": {"fn": momentum_weights, "capital_share": 0.5, "rebalance": "monthly", "algo": "VWAP", "urgency": "low"},
    "REV": {"fn": reversal_weights, "capital_share": 0.2, "rebalance": "daily", "algo": "IS", "urgency": "high"},
    "LOWVOL": {"fn": lowvol_weights, "capital_share": 0.3, "rebalance": "weekly", "algo": "CLOSE", "urgency": "low"},
}


def is_rebalance_day(rule: str, d: dt.date, exchange: str = "XNYS") -> bool:
    if rule == "daily":
        return True
    if rule == "weekly":
        return d.weekday() == 0 or cal.next_trading_day(exchange, d, -1).weekday() > d.weekday()   # first trading day of the week
    if rule == "monthly":
        prev = cal.next_trading_day(exchange, d, -1)
        return prev.month != d.month
    raise ValueError(rule)


def targets_for(adj: pd.DataFrame, close: pd.DataFrame, region: pd.Series, d: dt.date, aum: float, prev_targets: dict[str, pd.Series] | None = None) -> dict[str, pd.Series]:
    """Target shares per strategy for trading day d, computed from data up to the previous close (the decision price).
    Strategies not rebalancing today keep their previous targets."""
    ts = pd.Timestamp(d); prev_day = adj.loc[:ts - pd.Timedelta(days=1)]
    if prev_day.empty:
        return {}
    dprev = prev_day.index[-1]; px = close.loc[dprev]
    out = {}
    for name, spec in STRATEGIES.items():
        if not is_rebalance_day(spec["rebalance"], d) and prev_targets and name in prev_targets:
            out[name] = prev_targets[name]; continue
        w = spec["fn"](adj, dprev, region)
        cap = aum * spec["capital_share"]
        shares = (w * cap / px.reindex(w.index)).replace([np.inf, -np.inf], np.nan).dropna()
        out[name] = shares.round()
    return out
