"""Calendars against known dates, the fixed-income rules against published contract dates, and the corporate-actions
engine on positions and orders."""
import datetime as dt

import numpy as np
import pandas as pd

from xops import calendar as cal, corpact, fi

D = dt.date


def test_nyse_rules_and_special_closures():
    assert cal.easter(2025) == D(2025, 4, 20) and cal.easter(2024) == D(2024, 3, 31)
    h = cal.nyse_holidays(2025)
    assert {D(2025, 1, 1), D(2025, 1, 9), D(2025, 1, 20), D(2025, 2, 17), D(2025, 4, 18), D(2025, 5, 26), D(2025, 6, 19), D(2025, 7, 4), D(2025, 9, 1), D(2025, 11, 27), D(2025, 12, 25)} == set(h)
    assert D(2022, 1, 1) not in cal.nyse_holidays(2022) and D(2021, 12, 31) not in cal.nyse_holidays(2021)   # Saturday New Year not observed
    assert D(2023, 1, 2) in cal.nyse_holidays(2023)                                                       # Sunday New Year observed Monday
    assert D(2012, 10, 29) in cal.nyse_holidays(2012) and D(2018, 12, 5) in cal.nyse_holidays(2018)
    assert cal.is_early_close("XNYS", D(2026, 11, 27)) and cal.is_early_close("XNYS", D(2026, 12, 24)) and not cal.is_early_close("XNYS", D(2021, 12, 24))
    assert cal.next_trading_day("XNYS", D(2026, 9, 4), 1) == D(2026, 9, 8)      # Labor Day skipped


def test_lse_rules():
    h = cal.lse_holidays(2026)
    assert {D(2026, 1, 1), D(2026, 4, 3), D(2026, 4, 6), D(2026, 5, 4), D(2026, 5, 25), D(2026, 8, 31), D(2026, 12, 25), D(2026, 12, 28)} == set(h)
    assert D(2022, 6, 2) in cal.lse_holidays(2022) and D(2022, 6, 3) in cal.lse_holidays(2022) and D(2022, 9, 19) in cal.lse_holidays(2022) and D(2023, 5, 8) in cal.lse_holidays(2023)
    assert D(2021, 12, 27) in cal.lse_holidays(2021) and D(2021, 12, 28) in cal.lse_holidays(2021)     # Christmas on a Saturday
    assert cal.session_minutes("XLON", D(2026, 12, 24)) == 270 and cal.session_minutes("XNYS", D(2026, 9, 16)) == 390


def test_futures_rules_match_published_dates():
    assert fi.futures_dates("ZN", 2025, 12)["last_trade"] == D(2025, 12, 19) and fi.futures_dates("ZN", 2025, 12)["first_notice"] == D(2025, 11, 28)
    assert fi.futures_dates("ZT", 2025, 12)["last_trade"] == D(2025, 12, 31)
    assert fi.futures_dates("ES", 2025, 12)["last_trade"] == D(2025, 12, 19)
    assert fi.futures_dates("SR3", 2025, 12)["last_trade"] == D(2026, 3, 17)
    assert fi.futures_dates("GC", 2025, 12)["last_trade"] == D(2025, 12, 29)
    assert fi.futures_dates("6E", 2025, 12)["last_trade"] == D(2025, 12, 15)
    assert fi.futures_dates("CL", 2026, 11)["last_trade"] == D(2026, 10, 20) and fi.futures_dates("NG", 2026, 11)["last_trade"] == D(2026, 10, 28)
    assert fi.front_contract("ZN", D(2026, 9, 16)) == "ZNZ26" and fi.front_contract("ES", D(2026, 9, 16)) == "ESU26"
    rc = fi.roll_checks({"ZNU26": 10}, D(2026, 9, 16)); assert rc[0]["status"] == "fail"
    rc = fi.roll_checks({"ESU26": 10}, D(2026, 9, 16)); assert rc[0]["status"] == "warn" and rc[0]["days_to_last_trade"] == 2


def test_option_cdx_imm():
    assert fi.option_expiry(2025, 4) == D(2025, 4, 17)      # Good Friday on the third Friday
    assert fi.option_expiry(2024, 3) == D(2024, 3, 15) and fi.imm_date(2026, 3) == D(2026, 3, 18)
    assert fi.cdx_roll_dates(2026) == [D(2026, 3, 20), D(2026, 9, 21)]


