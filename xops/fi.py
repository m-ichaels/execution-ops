"""Fixed-income and derivatives calendars the daily checks use: CME futures first-notice and last-trading days (Treasury
notes and bonds, SOFR, equity index, energy, gold, FX), IMM dates, CDX series rolls, listed-option expiries, TBA
notification and settlement classes from the SIFMA table, and the TIPS reference CPI and index ratio from BLS CPI-U."""
from __future__ import annotations

import datetime as dt
import os

import pandas as pd

from . import calendar as cal

MONTH_CODES = "FGHJKMNQUVXZ"
QUARTERLY = (3, 6, 9, 12)


def _last_bd(y: int, m: int) -> dt.date:
    d = dt.date(y + (m == 12), m % 12 + 1, 1) - cal.ONE_DAY
    while not cal.is_trading_day("CME", d):
        d -= cal.ONE_DAY
    return d


def _bd_before(d: dt.date, n: int) -> dt.date:
    return cal.next_trading_day("CME", d, -n)


def imm_date(y: int, m: int) -> dt.date:
    """third Wednesday"""
    return cal.nth_weekday(y, m, 2, 3)


def futures_dates(root: str, y: int, m: int) -> dict:
    """first notice / last trading day for the delivery month (y, m) of a CME or NYMEX contract"""
    r = root.upper()
    if r in ("ZN", "TN", "ZB", "UB"):
        last_bd = _last_bd(y, m); return {"first_notice": _last_bd(y - (m == 1), m - 1 if m > 1 else 12), "last_trade": _bd_before(last_bd, 7), "delivery": last_bd}
    if r in ("ZT", "ZF"):
        return {"first_notice": _last_bd(y - (m == 1), m - 1 if m > 1 else 12), "last_trade": _last_bd(y, m), "delivery": cal.next_trading_day("CME", _last_bd(y, m), 3)}
    if r == "SR3":
        ny, nm = (y, m + 3) if m <= 9 else (y + 1, m - 9); end = imm_date(ny, nm)
        return {"first_notice": None, "last_trade": _bd_before(end, 1), "reference_start": imm_date(y, m), "reference_end": end}
    if r == "SR1":
        return {"first_notice": None, "last_trade": _last_bd(y, m)}
    if r in ("ES", "NQ", "RTY"):
        d = cal.nth_weekday(y, m, 4, 3)
        while not cal.is_trading_day("CME", d):
            d -= cal.ONE_DAY
        return {"first_notice": None, "last_trade": d}
    if r == "6E":
        return {"first_notice": None, "last_trade": _bd_before(imm_date(y, m), 2)}
    if r == "GC":
        return {"first_notice": _last_bd(y - (m == 1), m - 1 if m > 1 else 12), "last_trade": _bd_before(_last_bd(y, m), 2)}
    if r == "CL":
        py, pm = (y, m - 1) if m > 1 else (y - 1, 12); d = dt.date(py, pm, 25)
        while not cal.is_trading_day("CME", d):
            d -= cal.ONE_DAY
        lt = _bd_before(d, 3); return {"first_notice": cal.next_trading_day("CME", lt, 1), "last_trade": lt}
    if r == "NG":
        d = dt.date(y, m, 1)
        return {"first_notice": None, "last_trade": _bd_before(d, 3)}
    raise ValueError(f"unknown root {root}")


def contract_code(root: str, y: int, m: int) -> str:
    return f"{root}{MONTH_CODES[m - 1]}{y % 100:02d}"


def parse_contract(code: str) -> tuple[str, int, int]:
    root = code[:-3]; mc = code[-3]; yy = int(code[-2:]); return root, 2000 + yy, MONTH_CODES.index(mc) + 1


def cdx_roll_dates(year: int) -> list[dt.date]:
    """CDX and iTraxx series roll on 20 March and 20 September, the next business day when that is a weekend"""
    out = []
    for m in (3, 9):
        d = dt.date(year, m, 20)
        while d.weekday() >= 5:
            d += cal.ONE_DAY
        out.append(d)
    return out


def option_expiry(y: int, m: int) -> dt.date:
    """standard monthly listed-equity option expiration: third Friday, the preceding business day when it is a holiday"""
    d = cal.nth_weekday(y, m, 4, 3)
    while not cal.is_trading_day("XNYS", d):
        d -= cal.ONE_DAY
    return d


