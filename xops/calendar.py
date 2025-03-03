"""Exchange calendars (NYSE, LSE, XETR / XPAR / XAMS, CME) with early closes and one-off closures, plus the date helpers
every other module uses.  The rules are written out rather than imported so that they can be validated against the
trading days actually present in the price data (see checks.calendar_vs_data)."""
from __future__ import annotations

import datetime as dt
from functools import lru_cache

ONE_DAY = dt.timedelta(days=1)


def easter(year: int) -> dt.date:
    """Anonymous Gregorian algorithm."""
    a = year % 19; b, c = divmod(year, 100); d, e = divmod(b, 4); f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30; i, k = divmod(c, 4); l = (32 + 2 * e + 2 * i - h - k) % 7; m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """n-th (1-based) given weekday (Mon=0) of the month; n=-1 for the last one."""
    if n > 0:
        d = dt.date(year, month, 1)
        d += dt.timedelta(days=(weekday - d.weekday()) % 7)
        return d + dt.timedelta(days=7 * (n - 1))
    d = dt.date(year + (month == 12), month % 12 + 1, 1) - ONE_DAY
    d -= dt.timedelta(days=(d.weekday() - weekday) % 7)
    return d


def observed_us(d: dt.date) -> dt.date | None:
    """US federal-style observance: Saturday -> Friday, Sunday -> Monday."""
    if d.weekday() == 5:
        return d - ONE_DAY
    if d.weekday() == 6:
        return d + ONE_DAY
    return d


def observed_uk(d: dt.date, taken: set[dt.date]) -> dt.date:
    """UK bank-holiday observance: weekend -> next weekday not already a holiday."""
    while d.weekday() >= 5 or d in taken:
        d += ONE_DAY
    return d


@lru_cache(maxsize=None)
def nyse_holidays(year: int) -> frozenset[dt.date]:
    h = set()
    ny = dt.date(year, 1, 1)
    if ny.weekday() == 6:
        h.add(ny + ONE_DAY)          # Sunday -> Monday; a Saturday New Year is not observed on the Friday
    elif ny.weekday() < 5:
        h.add(ny)
    if year >= 1998:
        h.add(nth_weekday(year, 1, 0, 3))   # Martin Luther King
    h.add(nth_weekday(year, 2, 0, 3))       # Presidents' Day
    h.add(easter(year) - dt.timedelta(days=2))   # Good Friday
    h.add(nth_weekday(year, 5, 0, -1))      # Memorial Day
    if year >= 2022:
        h.add(observed_us(dt.date(year, 6, 19)))   # Juneteenth
    h.add(observed_us(dt.date(year, 7, 4)))
    h.add(nth_weekday(year, 9, 0, 1))       # Labor Day
    h.add(nth_weekday(year, 11, 3, 4))      # Thanksgiving
    h.add(observed_us(dt.date(year, 12, 25)))
    special = {2001: ["09-11", "09-12", "09-13", "09-14"], 2004: ["06-11"], 2007: ["01-02"], 2012: ["10-29", "10-30"], 2018: ["12-05"], 2025: ["01-09"]}
    for md in special.get(year, []):
        h.add(dt.date.fromisoformat(f"{year}-{md}"))
    return frozenset(h)


@lru_cache(maxsize=None)
def nyse_early_closes(year: int) -> frozenset[dt.date]:
    e = set()
    e.add(nth_weekday(year, 11, 3, 4) + ONE_DAY)   # day after Thanksgiving
    j3, j4 = dt.date(year, 7, 3), dt.date(year, 7, 4)
    if j3.weekday() < 5 and j4.weekday() != 5:
        e.add(j3)
    c24, c25 = dt.date(year, 12, 24), dt.date(year, 12, 25)
    if c24.weekday() < 5 and c25.weekday() != 5:
        e.add(c24)
    return frozenset(e - set(nyse_holidays(year)))


@lru_cache(maxsize=None)
def lse_holidays(year: int) -> frozenset[dt.date]:
    h: set[dt.date] = set()
    h.add(observed_uk(dt.date(year, 1, 1), h))
    e = easter(year); h.add(e - dt.timedelta(days=2)); h.add(e + ONE_DAY)
    if year == 2020:
        h.add(dt.date(2020, 5, 8))          # VE Day 75: early May holiday moved to the Friday
    elif year == 1995:
        h.add(dt.date(1995, 5, 8))
    else:
        h.add(nth_weekday(year, 5, 0, 1))
    if year == 2022:
        h.add(dt.date(2022, 6, 2)); h.add(dt.date(2022, 6, 3))   # Platinum Jubilee: spring holiday moved, extra day
    elif year == 2012:
        h.add(dt.date(2012, 6, 4)); h.add(dt.date(2012, 6, 5))   # Diamond Jubilee
    else:
        h.add(nth_weekday(year, 5, 0, -1))
    h.add(nth_weekday(year, 8, 0, -1))
    c = observed_uk(dt.date(year, 12, 25), h); h.add(c); h.add(observed_uk(dt.date(year, 12, 26), h))
    special = {2011: ["04-29"], 2022: ["09-19"], 2023: ["05-08"]}
    for md in special.get(year, []):
        h.add(dt.date.fromisoformat(f"{year}-{md}"))
    return frozenset(h)


