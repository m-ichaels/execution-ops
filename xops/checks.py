"""Start-of-day and end-of-day checks: calendars, securities master, price coverage and staleness, unexplained jumps,
position and order limits, futures rolls, session state, open orders.  Each check returns rows for the `checks` table
with a status of ok / warn / fail and a detail string an operator can act on."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from . import calendar as cal, fi

FUTURES_OVERLAY = {"ES": 1, "ZN": -1}   # the book carries an equity-index hedge and a duration hedge in the front contracts


def calendar_vs_data(prices: pd.DataFrame, securities: pd.DataFrame, start: str = "2007-01-01") -> dict:
    """Trading days present in the data (a day with bars for at least half the exchange's names) against the calendar."""
    sec = securities.set_index("symbol"); out = {}
    for ex in sorted(sec["exchange"].unique()):
        syms = sec.index[sec["exchange"] == ex]; p = prices[(prices["symbol"].isin(syms)) & (prices["date"] >= start)]
        if p.empty:
            continue
        counts = p.groupby("date")["symbol"].nunique(); n = len(syms)
        data_days = {dt.date.fromisoformat(str(d)[:10]) for d, c in counts.items() if c >= max(1, n // 2)}
        first, last = min(data_days), max(data_days)
        cal_days = set(cal.trading_days(ex, first, last))
        thin = {dt.date.fromisoformat(str(d)[:10]) for d, c in counts.items() if c < max(1, n // 2)}
        open_no_data = sorted(cal_days - data_days); data_on_closed = sorted(data_days - cal_days)
        out[ex] = {"symbols": int(n), "from": first.isoformat(), "to": last.isoformat(), "calendar_days": len(cal_days), "data_days": len(data_days), "calendar_open_but_no_data": [d.isoformat() for d in open_no_data], "data_on_calendar_closed": [d.isoformat() for d in data_on_closed],
                   "thin_days": len(thin), "mismatches": len(open_no_data) + len(data_on_closed)}
    return out


def sod_checks(d: dt.date, state, stats: pd.DataFrame, orders: list, bars: pd.DataFrame, prices_hist: pd.DataFrame | None = None, securities: pd.DataFrame | None = None, events: pd.DataFrame | None = None, cfg=None) -> list[dict]:
    rows = []; date_s = d.isoformat()

    def add(name, status, detail):
        rows.append({"date": date_s, "phase": "SOD", "check_name": name, "status": status, "detail": detail})

    sec = securities.set_index("symbol") if securities is not None else None
    # calendars
    for ex in sorted(sec["exchange"].unique()) if sec is not None else ["XNYS"]:
        if not cal.is_trading_day(ex, d):
            add(f"calendar:{ex}", "warn", f"{ex} closed today ({'holiday' if d.weekday() < 5 else 'weekend'}): no orders will be sent there")
        elif cal.is_early_close(ex, d):
            add(f"calendar:{ex}", "warn", f"{ex} early close at {cal.EARLY_CLOSE_TIME.get(ex, '?')}: algo end times shortened")
        else:
            add(f"calendar:{ex}", "ok", f"{ex} open, full session")
    # securities master: every position and target must be a known symbol
    held = state.fund_positions(); unknown = [s for s in held if sec is not None and s not in sec.index]
    add("securities_master", "fail" if unknown else "ok", f"{len(unknown)} held symbols missing from the securities master: {unknown[:5]}" if unknown else f"{len(held)} held symbols all known")
    # price coverage and staleness for held names
    stale, missing = [], []
    for s in held:
        if s not in stats.index:
            missing.append(s); continue
        last = dt.date.fromisoformat(stats.loc[s, "last_date"]); ex = sec.loc[s, "exchange"] if sec is not None else "XNYS"
        if cal.is_trading_day(ex, d) and last < cal.next_trading_day(ex, d, -1):
            stale.append((s, last.isoformat()))
    add("price_coverage", "fail" if missing else ("warn" if stale else "ok"), (f"{len(missing)} held names without prices: {missing[:5]}; " if missing else "") + (f"{len(stale)} stale (last close before the previous session): {stale[:5]}" if stale else "all held names priced at the previous close"))
    # unexplained jumps: |return| beyond 6 sigma on the previous day with no corporate action
    if prices_hist is not None and events is not None:
        prev = cal.next_trading_day("XNYS", d, -1); jumps = []
        h = prices_hist[pd.to_datetime(prices_hist["date"]).dt.date <= prev]
        for s, g in h.groupby("symbol"):
            g = g.sort_values("date").tail(22)
            if len(g) < 10 or str(g["date"].iloc[-1])[:10] != prev.isoformat():
                continue
            r = np.log(g["close"]).diff().dropna(); sig = max(r.iloc[:-1].std(), 0.005)
            if abs(r.iloc[-1]) > 6 * sig:
                ev = events[(events["symbol"] == s) & (pd.to_datetime(events["ex_date"]).dt.date == prev)]
                if ev.empty:
                    jumps.append((s, f"{100 * r.iloc[-1]:+.1f}%"))
        add("unexplained_jumps", "warn" if jumps else "ok", f"{len(jumps)} names moved more than 6 sigma on {prev} with no corporate action: {jumps[:6]}" if jumps else f"no unexplained jump on {prev}")
    # corporate actions today
    if events is not None:
        ev = events[pd.to_datetime(events["ex_date"]).dt.date == d]; touched = [s for s in ev["symbol"] if s in held]
        add("corporate_actions", "warn" if touched else "ok", f"{len(ev)} events ex today, {len(touched)} on held names: {[(r.symbol, r.type) for r in ev[ev.symbol.isin(touched)].itertuples()][:6]}" if len(ev) else "no corporate action ex today")
    # limits: position notional and share of ADV
    aum = getattr(cfg, "aum", 2e9); big, illiq = [], []
    for s, q in held.items():
        if s in stats.index:
            notional = abs(q) * stats.loc[s, "close_prev"] * (sec.loc[s, "price_scale"] if sec is not None else 1)
            if notional > 0.05 * aum:
                big.append((s, round(notional / 1e6)))
            if abs(q) > 0.25 * stats.loc[s, "adv"]:
                illiq.append((s, round(abs(q) / stats.loc[s, "adv"], 2)))
    add("position_limits", "warn" if big or illiq else "ok", (f"{len(big)} positions above 5% of AUM: {big[:4]}; " if big else "") + (f"{len(illiq)} positions above 25% of ADV: {illiq[:4]}" if illiq else "") or "all positions inside the 5% AUM and 25% ADV limits")
    # orders
    n_cap = sum(1 for o in orders if o.qty > 0.10 * o.adv + 1); n_short = sum(1 for o in orders if o.side == "5")
    add("order_sanity", "fail" if n_cap else "ok", f"{len(orders)} orders, {n_short} short sales with locate flag, {n_cap} above the 10% ADV cap")
    # futures overlay rolls
    fut = {fi.front_contract(r, d, quarterly=True): q * 100 for r, q in FUTURES_OVERLAY.items()}   # the overlay is rolled on the trigger day; the check warns in the five days before
    for rc in fi.roll_checks(fut, d):
        add(f"futures_roll:{rc['contract']}", rc["status"], rc["detail"])
    # FIX sequence numbers persisted from the previous session
    for b, st in (state.seq_stores or {}).items():
        add(f"fix_sequence:{b}", "ok", f"resuming {b} at out {st.get('out', 1)} / in {st.get('in', 1)}")
    return rows


def eod_checks(d: dt.date, state, mon, session_stats: dict, orders: list) -> list[dict]:
    rows = []; date_s = d.isoformat()

    def add(name, status, detail):
        rows.append({"date": date_s, "phase": "EOD", "check_name": name, "status": status, "detail": detail})

    open_orders = [o for o in mon.book.orders.values() if not o.terminal]
    add("open_orders", "fail" if open_orders else "ok", f"{len(open_orders)} orders not terminal at end of day: {[o.chain[0] for o in open_orders][:5]}" if open_orders else "every order reached a terminal state")
    anomalies = sum(len(o.anomalies) for o in mon.book.orders.values())
    add("state_anomalies", "warn" if anomalies else "ok", f"{anomalies} execution reports rejected or resynced by the state machine" if anomalies else "no state-machine anomaly")
    for b, s in session_stats.items():
        bad = s.get("pending_gap") or s.get("link", {}).get("dropped", 0) > 0 or s.get("resent", 0) > 0 or s.get("gap_fills", 0) > 0
        add(f"session:{b}", "warn" if bad else "ok", f"sent {s['sent']}, received {s['received']}, resent {s['resent']}, gap fills {s['gap_fills']}, transport drops {s['link']['dropped']}, pending gap {s['pending_gap']}")
    crit = [a for a in mon.alerts if a["severity"] in ("high", "critical")]
    add("alerts", "warn" if crit else "ok", f"{len(mon.alerts)} alerts, {len(crit)} high or critical: " + ", ".join(sorted({a['rule'] for a in crit})) if mon.alerts else "no alerts")
    neg = [(s, q) for s, q in state.positions.get("LOWVOL", {}).items() if q < 0]
    add("long_only_book", "fail" if neg else "ok", f"LOWVOL is long-only but holds shorts: {neg[:4]}" if neg else "LOWVOL long-only book has no short position")
    return rows