def weekly_expiries(y: int, m: int) -> list[dt.date]:
    out = []; d = dt.date(y, m, 1)
    while d.month == m:
        if d.weekday() == 4:
            e = d
            while not cal.is_trading_day("XNYS", e):
                e -= cal.ONE_DAY
            out.append(e)
        d += cal.ONE_DAY
    return out


TBA_CLASSES = {"A": "30-year UMBS, FNMA and FHLMC", "B": "15-year UMBS, FNMA and FHLMC", "C": "30-year GNMA", "D": "15-year GNMA, balloons, ARMs and other"}


def tba_calendar(path: str | None = None) -> pd.DataFrame:
    p = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "reference", "sifma_tba.csv")
    t = pd.read_csv(p); t["date"] = pd.to_datetime(t["date"]).dt.date; return t


def tba_dates(month: str, cls: str, table: pd.DataFrame | None = None) -> dict:
    t = table if table is not None else tba_calendar(); r = t[(t["settlement_month"] == month) & (t["class"] == cls)]
    return {row["date_type"]: row["date"] for _, row in r.iterrows()}


def cpi_table(path: str | None = None) -> dict[tuple[int, int], float]:
    p = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "reference", "cpi_u.csv")
    t = pd.read_csv(p); return {(int(r.year), int(r.month)): float(r.cpi_u_nsa) for r in t.itertuples()}


def reference_cpi(d: dt.date, cpi: dict[tuple[int, int], float]) -> float:
    """TIPS reference CPI: the CPI-U of the third month before, interpolated toward the second month before by the day
    of the month: Ref(d) = CPI(m-3) + (day-1)/days_in_month * (CPI(m-2) - CPI(m-3))"""
    def back(y, m, k):
        m -= k
        while m <= 0:
            m += 12; y -= 1
        return y, m
    y3, m3 = back(d.year, d.month, 3); y2, m2 = back(d.year, d.month, 2)
    c3, c2 = cpi[(y3, m3)], cpi[(y2, m2)]
    dim = (dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1) - dt.date(d.year, d.month, 1)).days
    return c3 + (d.day - 1) / dim * (c2 - c3)


def index_ratio(d: dt.date, issue: dt.date, cpi: dict[tuple[int, int], float]) -> float:
    return round(reference_cpi(d, cpi) / reference_cpi(issue, cpi), 5)


def roll_checks(futures_positions: dict[str, float], d: dt.date, warn_days: int = 5) -> list[dict]:
    """for each held contract: days to first notice and to the last trading day; a warning inside `warn_days`"""
    out = []
    for code, qty in futures_positions.items():
        if abs(qty) < 1e-9:
            continue
        root, y, m = parse_contract(code); fd = futures_dates(root, y, m)
        fn, lt = fd.get("first_notice"), fd.get("last_trade")
        d_fn = (fn - d).days if fn else None; d_lt = (lt - d).days if lt else None
        status = "ok"; detail = f"{code}: last trade {lt}" + (f", first notice {fn}" if fn else "")
        if (d_fn is not None and d_fn <= 0) or (d_lt is not None and d_lt <= 0):
            status = "fail"; detail += " - position held past first notice or expiry"
        elif (d_fn is not None and d_fn <= warn_days) or (d_lt is not None and d_lt <= warn_days):
            status = "warn"; detail += f" - roll within {min(x for x in (d_fn, d_lt) if x is not None)} days"
        out.append({"contract": code, "qty": qty, "days_to_first_notice": d_fn, "days_to_last_trade": d_lt, "status": status, "detail": detail})
    return out


def front_contract(root: str, d: dt.date, quarterly: bool = True) -> str:
    """the nearest contract whose last trading day is after d (and whose first notice, when it has one, is after d)"""
    y, m = d.year, d.month
    for _ in range(40):
        if not quarterly or m in QUARTERLY:
            fd = futures_dates(root, y, m)
            if fd["last_trade"] > d and (fd.get("first_notice") is None or fd["first_notice"] > d):
                return contract_code(root, y, m)
        m += 1
        if m == 13:
            m = 1; y += 1
    raise RuntimeError("no front contract")
