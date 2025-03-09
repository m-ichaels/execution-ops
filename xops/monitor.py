"""The live order-flow monitor on the fund side: maintains the order state machines from ExecutionReports, watches the
sessions, and evaluates the alert rules once a minute.  Every rule has a threshold in `Thresholds`, alerts are
de-duplicated per (rule, order) inside a cooling window, and the whole alert log is written to the store so the
fault-injection scorer and the summary can grade it."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .fix import message as M
from .fix import orders as O
from .fix.message import FixMessage
from .fix.state import OrderBook, OrderState


@dataclass
class Thresholds:
    unacked_s: float = 10.0
    stalled_min: float = 10.0          # no fill for this long while the schedule expects progress
    stalled_shortfall: float = 0.03    # ... and the schedule expected at least this share of the order in that time
    behind_shortfall: float = 0.25
    ahead_excess: float = 0.25
    participation_mult: float = 2.0    # fills in the last 10 minutes vs max_pct x market volume
    price_collar_bp: float = 100.0     # fill price vs the current mid
    slippage_bp: float = 30.0          # running average vs the algo's benchmark, absolute floor
    slippage_mult: float = 3.0         # ... or this multiple of the pre-trade estimate
    reject_count: int = 3; reject_window_s: float = 300.0
    latency_p95_s: float = 1.0
    session_silent_mult: float = 2.0     # the session layer itself sends a TestRequest at 1.2 x HeartBtInt
    leaves_after_end_min: float = 5.0
    cooldown_s: float = 900.0


SEVERITY = {"UNACKED": "high", "STALLED": "medium", "BEHIND_SCHEDULE": "low", "AHEAD_SCHEDULE": "medium", "PARTICIPATION": "high", "PRICE_COLLAR": "high", "SLIPPAGE": "medium", "STATE_ANOMALY": "high", "UNKNOWN_ORDER": "high", "REJECT_RATE": "high", "LATENCY": "medium", "SESSION_SILENT": "high", "SESSION_LOST": "critical", "SEQ_GAP": "medium", "LEAVES_AFTER_END": "medium", "CANCEL_REJECT": "medium", "PRETRADE_REJECT": "medium"}


def _parse_ts(s: str) -> float | None:
    import datetime as dt
    try:
        base = dt.datetime.strptime(s[:17], "%Y%m%d-%H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()
        return base + (float("0." + s[18:]) if len(s) > 18 else 0.0)
    except (ValueError, TypeError):
        return None


def pretrade_cost_bp(spec) -> float:
    """square-root pre-trade estimate: half spread plus 0.6 sigma sqrt(Q/ADV)"""
    return 0.5 * spec.spread_bp + 0.6 * spec.vol_day * 1e4 * np.sqrt(spec.qty / max(spec.adv, 1.0))


class Monitor:
    def __init__(self, clock, market, specs: dict, sessions: dict, th: Thresholds | None = None):
        self.clock, self.market, self.specs, self.sessions, self.th = clock, market, specs, sessions, th or Thresholds()
        self.book = OrderBook(); self.alerts: list[dict] = []; self._last_alert: dict[tuple, float] = {}
        self.arrival: dict[str, float] = {}; self.sent_at: dict[str, float] = {}; self.rejects: deque = deque(); self.ack_lat: deque = deque(maxlen=200)
        self.fills_by_order: dict[str, deque] = {}; self.cancel_times: dict[str, float] = {}; self.session_flags: dict[str, set] = {b: set() for b in sessions}; self.report_lat: deque = deque(maxlen=200)
        self.n_exec = 0; self.last_fill_at: dict[str, float] = {}; self.rejects_by_broker: dict[str, deque] = {b: deque() for b in sessions}; self.lat_by_broker: dict[str, deque] = {b: deque(maxlen=200) for b in sessions}

    # ---- events -------------------------------------------------------------------------------------------------
    def alert(self, rule: str, cl_ord_id: str = "", symbol: str = "", detail: str = "", broker: str = ""):
        now = self.clock(); key = (rule, cl_ord_id or broker)
        if now - self._last_alert.get(key, -1e9) < self.th.cooldown_s:
            return
        self._last_alert[key] = now
        self.alerts.append({"time": now, "rule": rule, "severity": SEVERITY.get(rule, "low"), "cl_ord_id": cl_ord_id, "symbol": symbol, "detail": detail, "broker": broker})

    def on_order_sent(self, spec, t: float):
        st = OrderState(spec.cl_ord_id, spec.symbol, spec.side, spec.qty, t); self.book.add(st); self.sent_at[spec.cl_ord_id] = t
        sym = self.market.symbols.get(spec.symbol)
        self.arrival[spec.cl_ord_id] = sym.mid_at(max(t, sym.t_open)) if sym else spec.decision_px
        self.fills_by_order[spec.cl_ord_id] = deque()

    def on_cancel_sent(self, cl_ord_id: str, new_cl: str, t: float):
        o = self.book.by_cl.get(cl_ord_id)
        if o:
            o.request_cancel(new_cl); self.book.link(new_cl, o); self.cancel_times[cl_ord_id] = t; self.cancel_times[new_cl] = t

    def on_replace_sent(self, cl_ord_id: str, new_cl: str, t: float):
        o = self.book.by_cl.get(cl_ord_id)
        if o:
            o.request_replace(new_cl); self.book.link(new_cl, o)

    def on_app(self, broker: str, msg: FixMessage, t: float):
        mt = msg.msg_type
        if mt == "8":
            self._on_exec(broker, O.ExecReport.parse(msg, t))
        elif mt == "9":
            o = self.book.by_cl.get(msg.get(M.OrigClOrdID)) or self.book.by_cl.get(msg.get(M.ClOrdID))
            if o:
                o.apply_cancel_reject(msg.get(M.OrdStatus, ""), t)
            self.alert("CANCEL_REJECT", msg.get(M.OrigClOrdID, ""), o.symbol if o else "", msg.get(M.Text, ""), broker)

    def _on_exec(self, broker: str, er: O.ExecReport):
        self.n_exec += 1
        o, anomaly = self.book.apply(er)
        if o is None:
            self.alert("UNKNOWN_ORDER", er.cl_ord_id, er.symbol, anomaly or "", broker); return
        if anomaly:
            self.alert("STATE_ANOMALY", o.chain[0], o.symbol, anomaly, broker); return
        spec = self.specs.get(o.chain[0])
        if er.exec_type == O.EXEC_NEW and o.chain[0] in self.sent_at:
            self.ack_lat.append(er.recv_time - self.sent_at[o.chain[0]])
        tt = _parse_ts(er.transact_time)
        if tt is not None:
            self.report_lat.append(er.recv_time - tt); self.lat_by_broker.setdefault(broker, deque(maxlen=200)).append(er.recv_time - tt)
        if er.exec_type == O.EXEC_REJECTED:
            self.rejects.append(er.recv_time); self.rejects_by_broker.setdefault(broker, deque()).append(er.recv_time); self.alert("PRETRADE_REJECT", o.chain[0], o.symbol, er.text, broker)
        if er.is_fill:
            self.fills_by_order.setdefault(o.chain[0], deque()).append((er.recv_time, er.last_qty, er.last_px)); self.last_fill_at[o.chain[0]] = er.recv_time
            sym = self.market.symbols.get(o.symbol)
            if sym is not None:
                mid = sym.mid_at(er.recv_time) if sym.is_open(er.recv_time) else sym.close_px
                dev_bp = abs(er.last_px / mid - 1) * 1e4
                if dev_bp > self.th.price_collar_bp:
                    self.alert("PRICE_COLLAR", o.chain[0], o.symbol, f"fill {er.last_px:.4f} vs mid {mid:.4f}: {dev_bp:.0f} bp", broker)
            if spec is not None and spec.algo != "CLOSE":
                exp = self.expected_fraction(spec, er.recv_time) * o.order_qty
                if o.cum_qty - exp > self.th.ahead_excess * o.order_qty and er.recv_time < spec.end_time:
                    self.alert("AHEAD_SCHEDULE", o.chain[0], o.symbol, f"{100 * (o.cum_qty - exp) / o.order_qty:.0f}% ahead of the {spec.algo} schedule", broker)
            if spec is not None and o.cum_qty > 0 and sym is not None:
                # running average against the algo's own benchmark: the interval VWAP for schedule algos (market drift
                # cancels), arrival for IS with a drift allowance that grows with the square root of elapsed time
                sign = 1 if o.side == O.SIDE_BUY else -1; now = min(er.recv_time, sym.t_close - 1)
                if spec.algo == "IS":
                    ref = self.arrival[o.chain[0]]; elapsed = max(now - spec.start_time, 60.0)
                    limit = max(self.th.slippage_bp, self.th.slippage_mult * pretrade_cost_bp(spec) + 3.0 * spec.vol_day * 1e4 * np.sqrt(elapsed / max(sym.t_close - sym.t_open, 3600.0)))
                    bench = "arrival"
                elif spec.algo == "CLOSE":
                    # a close order works the last hour and the auction: its interval is the last hour, not the day
                    ref = sym.vwap(max(spec.end_time - 3600, spec.start_time), now); limit = max(self.th.slippage_bp, self.th.slippage_mult * pretrade_cost_bp(spec)); bench = "last-hour VWAP"
                else:
                    ref = sym.vwap(spec.start_time, now); limit = max(self.th.slippage_bp, self.th.slippage_mult * pretrade_cost_bp(spec)); bench = "interval VWAP"
                slip = sign * (o.avg_px / ref - 1) * 1e4
                if slip > limit:
                    self.alert("SLIPPAGE", o.chain[0], o.symbol, f"running average {slip:.0f} bp worse than {bench} (limit {limit:.0f} bp)", broker)

    def on_session_event(self, broker: str, ev):
        if ev.kind == "gap_detected":
            self.alert("SEQ_GAP", "", "", ev.detail, broker)
        elif ev.kind == "session_lost":
            self.alert("SESSION_LOST", "", "", ev.detail, broker)
        elif ev.kind == "logged_out" and not getattr(self.sessions.get(broker), "logout_sent", False):
            self.alert("SESSION_LOST", "", "", "logout: " + ev.detail, broker)

    # ---- periodic rules -------------------------------------------------------------------------------------------
    def expected_fraction(self, spec, t: float) -> float:
        if t <= spec.start_time:
            return 0.0
        T = max(spec.end_time - spec.start_time, 60.0); tau = min(t - spec.start_time, T)
        sym = self.market.symbols.get(spec.symbol)
        if spec.algo == "VWAP" and sym is not None:
            return sym.volume_between(spec.start_time, t) / max(sym.volume_between(spec.start_time, spec.end_time), 1.0)
        if spec.algo == "IS":
            lam = (3.0 if spec.urgency == "high" else 1.2) / T; return (1 - np.exp(-lam * tau)) / (1 - np.exp(-lam * T))
        if spec.algo == "CLOSE":
            return 0.0 if t < spec.end_time - 3600 else 0.3 * min(1.0, (t - (spec.end_time - 3600)) / 3600)
        if spec.algo == "POV" and sym is not None:
            return min(1.0, spec.max_pct * sym.volume_between(spec.start_time, t) / max(spec.qty, 1.0))
        return tau / T

    def step(self, t: float):
        th = self.th
        # rejects, latency, sessions: per broker
        for b, rj in self.rejects_by_broker.items():
            while rj and t - rj[0] > th.reject_window_s:
                rj.popleft()
            if len(rj) >= th.reject_count:
                self.alert("REJECT_RATE", "", "", f"{len(rj)} rejects from {b} in {th.reject_window_s:.0f} s", b)
        for b, lat in self.lat_by_broker.items():
            if len(lat) >= 20 and np.quantile(list(lat)[-50:], 0.95) > th.latency_p95_s:
                self.alert("LATENCY", "", "", f"report latency p95 {np.quantile(list(lat)[-50:], 0.95):.2f} s from {b} (TransactTime to receipt)", b)
        for b, s in self.sessions.items():
            if s.logged_on and s.seconds_since_recv(t) > th.session_silent_mult * s.heartbeat:
                self.alert("SESSION_SILENT", "", "", f"no message from {b} for {s.seconds_since_recv(t):.0f} s", b)
        # per order
        for o in self.book.orders.values():
            spec = self.specs.get(o.chain[0])
            if spec is None:
                continue
            b = spec.broker
            if o.status == O.ST_PENDING_NEW and t - o.created > th.unacked_s:
                self.alert("UNACKED", o.chain[0], o.symbol, f"no acknowledgement after {t - o.created:.0f} s", b)
                continue
            if o.terminal or o.status == O.ST_PENDING_NEW:
                continue
            if t > spec.end_time + th.leaves_after_end_min * 60 and o.leaves_qty > 0:
                self.alert("LEAVES_AFTER_END", o.chain[0], o.symbol, f"{o.leaves_qty:.0f} left {((t - spec.end_time) / 60):.0f} min after the end time, status {O.STATUS_NAME.get(o.status, o.status)}", b)
            exp = self.expected_fraction(spec, t) * o.order_qty; done = o.cum_qty
            fills = self.fills_by_order.get(o.chain[0], deque())
            last_fill = self.last_fill_at.get(o.chain[0], spec.start_time)
            expected_since = (self.expected_fraction(spec, t) - self.expected_fraction(spec, last_fill)) * o.order_qty
            if o.leaves_qty > 0 and t - last_fill > th.stalled_min * 60 and t > spec.start_time and expected_since > max(th.stalled_shortfall * o.order_qty, 50.0):
                self.alert("STALLED", o.chain[0], o.symbol, f"no fill for {(t - last_fill) / 60:.0f} min while the {spec.algo} schedule expected {100 * expected_since / o.order_qty:.0f}% of the order", b)
            elif spec.algo != "POV" and exp - done > th.behind_shortfall * o.order_qty:
                self.alert("BEHIND_SCHEDULE", o.chain[0], o.symbol, f"{100 * (exp - done) / o.order_qty:.0f}% behind the {spec.algo} schedule", b)
            if spec.algo not in ("CLOSE",) and done - exp > th.ahead_excess * o.order_qty and t < spec.end_time:
                self.alert("AHEAD_SCHEDULE", o.chain[0], o.symbol, f"{100 * (done - exp) / o.order_qty:.0f}% ahead of the {spec.algo} schedule", b)
            sym = self.market.symbols.get(o.symbol)
            if sym is not None and fills:
                while fills and t - fills[0][0] > 600:
                    fills.popleft()
                recent = sum(q for _, q, _ in fills); mkt = sym.volume_between(max(t - 600, sym.t_open), min(t, sym.t_close - 1))
                if mkt > 0 and recent > th.participation_mult * spec.max_pct * mkt and recent > 500:
                    self.alert("PARTICIPATION", o.chain[0], o.symbol, f"{100 * recent / mkt:.0f}% of volume in the last 10 min vs cap {100 * spec.max_pct:.0f}%", b)

    def order_rows(self) -> list[dict]:
        rows = []
        for o in self.book.orders.values():
            spec = self.specs.get(o.chain[0])
            rows.append({"cl_ord_id": o.chain[0], "order_id": o.order_id, "status": O.STATUS_NAME.get(o.status, o.status), "cum_qty": o.cum_qty, "avg_px": o.avg_px, "n_fills": len(o.fills), "arrival_px": self.arrival.get(o.chain[0]), "ack_latency_ms": None if o.ack_time is None else 1000 * (o.ack_time - o.created), "anomalies": len(o.anomalies), "spec": spec})
        return rows