def test_tips_reference_cpi_interpolation():
    cpi = {(2026, 6): 330.0, (2026, 7): 333.0, (2026, 8): 336.0}
    assert abs(fi.reference_cpi(D(2026, 9, 1), cpi) - 330.0) < 1e-12
    assert abs(fi.reference_cpi(D(2026, 9, 16), cpi) - (330.0 + 15 / 30 * 3.0)) < 1e-12
    assert fi.index_ratio(D(2026, 9, 16), D(2026, 9, 1), cpi) == round(331.5 / 330.0, 5)


def ev(**kw):
    base = {"symbol": "X", "ex_date": "2026-09-16", "type": "split", "amount": np.nan, "ratio": np.nan, "new_symbol": "", "source": "test", "note": ""}
    base.update(kw); return base


def test_positions_through_split_dividend_spinoff_merger_rename():
    events = pd.DataFrame([ev(symbol="NVDA", type="split", ratio=10.0), ev(symbol="KO", type="dividend", amount=0.51), ev(symbol="GE", type="spinoff", ratio=0.25, new_symbol="GEV"),
                           ev(symbol="ATVI", type="merger_cash", amount=95.0), ev(symbol="FB", type="symbol_change", new_symbol="META"), ev(symbol="ODD", type="split", ratio=1.5)])
    pos = {"NVDA": 100, "KO": 1000, "GE": 1001, "ATVI": 200, "FB": 50, "ODD": 7}
    prev = {"NVDA": 1200.0, "KO": 60.0, "GE": 160.0, "ATVI": 94.9, "FB": 180.0, "ODD": 30.0}
    r = corpact.apply_to_positions(pos, events, prev, 0.0, new_symbol_price={"GEV": 140.0})
    assert r.positions["NVDA"] == 1000                       # 10-for-1
    assert r.positions["GE"] == 1001 and r.positions["GEV"] == 250   # 1001/4 = 250.25 -> 250 whole shares + cash in lieu
    assert "ATVI" not in r.positions and "FB" not in r.positions and r.positions["META"] == 50
    assert r.positions["ODD"] == 10                          # 7 * 1.5 = 10.5 -> 10 + cash in lieu of half a share
    expected_cash = 1000 * 0.51 + 200 * 95.0 + 0.25 * 140.0 + 0.5 * 30.0 / 1.5
    assert abs(r.cash - expected_cash) < 1e-9
    # value invariance through the split: 100 x 1200 before = 1000 x 120 after
    assert abs(100 * 1200.0 - 1000 * (1200.0 / 10)) < 1e-9


def test_open_orders_on_ex_date():
    events = pd.DataFrame([ev(symbol="NVDA", type="split", ratio=10.0), ev(symbol="FB", type="symbol_change", new_symbol="META"), ev(symbol="ATVI", type="merger_cash", amount=95.0)])
    orders = [{"cl_ord_id": "a", "symbol": "NVDA", "qty": 100, "limit_px": 1200.0}, {"cl_ord_id": "b", "symbol": "FB", "qty": 10}, {"cl_ord_id": "c", "symbol": "ATVI", "qty": 5}, {"cl_ord_id": "d", "symbol": "KO", "qty": 5}]
    out, acts = corpact.apply_to_orders(orders, events)
    by = {o["cl_ord_id"]: o for o in out}
    assert by["a"]["qty"] == 1000 and abs(by["a"]["limit_px"] - 120.0) < 1e-9 and by["b"]["symbol"] == "META" and "c" not in by and by["d"]["qty"] == 5
    assert ("c", "cancelled", "merger_cash") in acts


def test_dividend_factor_matches_a_constructed_adjusted_series():
    dates = pd.bdate_range("2026-01-05", periods=10); close = np.full(10, 50.0)
    events = pd.DataFrame([ev(symbol="X", ex_date=str(dates[5].date()), type="dividend", amount=1.0)])
    f = corpact.adjustment_factors(pd.DataFrame({"date": dates, "close": close}), events)
    assert np.allclose(f.values[:5], 1 - 1.0 / 50.0) and np.allclose(f.values[5:], 1.0)
    px = pd.DataFrame({"symbol": "X", "date": dates, "close": close, "adjclose": close * f.values})
    r = corpact.validate_against_yahoo(px, events); assert r.n_events == 1 and r.n_matched == 1 and r.n_unexplained == 0
