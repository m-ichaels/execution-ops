"""The day runner: start-of-day checks and corporate actions, targets to orders, FIX sessions to the simulated brokers
on a virtual clock (minute steps with event-driven delivery inside each minute), the live monitor, end-of-day
outputs (orders, executions, drop copy, positions) and the fault-injection scoring."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import calendar as cal
from . import corpact, faults as F, portfolio as P, strategies as S
from .broker import BrokerSim
from .fix import message as M
from .fix import orders as O
from .fix.session import Session
from .fix.transport import VirtualLink
from .market import DayMarket
from .monitor import Monitor, Thresholds

BROKER_QUALITY = {"BRK1": 0.85, "BRK2": 1.0, "BRK3": 1.25}
BROKER_ACK = {"BRK1": (0.02, 0.06), "BRK2": (0.03, 0.10), "BRK3": (0.08, 0.25)}


@dataclass
class FundState:
    positions: dict[str, dict[str, float]] = field(default_factory=dict)   # strategy -> symbol -> shares
    cash: float = 0.0
    residuals: list = field(default_factory=list)
    prev_targets: dict = field(default_factory=dict)
    seq_stores: dict = field(default_factory=dict)                        # broker -> {in, out}: persisted sequence numbers

    def fund_positions(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for cur in self.positions.values():
            for s, q in cur.items():
                out[s] = out.get(s, 0.0) + q
        return {k: v for k, v in out.items() if abs(v) > 1e-9}


@dataclass
class DayResult:
    date: str; orders: list; specs: dict; exec_rows: list; dropcopy_rows: list; alerts: list; detections: list; faults: dict; session_stats: dict; broker_stats: dict
    positions_sod: dict; positions_eod: dict; ca_applied: list; netting: dict; checks: list; market: DayMarket; cancel_times: dict; replaced: list; canceled: list; mon_orders: list = field(default_factory=list); rejected_reports: dict = field(default_factory=dict)


class Clock:
    def __init__(self, t0: float):
        self.t = t0

    def __call__(self) -> float:
        return self.t


def run_day(d: dt.date, prices: pd.DataFrame, securities: pd.DataFrame, state: FundState, cfg: P.Config, *, adj: pd.DataFrame, close: pd.DataFrame, region: pd.Series,
            events: pd.DataFrame, fault_mix: dict | None = None, seed: int = 1, thresholds: Thresholds | None = None, warm: bool = False, checks_fn=None) -> DayResult | None:
    """Run one trading day.  With warm=True the targets become the positions without trading (used to build the book
    before the simulation window)."""
    rng = np.random.default_rng(seed * 100003 + d.toordinal())
    date_s = d.isoformat(); bars = prices[pd.to_datetime(prices["date"]).dt.date == d]
    if bars.empty:
        return None
    stats = P.symbol_stats(prices, d)
    checks = []
    # ---- start of day: corporate actions on positions and carried orders ---------------------------------------------
    ev_today = corpact.events_on(events, d)
    ca_applied = []
    if len(ev_today):
        close_prev = stats["close_prev"].to_dict()
        for strat in list(state.positions):
            res = corpact.apply_to_positions(state.positions[strat], ev_today, close_prev, 0.0)
            state.positions[strat] = res.positions; state.cash += res.cash; ca_applied += [(strat,) + a for a in res.applied]
        state.residuals, acts = corpact.apply_to_orders([dict(r, cl_ord_id=r.get("from", ""), qty=abs(r["qty_signed"])) for r in state.residuals], ev_today)
        state.residuals = [dict(r, qty_signed=r["qty"] * np.sign(r["qty_signed"])) for r in state.residuals]
        for strat, tg in list(state.prev_targets.items()):
            for _, e in ev_today.iterrows():
                if e["type"] == corpact.SPLIT and e["symbol"] in tg.index:
                    tg[e["symbol"]] = np.floor(tg[e["symbol"]] * float(e["ratio"]))
                if e["type"] == corpact.SYMBOL_CHANGE and e["symbol"] in tg.index:
                    tg = tg.rename({e["symbol"]: e["new_symbol"]})
            state.prev_targets[strat] = tg
    # ---- targets and orders -----------------------------------------------------------------------------------------------
    targets = S.targets_for(adj, close, region, d, cfg.aum, state.prev_targets)
    state.prev_targets = targets
    positions_sod = {k: dict(v) for k, v in state.positions.items()}
    if warm:
        for strat, tg in targets.items():
            state.positions[strat] = {s: float(q) for s, q in tg.items() if abs(q) >= 1}
        return None
    orders, carry, netting = P.build_orders(d, targets, state.positions, stats, securities, cfg, residuals=state.residuals, strategy_specs=S.STRATEGIES, rng=rng)
    state.residuals = [r for r in carry if r["days"] <= cfg.max_carry_days]
    specs = {o.cl_ord_id: o for o in orders}
    if checks_fn is not None:
        checks += checks_fn("SOD", d, state, stats, orders, bars)
    # ---- market, links, sessions --------------------------------------------------------------------------------------
    market = DayMarket(d, bars, stats, securities, rng, kappa=0.3, psi=0.4)
    orders = [o for o in orders if market.has(o.symbol)]; specs = {o.cl_ord_id: o for o in orders}
    if not orders:
        return None
    t_start = min(o.start_time for o in orders) - 600; t_end = max(o.end_time for o in orders) + 900
    clock = Clock(t_start)
    fault_sched = F.generate_schedule(orders, rng, fault_mix, cfg.brokers) if fault_mix is not None else {b: [] for b in cfg.brokers}
    links, sessions, brokers, dc_links, dc_sessions, dc_rows = {}, {}, {}, {}, {}, []
    for b in cfg.brokers:
        link = VirtualLink(0.002, 0.002); link.now = t_start; links[b] = link
        store = state.seq_stores.setdefault(b, {})
        fund = Session("FUND", b, link.endpoint("a"), clock, heartbeat=30, initiator=True, seq_store=store, reset_on_logon=True)
        brk = Session(b, "FUND", link.endpoint("b"), clock, heartbeat=30, initiator=False)
        link.endpoint("a").attach(fund.on_bytes); link.endpoint("b").attach(brk.on_bytes)
        dcl = VirtualLink(0.002, 0.002); dcl.now = t_start; dc_links[b] = dcl
        dc_fund = Session("FUNDDC", b + "DC", dcl.endpoint("a"), clock, heartbeat=30, initiator=True, reset_on_logon=True, on_app=lambda m, t, b=b: dc_rows.append(O.ExecReport.parse(m, t)))
        dc_brk = Session(b + "DC", "FUNDDC", dcl.endpoint("b"), clock, heartbeat=30, initiator=False)
        dcl.endpoint("a").attach(dc_fund.on_bytes); dcl.endpoint("b").attach(dc_brk.on_bytes)
        bs = BrokerSim(b, market, clock, brk, dc_brk, rng=np.random.default_rng(rng.integers(1 << 30)), quality=BROKER_QUALITY.get(b, 1.0), ack_latency=BROKER_ACK.get(b, (0.02, 0.08)), no_locate=cfg.no_locate, faults=fault_sched[b])
        brk.on_app = bs.on_app; sessions[b] = fund; brokers[b] = bs; dc_sessions[b] = (dc_fund, dc_brk)
    mon = Monitor(clock, market, specs, sessions, thresholds)
    for b in cfg.brokers:
        sessions[b].on_app = lambda m, t, b=b: mon.on_app(b, m, t); sessions[b].on_event = lambda e, b=b: mon.on_session_event(b, e)
    # ---- the fund's own script: sends, a few replaces and cancels ----------------------------------------------------------
    send_times = sorted((o.start_time - 60, o.cl_ord_id) for o in orders)
    long_orders = [o for o in orders if o.end_time - o.start_time > 2 * 3600 and o.algo != "CLOSE"]
    rng.shuffle(long_orders)
    n_rep = max(1, len(long_orders) // 20); n_cxl = max(1, len(long_orders) // 30)
    replaces = [(o.start_time + 0.5 * (o.end_time - o.start_time), o.cl_ord_id) for o in long_orders[:n_rep]]
    cancels = [(o.start_time + 0.6 * (o.end_time - o.start_time), o.cl_ord_id) for o in long_orders[n_rep:n_rep + n_cxl]]
    # faults that fire on a cancel need a cancel: add them to the cancel list
    for b, fl in fault_sched.items():
        for f in fl:
            if f.type == "late_fill_after_cancel" and f.cl_ord_id in specs:
                s = specs[f.cl_ord_id]; cancels.append((s.start_time + 0.6 * (s.end_time - s.start_time), f.cl_ord_id))
    replaces.sort(); cancels.sort(); cancel_times = {}; replaced, canceled = [], []
    transport_faults = [(f, b) for b, fl in fault_sched.items() for f in fl if f.type in ("silent_session", "seq_gap")]
    for b in cfg.brokers:
        sessions[b].logon(); dc_sessions[b][0].logon()

    def drain(until: float):
        """deliver everything due before `until`, event by event"""
        while True:
            due = [l.next_due() for l in list(links.values()) + list(dc_links.values())] + [bs.outbox[0][0] for bs in brokers.values() if bs.outbox]; due = [x for x in due if x is not None]
            nxt = min(due) if due else None
            if nxt is None or nxt >= until:
                break
            clock.t = max(clock.t, nxt)
            for l in list(links.values()) + list(dc_links.values()):
                l.pump(clock.t)
            for bs in brokers.values():
                bs.flush(clock.t)

    t = t_start; si = ri = ci = 0; seq_counter = 0
    while t < t_end:
        clock.t = t
        for l in list(links.values()) + list(dc_links.values()):
            l.now = t
        # transport-level faults: a broker goes silent for five minutes, or one of its messages is lost
        for f, b in transport_faults:
            if f.armed and f.t <= t:
                f.armed = False; f.fired_at = t
                if f.type == "silent_session":
                    links[b].faults["b"]["silent"] = True
                else:
                    links[b].faults["b"]["drop"] = 1
            if f.type == "silent_session" and f.fired_at is not None and t >= f.fired_at + 300 and links[b].faults["b"].get("silent"):
                links[b].faults["b"]["silent"] = False
        # reconnect a lost session once the line is back (sequence numbers continue, so the gap is resent)
        for b in cfg.brokers:
            if sessions[b].disconnected and not links[b].faults["b"].get("silent"):
                sessions[b].logon(reset=False)
        def timers(now):
            for s in list(sessions.values()) + [x for pair in dc_sessions.values() for x in pair]:
                s.on_timer(now)
            for bs in brokers.values():
                bs.session.on_timer(now)
        timers(t)
        # fund actions due this minute
        while si < len(send_times) and send_times[si][0] <= t:
            o = specs[send_times[si][1]]; si += 1
            msg = O.new_order_single(o.cl_ord_id, o.symbol, o.side, o.qty, t, account="FUND", exchange=o.exchange, currency=o.currency, strategy=o.algo, start=o.start_time, end=o.end_time, max_pct=o.max_pct, urgency=o.urgency)
            sessions[o.broker].send(msg); mon.on_order_sent(o, t)
        while ri < len(replaces) and replaces[ri][0] <= t:
            cl = replaces[ri][1]; ri += 1; o = specs[cl]; st = mon.book.by_cl.get(cl)
            if st and not st.terminal and not st.is_pending:
                seq_counter += 1; new_cl = f"{cl}-R{seq_counter}"; new_qty = max(st.cum_qty + 1, np.floor(st.order_qty - 0.1 * st.leaves_qty))   # reduce the remainder by a tenth
                sessions[o.broker].send(O.cancel_replace(st.cl_ord_id, new_cl, st.order_id, o.symbol, o.side, new_qty, t, max_pct=o.max_pct)); mon.on_replace_sent(st.cl_ord_id, new_cl, t); replaced.append((cl, new_cl, new_qty))
        while ci < len(cancels) and cancels[ci][0] <= t:
            cl = cancels[ci][1]; ci += 1; o = specs[cl]; st = mon.book.by_cl.get(cl)
            if st and not st.terminal and not st.is_pending:
                seq_counter += 1; new_cl = f"{cl}-C{seq_counter}"
                sessions[o.broker].send(O.cancel_request(st.cl_ord_id, new_cl, st.order_id, o.symbol, o.side, st.order_qty, t)); mon.on_cancel_sent(st.cl_ord_id, new_cl, t); cancel_times[cl] = t; canceled.append((cl, new_cl))
        for bs in brokers.values():
            bs.step(t)
        # deliver event by event inside the minute, with session timers every 15 seconds
        for k in range(1, 5):
            drain(t + 15 * k); clock.t = t + 15 * k
            for l in list(links.values()) + list(dc_links.values()):
                l.now = clock.t
            if k < 4:
                timers(clock.t)
            mon.step(clock.t)
        t += 60
    # ---- end of day --------------------------------------------------------------------------------------------------------
    for bs in brokers.values():
        bs.end_of_day(t)
    drain(t + 120); clock.t = t + 120
    for b in cfg.brokers:
        if sessions[b].logged_on:
            sessions[b].logout("end of day")
    drain(t + 240)
    detections = F.score(fault_sched, mon.alerts, {o.cl_ord_id: o.broker for o in orders}, cancel_times=mon.cancel_times)
    # executions from the state machines (what the fund booked) and from the drop copy
    exec_rows = []
    for o in mon.book.orders.values():
        spec = specs[o.chain[0]]
        for (tt, q, px, eid, liq) in o.fills:
            exec_rows.append({"date": date_s, "source": "internal", "exec_id": eid, "cl_ord_id": o.chain[0], "order_id": o.order_id, "symbol": spec.symbol, "side": spec.side, "qty": q, "px": px, "time": tt, "broker": spec.broker, "liquidity": liq})
    dropcopy_rows = [{"date": date_s, "source": "dropcopy", "exec_id": r.exec_id, "cl_ord_id": r.cl_ord_id.split("-R")[0].split("-C")[0], "order_id": r.order_id, "symbol": r.symbol, "side": r.side, "qty": r.last_qty, "px": r.last_px, "time": r.recv_time, "broker": r.order_id.split("-")[0], "liquidity": r.liquidity} for r in dc_rows if r.is_fill]
    # positions: allocate fills to strategies
    positions_eod = {k: dict(v) for k, v in state.positions.items()}
    for o in mon.book.orders.values():
        spec = specs[o.chain[0]]
        if o.cum_qty > 0:
            for strat, q in P.allocate_fills(spec, o.cum_qty).items():
                positions_eod.setdefault(strat, {}); positions_eod[strat][spec.symbol] = positions_eod[strat].get(spec.symbol, 0.0) + q
            state.cash -= (1 if spec.side == O.SIDE_BUY else -1) * o.cum_qty * o.avg_px
    for strat in positions_eod:
        positions_eod[strat] = {s: q for s, q in positions_eod[strat].items() if abs(q) > 1e-9}
    state.positions = positions_eod
    for b in cfg.brokers:
        state.seq_stores[b] = dict(sessions[b].seq_store)
    session_stats = {b: sessions[b].status() | {"broker_side": brokers[b].session.status(), "link": links[b].stats} for b in cfg.brokers}
    broker_stats = {b: brokers[b].stats() for b in cfg.brokers}
    if checks_fn is not None:
        checks += checks_fn("EOD", d, state, stats, orders, bars, mon=mon, sessions=session_stats)
    mon_orders = [{k: v for k, v in row.items() if k != "spec"} for row in mon.order_rows()]
    rejected = {}
    for o in mon.book.orders.values():
        for (tt, text, eid) in o.anomalies:
            if eid:
                rejected[eid] = ("resync_after_rejected_report" if "resynced" in text else "rejected_report:" + text.split(":")[0][:40])
    return DayResult(date_s, orders, specs, exec_rows, dropcopy_rows, mon.alerts, detections, fault_sched, session_stats, broker_stats, positions_sod, positions_eod, ca_applied, netting, checks, market, mon.cancel_times, replaced, canceled, mon_orders, rejected)
