"""Reduced-form intraday market for the broker simulator: for each symbol and day a 1-minute mid path bridged from the
real open to the real close with the realised daily volatility, the real daily volume spread over a U-shaped curve, a
spread from the liquidity rule, and a square-root impact model (temporary per child order, permanent on the cumulative
executed quantity).  Aggressive child orders fill up to a share of the minute's volume; the rest stays with the algo."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .portfolio import session_times


@dataclass
class SymbolDay:
    symbol: str; exchange: str; t_open: float; t_close: float; n_min: int; open_px: float; close_px: float; volume: float; vol_day: float; spread_bp: float
    mid: np.ndarray; vol_curve: np.ndarray; consumed: np.ndarray; perm_shift: float = 0.0; executed: float = 0.0; buy_pressure: float = 0.0
    fills: list = field(default_factory=list); cum_vol: np.ndarray = None; cum_pv: np.ndarray = None

    def __post_init__(self):
        self.cum_vol = np.concatenate([[0.0], np.cumsum(self.vol_curve)]); self.cum_pv = np.concatenate([[0.0], np.cumsum(self.vol_curve * self.mid)])

    def minute(self, t: float) -> int:
        k = int((t - self.t_open) // 60)
        return 0 if k < 0 else (self.n_min - 1 if k >= self.n_min else k)

    def mid_at(self, t: float) -> float:
        return float(self.mid[self.minute(t)] + self.perm_shift)

    def is_open(self, t: float) -> bool:
        return self.t_open <= t < self.t_close

    def vwap(self, t0: float, t1: float) -> float:
        a, b = self.minute(t0), self.minute(t1)
        v = self.cum_vol[b + 1] - self.cum_vol[a]
        return float((self.cum_pv[b + 1] - self.cum_pv[a]) / v) if v > 0 else float(self.mid[a])

    def volume_between(self, t0: float, t1: float) -> float:
        a, b = self.minute(t0), self.minute(t1)
        return float(self.cum_vol[b + 1] - self.cum_vol[a])


def u_shape(n: int) -> np.ndarray:
    x = np.linspace(0, 1, n)
    w = 1.0 + 2.5 * np.exp(-x / 0.08) + 3.5 * np.exp(-(1 - x) / 0.06) + 0.3 * np.cos(2 * np.pi * x) ** 2
    return w / w.sum()


class DayMarket:
    def __init__(self, d: dt.date, bars_today: pd.DataFrame, stats: pd.DataFrame, securities: pd.DataFrame, rng: np.random.Generator, kappa: float = 0.6, psi: float = 0.5, fill_cap: float = 0.5):
        self.d, self.rng, self.kappa, self.psi, self.fill_cap = d, rng, kappa, psi, fill_cap
        sec = securities.set_index("symbol") if "symbol" in securities.columns else securities
        self.symbols: dict[str, SymbolDay] = {}
        for _, b in bars_today.iterrows():
            sym = b["symbol"]
            if sym not in stats.index or sym not in sec.index:
                continue
            ex = sec.loc[sym, "exchange"]; t_open, t_close = session_times(ex, d); n = int((t_close - t_open) // 60)
            if n < 30 or b["volume"] <= 0 or b["open"] <= 0 or b["close"] <= 0:
                continue
            st = stats.loc[sym]; vol_day = max(float(st["vol_day"]), 0.004)
            # Brownian bridge in log price from the real open to the real close
            steps = rng.normal(0, vol_day / np.sqrt(n), n - 1); w = np.concatenate([[0.0], np.cumsum(steps)]); x = np.arange(n) / max(n - 1, 1)
            bridge = w - x * w[-1]; logp = np.log(b["open"]) + x * (np.log(b["close"]) - np.log(b["open"])) + bridge   # exactly the open at the first minute and the close at the last
            mid = np.exp(logp)
            curve = u_shape(n) * float(b["volume"]) * np.exp(rng.normal(0, 0.15, n) - 0.5 * 0.15 ** 2); curve *= float(b["volume"]) / curve.sum()
            self.symbols[sym] = SymbolDay(sym, ex, t_open, t_close, n, float(b["open"]), float(b["close"]), float(b["volume"]), vol_day, float(st["spread_bp"]), mid, curve, np.zeros(n))

    def has(self, sym: str) -> bool:
        return sym in self.symbols

    def execute(self, sym: str, side: int, qty: float, t: float, quality: float = 1.0, at_close: bool = False) -> tuple[float, float, float]:
        """Fill up to `qty` of an aggressive child at time t.  Returns (filled, avg price, impact in bp vs the mid).
        quality > 1 means a worse broker (more impact for the same size)."""
        s = self.symbols[sym]
        if not s.is_open(t) and not at_close:
            return 0.0, 0.0, 0.0
        k = s.minute(t); vmin = s.vol_curve[k]; avail = max(0.0, self.fill_cap * vmin - s.consumed[k])
        if at_close:
            avail = max(avail, 0.02 * s.volume)      # closing auction depth
        filled = min(qty, np.floor(avail))
        if filled < 1:
            return 0.0, 0.0, 0.0
        mid = s.mid_at(t) if not at_close else s.close_px + s.perm_shift
        temp_bp = 0.5 * s.spread_bp + self.kappa * quality * s.vol_day * 1e4 * np.sqrt(filled / max(vmin, 1.0)) * (0.3 if at_close else 1.0)
        px = mid * (1 + side * temp_bp * 1e-4)
        # permanent impact on the cumulative executed quantity, square root in the day's volume
        before = self.psi * s.vol_day * mid * np.sqrt(abs(s.buy_pressure) / max(s.volume, 1.0)) * np.sign(s.buy_pressure)
        s.buy_pressure += side * filled
        after = self.psi * s.vol_day * mid * np.sqrt(abs(s.buy_pressure) / max(s.volume, 1.0)) * np.sign(s.buy_pressure)
        s.perm_shift += after - before
        s.consumed[k] += filled; s.executed += filled; s.fills.append((t, side, filled, px))
        return float(filled), float(px), float(temp_bp)

    def close_price(self, sym: str) -> float:
        s = self.symbols[sym]; return s.close_px + s.perm_shift

    def minutes_table(self) -> pd.DataFrame:
        rows = []
        for sym, s in self.symbols.items():
            for k in range(0, s.n_min, 5):
                rows.append((self.d.isoformat(), sym, k, float(s.mid[k]), float(s.vol_curve[k:k + 5].sum())))
        return pd.DataFrame(rows, columns=["date", "symbol", "minute", "mid", "volume"])