@lru_cache(maxsize=None)
def lse_early_closes(year: int) -> frozenset[dt.date]:
    e = set()
    for md in ("12-24", "12-31"):
        d = dt.date.fromisoformat(f"{year}-{md}")
        if d.weekday() < 5 and d not in lse_holidays(year):
            e.add(d)
    return frozenset(e)


@lru_cache(maxsize=None)
def euronext_holidays(year: int) -> frozenset[dt.date]:
    """Euronext Paris and Amsterdam: the TARGET closing days; 24 and 31 December are half days, not holidays."""
    e = easter(year)
    h = {dt.date(year, 1, 1), e - dt.timedelta(days=2), e + ONE_DAY, dt.date(year, 5, 1), dt.date(year, 12, 25), dt.date(year, 12, 26)}
    return frozenset(d for d in h if d.weekday() < 5)


@lru_cache(maxsize=None)
def xetra_holidays(year: int) -> frozenset[dt.date]:
    """Xetra: the TARGET closing days plus 24 and 31 December (the published Deutsche Börse trading calendar).  The
    vendor data disagrees on a few Whit Mondays and German Unity Days; checks.calendar_vs_data lists them."""
    e = easter(year)
    h = {dt.date(year, 1, 1), e - dt.timedelta(days=2), e + ONE_DAY, dt.date(year, 5, 1), dt.date(year, 12, 24), dt.date(year, 12, 25), dt.date(year, 12, 26), dt.date(year, 12, 31)}
    return frozenset(d for d in h if d.weekday() < 5)


@lru_cache(maxsize=None)
def euronext_early_closes(year: int) -> frozenset[dt.date]:
    return frozenset(d for d in (dt.date(year, 12, 24), dt.date(year, 12, 31)) if d.weekday() < 5)


@lru_cache(maxsize=None)
def cme_holidays(year: int) -> frozenset[dt.date]:
    """Days with no CME settlement (Globex closed or holiday-closed): the NYSE set is the exchange business-day
    calendar used for contract rules (first notice, last trading day)."""
    return nyse_holidays(year)


HOLIDAYS = {"XNYS": nyse_holidays, "XNAS": nyse_holidays, "XLON": lse_holidays, "XETR": xetra_holidays, "XPAR": euronext_holidays, "XAMS": euronext_holidays, "CME": cme_holidays}
EARLY = {"XNYS": nyse_early_closes, "XNAS": nyse_early_closes, "XLON": lse_early_closes, "XPAR": euronext_early_closes, "XAMS": euronext_early_closes}
SESSION = {"XNYS": ("09:30", "16:00", "America/New_York"), "XNAS": ("09:30", "16:00", "America/New_York"), "XLON": ("08:00", "16:30", "Europe/London"), "XETR": ("09:00", "17:30", "Europe/Berlin"), "XPAR": ("09:00", "17:30", "Europe/Paris"), "XAMS": ("09:00", "17:30", "Europe/Amsterdam"), "CME": ("18:00", "17:00", "America/Chicago")}
EARLY_CLOSE_TIME = {"XNYS": "13:00", "XNAS": "13:00", "XLON": "12:30", "XPAR": "14:05", "XAMS": "14:05"}


def is_holiday(exchange: str, d: dt.date) -> bool:
    return d in HOLIDAYS[exchange](d.year)


def is_trading_day(exchange: str, d: dt.date) -> bool:
    return d.weekday() < 5 and not is_holiday(exchange, d)


def is_early_close(exchange: str, d: dt.date) -> bool:
    f = EARLY.get(exchange)
    return bool(f) and d in f(d.year)


def next_trading_day(exchange: str, d: dt.date, n: int = 1) -> dt.date:
    step = ONE_DAY if n > 0 else -ONE_DAY
    for _ in range(abs(n)):
        d += step
        while not is_trading_day(exchange, d):
            d += step
    return d


def trading_days(exchange: str, start: dt.date, end: dt.date) -> list[dt.date]:
    out = []
    d = start
    while d <= end:
        if is_trading_day(exchange, d):
            out.append(d)
        d += ONE_DAY
    return out


def add_business_days(exchange: str, d: dt.date, n: int) -> dt.date:
    return next_trading_day(exchange, d, n) if n else d


def session_minutes(exchange: str, d: dt.date) -> int:
    o, c, _ = SESSION[exchange]
    if is_early_close(exchange, d):
        c = EARLY_CLOSE_TIME[exchange]
    oh, om = map(int, o.split(":")); ch, cm = map(int, c.split(":"))
    return (ch * 60 + cm) - (oh * 60 + om)
