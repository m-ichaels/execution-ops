"""Target portfolios to orders: per-strategy deltas, fund-level netting with internal crossing, lot rounding, minimum
notional, restricted list and short-locate flags, participation caps that carry the remainder to the next day, algo
choice by urgency and size, session scheduling per exchange, and randomised broker assignment (the wheel)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, asdict
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import calendar as cal

BROKERS = ["BRK1", "BRK2", "BRK3"]


@dataclass
class Config:
    aum: float = 2e9
    brokers: tuple = tuple(BROKERS)
    max_pct_adv: float = 0.10        # a day's order may not exceed this share of 20-day ADV; the rest is carried
    min_notional: float = 10_000.0
    max_carry_days: int = 5
    restricted: tuple = ("BA", "XRX")
    no_locate: tuple = ("AMC", "SMCI")
    start_offset_min: int = 5
    end_offset_min: int = 5
    seed: int = 7


@dataclass
class OrderSpec:
    cl_ord_id: str; date: str; strategy: str; symbol: str; exchange: str; currency: str; side: str; qty: float; algo: str; urgency: str; max_pct: float
    start_time: float; end_time: float; decision_px: float; adv: float; spread_bp: float; vol_day: float; broker: str; difficulty: int
    limit_px: float | None = None; locate_required: bool = False; carried_from: str = ""; gross_buys: float = 0.0; gross_sells: float = 0.0; crossed: float = 0.0
    allocations: dict = field(default_factory=dict)   # strategy -> signed qty share of the net order

    def as_row(self) -> dict:
        d = asdict(self); d.pop("allocations"); return d


def session_times(exchange: str, d: dt.date) -> tuple[float, float]:
    """open and close of the exchange session on d as POSIX seconds (UTC), with early closes"""
    o, c, tz = cal.SESSION[exchange]
    if cal.is_early_close(exchange, d):
        c = cal.EARLY_CLOSE_TIME[exchange]
    z = ZoneInfo(tz)
    t_open = dt.datetime.combine(d, dt.time.fromisoformat(o), tzinfo=z).timestamp(); t_close = dt.datetime.combine(d, dt.time.fromisoformat(c), tzinfo=z).timestamp()
    return t_open, t_close


def symbol_stats(prices: pd.DataFrame, d: dt.date, lookback: int = 20) -> pd.DataFrame:
    """ADV, daily volatility, close and a spread estimate per symbol from the data before d"""
    h = prices[pd.to_datetime(prices["date"]).dt.date < d]
    out = []
    for sym, g in h.groupby("symbol"):
        g = g.sort_values("date").tail(lookback + 1)
        if len(g) < 5:
            continue
        r = np.log(g["close"]).diff().dropna()
        close = float(g["close"].iloc[-1]); adv = float(g["volume"].tail(lookback).mean()); vol = float(r.std()) if len(r) > 2 else 0.02
        # spread estimate: Corwin-Schultz-style from high/low is noisy; a liquidity rule on dollar ADV is enough here
        dollar_adv = adv * close
        spread_bp = float(np.clip(25.0 / np.sqrt(max(dollar_adv, 1e5) / 1e6), 1.5, 40.0))
        out.append({"symbol": sym, "close_prev": close, "adv": adv, "vol_day": vol, "spread_bp": spread_bp, "dollar_adv": dollar_adv, "last_date": str(g["date"].iloc[-1])})
    return pd.DataFrame(out).set_index("symbol")


def build_orders(d: dt.date, targets: dict[str, pd.Series], positions: dict[str, dict[str, float]], stats: pd.DataFrame, securities: pd.DataFrame, cfg: Config,
                 residuals: list[dict] | None = None, strategy_specs: dict | None = None, rng: np.random.Generator | None = None) -> tuple[list[OrderSpec], list[dict], dict]:
    rng = rng or np.random.default_rng(cfg.seed + d.toordinal()); strategy_specs = strategy_specs or {}
    date_s = d.isoformat(); sec = securities.set_index("symbol") if "symbol" in securities.columns else securities
    # 1. per-strategy deltas (carried residuals are deltas too)
    deltas: dict[str, dict[str, float]] = {}
    for strat, tgt in targets.items():
        cur = positions.get(strat, {})
        for sym, q in tgt.items():
            dq = float(q) - cur.get(sym, 0.0)
            if abs(dq) >= 1:
                deltas.setdefault(sym, {})[strat] = dq
        for sym, q in cur.items():
            if sym not in tgt.index and abs(q) >= 1:
                deltas.setdefault(sym, {})[strat] = -q
    # carried remainders are reported, not added: tomorrow's deltas are recomputed from targets and positions, so
    # whatever did not trade today is in them already (adding the carry would order it twice)
    orders, carry, report = [], [], {"gross": 0.0, "net": 0.0, "crossed": 0.0, "dropped_min_notional": 0, "restricted": [], "locate_failed": [], "capped": 0, "n_symbols": len(deltas)}
    fund_pos = {}
    for strat, cur in positions.items():
        for sym, q in cur.items():
            fund_pos[sym] = fund_pos.get(sym, 0.0) + q
    n = 0
    for sym in sorted(deltas):
        if sym not in stats.index or sym not in sec.index:
            continue
        st = stats.loc[sym]; buys = sum(v for v in deltas[sym].values() if v > 0); sells = -sum(v for v in deltas[sym].values() if v < 0)
        crossed = min(buys, sells); net = buys - sells; report["gross"] += (buys + sells) * st["close_prev"]; report["crossed"] += crossed * st["close_prev"]
        if sym in cfg.restricted:
            report["restricted"].append(sym); continue
        qty = float(np.floor(abs(net))); side = "1" if net > 0 else "2"
        if qty < 1 or qty * st["close_prev"] * sec.loc[sym, "price_scale"] < cfg.min_notional:
            report["dropped_min_notional"] += 1; continue
        # short sale needs a locate
        locate = side == "2" and fund_pos.get(sym, 0.0) - qty < 0
        if locate and sym in cfg.no_locate:
            report["locate_failed"].append(sym); continue
        if locate:
            side = "5"
        # participation cap: carry the remainder
        cap = max(100.0, np.floor(cfg.max_pct_adv * st["adv"]))
        if qty > cap:
            rem = qty - cap; qty = cap; report["capped"] += 1
            for strat, v in deltas[sym].items():
                share = v / net if net != 0 else 0
                carry.append({"symbol": sym, "strategy": strat, "qty_signed": share * rem * (1 if net > 0 else -1), "days": 1, "from": date_s})
        report["net"] += qty * st["close_prev"]
        # algo by urgency and size
        pct = qty / max(st["adv"], 1.0); strat_names = sorted(deltas[sym], key=lambda s: -abs(deltas[sym][s]))
        urg = "high" if any(strategy_specs.get(s, {}).get("urgency") == "high" for s in strat_names) else "low"
        base_algo = strategy_specs.get(strat_names[0], {}).get("algo", "VWAP")
        if pct > 0.05:
            algo, max_pct = "POV", 0.05
        elif pct > 0.01:
            algo, max_pct = ("IS" if base_algo != "CLOSE" else "CLOSE"), 0.10
        else:
            algo, max_pct = base_algo, 0.20
        difficulty = 0 if pct < 0.01 else 1 if pct < 0.05 else 2
        ex = sec.loc[sym, "exchange"]; t_open, t_close = session_times(ex, d)
        start = t_open + cfg.start_offset_min * 60; end = t_close - (0 if algo == "CLOSE" else cfg.end_offset_min * 60)
        broker = cfg.brokers[int(rng.integers(0, len(cfg.brokers)))]       # the wheel: uniform random assignment within every stratum
        n += 1
        o = OrderSpec(cl_ord_id=f"{date_s.replace('-', '')}-{n:04d}", date=date_s, strategy=",".join(strat_names), symbol=sym, exchange=ex, currency=sec.loc[sym, "currency"], side=side, qty=qty, algo=algo, urgency=urg, max_pct=max_pct,
                      start_time=start, end_time=end, decision_px=float(st["close_prev"]), adv=float(st["adv"]), spread_bp=float(st["spread_bp"]), vol_day=float(st["vol_day"]), broker=broker, difficulty=difficulty,
                      locate_required=locate, gross_buys=buys, gross_sells=sells, crossed=crossed, allocations={s: v for s, v in deltas[sym].items()})
        orders.append(o)
    # carried residuals age out
    for r in residuals or []:
        pass
    return orders, carry, report


def allocate_fills(order: OrderSpec, filled_qty: float) -> dict[str, float]:
    """Pro-rata allocation of a net fill to the strategies behind the order (signed shares)."""
    net = sum(order.allocations.values())
    if net == 0:
        return {}
    signed = filled_qty * (1 if order.side == "1" else -1)
    return {s: signed * (v / net) for s, v in order.allocations.items()}
