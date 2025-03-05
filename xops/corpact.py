"""Corporate actions: adjustment factors for price history, application to positions and open orders on the ex-date,
and the validation against Yahoo's adjusted close (which encodes every split and dividend Yahoo knows about, so
reproducing it event by event checks both the engine and the completeness of the event feed)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

SPLIT, DIVIDEND, SPINOFF, MERGER_CASH, MERGER_STOCK, SYMBOL_CHANGE, DELISTING = "split", "dividend", "spinoff", "merger_cash", "merger_stock", "symbol_change", "delisting"


# ---- price adjustment ------------------------------------------------------------------------------------------------
def event_factor(ev: pd.Series, prev_close: float) -> float:
    """multiplicative factor applied to all prices before the ex-date (CRSP / Yahoo convention)"""
    if ev["type"] == SPLIT:
        return 1.0 / float(ev["ratio"])
    if ev["type"] == DIVIDEND:
        return 1.0 - float(ev["amount"]) / prev_close if prev_close > 0 else 1.0
    return 1.0


def adjustment_factors(px: pd.DataFrame, events: pd.DataFrame) -> pd.Series:
    """cumulative factor per date so that adjclose = close * factor; px sorted by date with columns date, close"""
    dates = pd.to_datetime(px["date"]).values; close = px["close"].values
    f = np.ones(len(px))
    for _, ev in events.sort_values("ex_date").iterrows():
        ex = np.datetime64(pd.Timestamp(ev["ex_date"]))
        i = np.searchsorted(dates, ex)          # first index on/after the ex-date
        if i == 0 or i > len(px):
            continue
        prev = close[i - 1]
        f[:i] *= event_factor(ev, prev)
    return pd.Series(f, index=px.index)


def implied_factors(px: pd.DataFrame) -> pd.DataFrame:
    """day-to-day ratio of Yahoo's own adjustment (adjclose/close): != 1 exactly where Yahoo applied an event"""
    r = (px["adjclose"] / px["close"]).values
    jump = np.ones(len(px)); jump[1:] = r[:-1] / r[1:]      # factor applied to the day before, relative to the day
    out = px[["date", "close"]].copy(); out["implied_factor"] = jump
    return out


@dataclass
class ValidationResult:
    n_events: int = 0; n_matched: int = 0; n_unexplained: int = 0; max_rel_err: float = 0.0; median_rel_err: float = 0.0
    worst: list = field(default_factory=list); unexplained: list = field(default_factory=list); by_type: dict = field(default_factory=dict)


def validate_against_yahoo(prices: pd.DataFrame, events: pd.DataFrame, tol: float = 5e-4, min_price: float = 1.0, exchange_of: dict | None = None, div_scale: dict | None = None) -> ValidationResult:
    """For every dividend in the feed, compare the engine's factor with the factor implied by Yahoo's adjusted close on
    the ex-date (Yahoo's close is already split-adjusted, so splits leave no trace there and are checked elsewhere);
    list the days where Yahoo adjusted and the feed has no event (missing corporate actions).  `div_scale` rescales the
    dividend amount per exchange before the comparison, which is how the LSE unit mismatch is diagnosed."""
    res = ValidationResult(); errs = []; by_type: dict[str, list] = {}; exchange_of = exchange_of or {}; div_scale = div_scale or {}
    for sym, px in prices.groupby("symbol"):
        px = px.sort_values("date").reset_index(drop=True)
        if px["close"].min() < 1e-9:
            continue
        scale = div_scale.get(exchange_of.get(sym, ""), 1.0)
        ev = events[(events["symbol"] == sym) & (events["type"] == DIVIDEND)].copy(); ev["amount"] = ev["amount"] * scale
        imp = implied_factors(px); dates = pd.to_datetime(px["date"]).values
        explained = np.zeros(len(px), dtype=bool)
        for _, e in ev.iterrows():
            ex = np.datetime64(pd.Timestamp(e["ex_date"])); i = int(np.searchsorted(dates, ex))
            if i <= 0 or i >= len(px):
                continue
            # all events with the same ex-date combine multiplicatively
            same = ev[pd.to_datetime(ev["ex_date"]).values == dates[i]]
            f = 1.0
            for _, e2 in same.iterrows():
                f *= event_factor(e2, px["close"].iloc[i - 1])
            explained[i] = True
            got = imp["implied_factor"].iloc[i]
            rel = abs(got - f) / f
            # Yahoo rounds adjclose; the resolution of the implied factor is about 1e-4 / price
            errs.append((rel, sym, str(px["date"].iloc[i]), e["type"], f, got)); by_type.setdefault(exchange_of.get(sym, e["type"]), []).append(rel)
        # unexplained: Yahoo adjusted (factor away from 1 beyond rounding) with no event on that date
        noise = 2e-4 / np.maximum(px["close"].values, min_price) + 1e-6
        dev = np.abs(imp["implied_factor"].values - 1.0)
        for i in np.where((dev > np.maximum(noise * 20, 2e-3)) & (~explained))[0]:
            if px["close"].iloc[i] >= min_price:
                res.unexplained.append((sym, str(px["date"].iloc[i]), float(imp["implied_factor"].iloc[i])))
    if errs:
        e = np.array([x[0] for x in errs]); res.n_events = len(errs); res.n_matched = int((e <= tol).sum()); res.max_rel_err = float(e.max()); res.median_rel_err = float(np.median(e))
        res.worst = sorted(errs, reverse=True)[:12]
        res.by_type = {k: {"n": len(v), "within_tol": int((np.array(v) <= tol).sum()), "median": float(np.median(v)), "p99": float(np.quantile(v, 0.99))} for k, v in by_type.items()}
    res.n_unexplained = len(res.unexplained)
    return res


# ---- positions and orders on the ex-date --------------------------------------------------------------------------
@dataclass
class CAResult:
    positions: dict; cash: float; cash_in_lieu: float = 0.0; applied: list = field(default_factory=list); renamed: dict = field(default_factory=dict)


def apply_to_positions(positions: dict[str, float], events: pd.DataFrame, close_prev: dict[str, float], cash: float = 0.0, new_symbol_price: dict[str, float] | None = None) -> CAResult:
    """Apply the events with this ex-date to a position map (shares per symbol).  Fractional shares are paid in cash in
    lieu at the previous close (or the spun-off name's first close); cash dividends are booked as receivable on the
    ex-date; a cash merger closes the position; a symbol change renames it."""
    pos = dict(positions); res = CAResult(pos, cash)
    for _, e in events.sort_values(["symbol", "type"]).iterrows():
        sym = e["symbol"]; t = e["type"]; q = pos.get(sym, 0.0)
        if t == SYMBOL_CHANGE:
            new = e["new_symbol"]
            if sym in pos:
                pos[new] = pos.pop(sym) + pos.get(new, 0.0)
            res.renamed[sym] = new; res.applied.append((sym, t, new)); continue
        if q == 0:
            continue
        if t == SPLIT:
            r = float(e["ratio"]); newq = q * r; whole = np.floor(newq) if newq >= 0 else -np.floor(-newq)
            frac = newq - whole; cil = frac * close_prev.get(sym, 0.0) / r
            pos[sym] = whole; res.cash += cil; res.cash_in_lieu += cil; res.applied.append((sym, t, r))
        elif t == DIVIDEND:
            res.cash += q * float(e["amount"]); res.applied.append((sym, t, float(e["amount"])))
        elif t == SPINOFF:
            new = e["new_symbol"]; r = float(e["ratio"]); newq = q * r; whole = np.floor(newq) if newq >= 0 else -np.floor(-newq)
            frac = newq - whole; px_new = (new_symbol_price or {}).get(new, close_prev.get(new, 0.0)); cil = frac * px_new
            pos[new] = pos.get(new, 0.0) + whole; res.cash += cil; res.cash_in_lieu += cil; res.applied.append((sym, t, f"{whole:g} {new}"))
        elif t == MERGER_CASH:
            res.cash += q * float(e["amount"]); pos.pop(sym, None); res.applied.append((sym, t, float(e["amount"])))
        elif t == MERGER_STOCK:
            new = e["new_symbol"]; r = float(e["ratio"]); pos[new] = pos.get(new, 0.0) + q * r; pos.pop(sym, None); res.applied.append((sym, t, r))
        elif t == DELISTING:
            res.cash += q * close_prev.get(sym, 0.0); pos.pop(sym, None); res.applied.append((sym, t, ""))
    res.positions = {k: v for k, v in pos.items() if abs(v) > 1e-9}
    return res


def apply_to_orders(orders: list[dict], events: pd.DataFrame) -> tuple[list[dict], list[tuple]]:
    """Open (carried-over) orders on the ex-date: split -> quantity scaled and limit price divided, symbol change ->
    renamed, merger or delisting -> cancelled.  Returns the adjusted list and the actions taken."""
    out, actions = [], []
    ev_by_sym: dict[str, list] = {}
    for _, e in events.iterrows():
        ev_by_sym.setdefault(e["symbol"], []).append(e)
    for o in orders:
        o = dict(o); keep = True
        for e in ev_by_sym.get(o["symbol"], []):
            if e["type"] == SPLIT:
                r = float(e["ratio"]); o["qty"] = float(np.floor(o["qty"] * r))
                if o.get("limit_px"):
                    o["limit_px"] = o["limit_px"] / r
                actions.append((o["cl_ord_id"], "split_adjusted", r))
            elif e["type"] == SYMBOL_CHANGE:
                o["symbol"] = e["new_symbol"]; actions.append((o["cl_ord_id"], "renamed", e["new_symbol"]))
            elif e["type"] in (MERGER_CASH, MERGER_STOCK, DELISTING):
                keep = False; actions.append((o["cl_ord_id"], "cancelled", e["type"]))
        if keep and o["qty"] > 0:
            out.append(o)
    return out, actions


def events_on(events: pd.DataFrame, d: dt.date) -> pd.DataFrame:
    return events[pd.to_datetime(events["ex_date"]).dt.date == d]
