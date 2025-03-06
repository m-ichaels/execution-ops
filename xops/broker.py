"""Simulated broker behind a FIX acceptor session: validates orders, runs the algos (VWAP, TWAP, POV, IS, CLOSE) minute
by minute against the DayMarket, reports fills through ExecutionReports, mirrors them on a drop-copy session, and
carries the fault-injection hooks the monitor is tested against.  Timing is on the engine's virtual clock: acks and
reports leave after the broker's latency."""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field

import numpy as np

from .fix import message as M
from .fix import orders as O
from .fix.message import FixMessage
from .market import DayMarket

REJECT_UNKNOWN_SYMBOL, REJECT_DUP_CLORDID, REJECT_QTY, REJECT_NO_LOCATE, REJECT_STORM = 1, 6, 2, 99, 0


@dataclass
class Fault:
    type: str; t: float; cl_ord_id: str = ""; params: dict = field(default_factory=dict); armed: bool = True; fired_at: float | None = None


@dataclass
class AlgoOrder:
    cl_ord_id: str; order_id: str; symbol: str; side: str; qty: float; algo: str; start: float; end: float; max_pct: float; urgency: str
    cum: float = 0.0; avg_px: float = 0.0; status: str = O.ST_NEW; n_fills: int = 0; last_exec: FixMessage | None = None; done: bool = False; stuck: bool = False
    chain: list = field(default_factory=list); cancel_pending: bool = False; n_children: int = 0

    @property
    def leaves(self) -> float:
        return max(0.0, self.qty - self.cum)

    @property
    def sign(self) -> int:
        return 1 if self.side == O.SIDE_BUY else -1


class BrokerSim:
    def __init__(self, name: str, market: DayMarket, clock, session, dropcopy=None, *, rng: np.random.Generator, quality: float = 1.0, ack_latency: tuple[float, float] = (0.02, 0.08), fill_latency: float = 0.05,
                 no_locate: tuple = (), faults: list[Fault] | None = None, max_order_qty: float = 5e6):
        self.name, self.market, self.clock, self.session, self.dropcopy = name, market, clock, session, dropcopy
        self.rng, self.quality, self.ack_latency, self.fill_latency, self.no_locate, self.max_order_qty = rng, quality, ack_latency, fill_latency, set(no_locate), max_order_qty
        self.faults = faults or []
        self.orders: dict[str, AlgoOrder] = {}; self.by_cl: dict[str, AlgoOrder] = {}
        self.outbox: list[tuple[float, int, FixMessage, bool]] = []; self.n_out = 0
        self.oid = 0; self.eid = 0; self.reject_storm_left = 0; self.latency_extra = 0.0; self.latency_extra_until = 0.0
        self.log: list[tuple] = []

    # ---- outbound with latency -------------------------------------------------------------------------------------
    def _send(self, msg: FixMessage, delay: float | None = None, mirror: bool = True):
        d = self.fill_latency if delay is None else delay
        if self.clock() < self.latency_extra_until:
            d += self.latency_extra
        heapq.heappush(self.outbox, (self.clock() + d, self.n_out, msg, mirror)); self.n_out += 1

    def flush(self, now: float):
        while self.outbox and self.outbox[0][0] <= now:
            _, _, msg, mirror = heapq.heappop(self.outbox)
            self.session.send(msg)
            if mirror and self.dropcopy is not None and msg.msg_type == "8" and msg.get(M.ExecType) == O.EXEC_TRADE:
                self.dropcopy.send(FixMessage(fields=list(msg.fields)))

    def _exec_id(self) -> str:
        self.eid += 1; return f"{self.name}-{self.market.d.strftime('%Y%m%d')}-E{self.eid}"

    def _report(self, o: AlgoOrder, exec_type: str, status: str, t: float, **kw) -> FixMessage:
        m = O.execution_report(o.order_id, o.cl_ord_id, self._exec_id(), exec_type, status, o.symbol, o.side, o.qty, o.cum, o.avg_px, t, last_mkt=self.name, **kw)
        o.last_exec = m; return m

    # ---- fault helpers -------------------------------------------------------------------------------------------------
    def _fault(self, kind: str, cl_ord_id: str = "", now: float | None = None) -> Fault | None:
        now = self.clock() if now is None else now
        for f in self.faults:
            if f.armed and f.type == kind and f.t <= now and (not f.cl_ord_id or f.cl_ord_id == cl_ord_id):
                return f
        return None

    def _fire(self, f: Fault, exec_id: str = ""):
        f.armed = False; f.fired_at = self.clock(); self.log.append((self.clock(), "fault", f.type, f.cl_ord_id))
        if exec_id:
            f.params["exec_id"] = exec_id

    # ---- inbound application messages ------------------------------------------------------------------------------
    def on_app(self, msg: FixMessage, t: float):
        mt = msg.msg_type
        if mt == "D":
            self._new_order(msg, t)
        elif mt == "G":
            self._replace(msg, t)
        elif mt == "F":
            self._cancel(msg, t)

    def _new_order(self, msg: FixMessage, t: float):
        cl = msg.get(M.ClOrdID); sym = msg.get(M.Symbol); side = msg.get(M.Side); qty = msg.num(M.OrderQty)
        ack_delay = float(self.rng.uniform(*self.ack_latency))
        f = self._fault("reject_storm")
        if f:
            self._fire(f); self.reject_storm_left = int(f.params.get("n", 5))
        reason = None
        if cl in self.by_cl:
            reason, text = REJECT_DUP_CLORDID, "duplicate ClOrdID"
        elif not self.market.has(sym):
            reason, text = REJECT_UNKNOWN_SYMBOL, "unknown symbol"
        elif qty <= 0 or qty > self.max_order_qty:
            reason, text = REJECT_QTY, "order quantity out of range"
        elif side == O.SIDE_SELL_SHORT and sym in self.no_locate:
            reason, text = REJECT_NO_LOCATE, "no locate"
        elif self.reject_storm_left > 0:
            self.reject_storm_left -= 1; reason, text = REJECT_STORM, "broker system error"
        self.oid += 1; oid = f"{self.name}-{self.market.d.strftime('%Y%m%d')}-{self.oid}"
        o = AlgoOrder(cl, oid, sym, side, qty, msg.get(M.TargetStrategy, "VWAP"), self._ts(msg.get(M.AlgoStart), t), self._ts(msg.get(M.AlgoEnd), t + 6 * 3600), msg.num(M.AlgoMaxPct, 0.2), msg.get(M.AlgoUrgency, "low"))
        o.chain.append(cl); self.orders[oid] = o; self.by_cl[cl] = o
        if reason is not None:
            o.status = O.ST_REJECTED; o.done = True
            self._send(self._report(o, O.EXEC_REJECTED, O.ST_REJECTED, t, text=text, reject_reason=reason), ack_delay); return
        fa = self._fault("drop_ack", cl)
        if fa:
            self._fire(fa); self.log.append((t, "ack_dropped", cl)); return
        self._send(self._report(o, O.EXEC_NEW, O.ST_NEW, t), ack_delay)

    def _after_pending(self, o: AlgoOrder) -> float:
        """delay that puts a reply after any fill of this order already queued in the outbox"""
        now = self.clock(); pend = [ts - now for ts, _, m, _ in self.outbox if m.get(M.OrderID) == o.order_id]
        return (max(pend) if pend else 0.0) + 0.01

    def _replace(self, msg: FixMessage, t: float):
        orig = msg.get(M.OrigClOrdID); new_cl = msg.get(M.ClOrdID); o = self.by_cl.get(orig)
        if o is None or o.done:
            self._send(O.cancel_reject(o.order_id if o else "NONE", new_cl, orig, o.status if o else O.ST_REJECTED, "2", 1, "unknown or completed order", t)); return
        new_qty = msg.num(M.OrderQty)
        if new_qty < o.cum:
            self._send(O.cancel_reject(o.order_id, new_cl, orig, o.status, "2", 3, "new quantity below cumulative", t)); return
        d0 = self._after_pending(o)
        self._send(self._report(o, O.EXEC_PENDING_REPLACE, O.ST_PENDING_REPLACE, t, orig_cl_ord_id=orig), d0)
        o.qty = new_qty; o.cl_ord_id = new_cl; o.chain.append(new_cl); self.by_cl[new_cl] = o
        if msg.has(M.ParticipationRate):
            o.max_pct = msg.num(M.ParticipationRate)
        status = O.ST_FILLED if o.leaves <= 0 else (O.ST_PARTIAL if o.cum > 0 else O.ST_NEW)
        if status == O.ST_FILLED:
            o.done = True
        self._send(self._report(o, O.EXEC_REPLACED, status, t, orig_cl_ord_id=orig), d0 + 0.02)

    def _cancel(self, msg: FixMessage, t: float):
        orig = msg.get(M.OrigClOrdID); new_cl = msg.get(M.ClOrdID); o = self.by_cl.get(orig)
        if o is None or o.done:
            self._send(O.cancel_reject(o.order_id if o else "NONE", new_cl, orig, o.status if o else O.ST_REJECTED, "1", 1, "unknown or completed order", t)); return
        d0 = self._after_pending(o)
        self._send(self._report(o, O.EXEC_PENDING_CANCEL, O.ST_PENDING_CANCEL, t, orig_cl_ord_id=orig), d0)
        o.cl_ord_id = new_cl; o.chain.append(new_cl); self.by_cl[new_cl] = o; o.status = O.ST_CANCELED; o.done = True
        self._send(self._report(o, O.EXEC_CANCELED, O.ST_CANCELED, t, orig_cl_ord_id=orig), d0 + 0.02)
        f = self._fault("late_fill_after_cancel", orig) or self._fault("late_fill_after_cancel", new_cl)
        if f:
            px = self.market.symbols[o.symbol].mid_at(t); q = max(1.0, np.floor(min(100.0, o.qty * 0.05)))
            o.cum += q; m = self._report(o, O.EXEC_TRADE, O.ST_CANCELED, t + 2, last_qty=q, last_px=px); self._fire(f, m.get(M.ExecID)); self._send(m, 2.0)

    @staticmethod
    def _ts(s: str | None, default: float) -> float:
        if not s:
            return default
        import datetime as dt
        try:
            return dt.datetime.strptime(s[:17], "%Y%m%d-%H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()
        except ValueError:
            return default

    # ---- the algos, one minute at a time ---------------------------------------------------------------------------------
    def schedule_fraction(self, o: AlgoOrder, t: float, sym) -> float:
        """target cumulative fraction of the order to be done by time t"""
        if t <= o.start:
            return 0.0
        T = max(o.end - o.start, 60.0); tau = min(t - o.start, T)
        if o.algo == "TWAP":
            return tau / T
        if o.algo == "VWAP":
            tot = sym.volume_between(o.start, o.end); done = sym.volume_between(o.start, t)
            return done / max(tot, 1.0)
        if o.algo == "IS":
            lam = (3.0 if o.urgency == "high" else 1.2) / T
            return (1 - np.exp(-lam * tau)) / (1 - np.exp(-lam * T))
        if o.algo == "CLOSE":
            last = 3600.0
            if t < o.end - last:
                return 0.0
            return 0.3 * min(1.0, (t - (o.end - last)) / last)
        return tau / T   # POV handled separately

    def step(self, t: float):
        """one minute of algo activity, then deliver what is due"""
        for o in list(self.orders.values()):
            if o.done or o.stuck or o.status == O.ST_REJECTED:
                continue
            sym = self.market.symbols.get(o.symbol)
            if sym is None:
                continue
            fs = self._fault("stuck", o.cl_ord_id)
            if fs:
                self._fire(fs); o.stuck = True; continue
            if t < o.start:
                continue
            if t >= o.end:
                self._finish(o, t, sym); continue
            k = sym.minute(t); vmin = sym.vol_curve[k]
            cap = o.max_pct * vmin
            fo = self._fault("over_participation", o.cl_ord_id); forced = None
            if fo and t < fo.t + 600:
                forced = min(o.leaves, np.floor(4.0 * cap))          # the algo runs away: four times its participation cap
                if fo.fired_at is None:
                    self._fire(fo); fo.armed = True
            elif fo and t >= fo.t + 600:
                fo.armed = False
            if o.algo == "POV":
                child = min(o.leaves, np.floor(o.max_pct / (1 - o.max_pct) * vmin))
            elif o.algo == "CLOSE" and t >= o.end - 60:
                child = 0.0
            else:
                target = o.qty * self.schedule_fraction(o, t + 60, sym)
                child = min(o.leaves, max(0.0, np.floor(target - o.cum)), np.floor(cap)) if o.algo != "POV" else 0.0
                # catch-up when behind (the algos may be behind after a partial fill)
                if o.algo in ("IS", "VWAP", "TWAP") and o.cum < o.qty * self.schedule_fraction(o, t, sym) - 1:
                    child = min(o.leaves, np.floor(cap), child + np.floor(0.5 * (o.qty * self.schedule_fraction(o, t, sym) - o.cum)))
            if forced is not None:
                child = forced
            if child >= 1:
                self._child(o, sym, child, t)
        self.flush(t)

    def _child(self, o: AlgoOrder, sym, child: float, t: float, at_close: bool = False):
        filled, px, _ = self.market.execute(o.symbol, o.sign, child, t + float(self.rng.uniform(0, 59)), quality=self.quality, at_close=at_close)
        if filled < 1:
            return
        o.n_children += 1
        fb = self._fault("bad_price", o.cl_ord_id)
        if fb:
            self._fire(fb); px = px * (1 + o.sign * fb.params.get("pct", 0.03))
        o.avg_px = (o.avg_px * o.cum + px * filled) / (o.cum + filled); o.cum += filled; o.n_fills += 1
        status = O.ST_FILLED if o.leaves <= 0 else O.ST_PARTIAL
        if status == O.ST_FILLED:
            o.done = True; o.status = O.ST_FILLED
        else:
            o.status = O.ST_PARTIAL
        m = self._report(o, O.EXEC_TRADE, status, t, last_qty=filled, last_px=px, liquidity="R"); eid = m.get(M.ExecID)
        # fault variants of the report
        fw = self._fault("wrong_side", o.cl_ord_id)
        if fw:
            self._fire(fw, eid); m.set(M.Side, O.SIDE_SELL if o.side == O.SIDE_BUY else O.SIDE_BUY)
        fsym = self._fault("symbol_mismatch", o.cl_ord_id)
        if fsym:
            self._fire(fsym, eid); m.set(M.Symbol, fsym.params.get("symbol", "ZZZZ"))
        fl = self._fault("stale_leaves", o.cl_ord_id)
        if fl:
            self._fire(fl, eid); m.set(M.LeavesQty, o.leaves + 1000)
        fov = self._fault("overfill", o.cl_ord_id)
        if fov:
            self._fire(fov, eid); m.set(M.CumQty, o.qty + filled).set(M.LastQty, filled + o.leaves + filled)
        fdc = self._fault("dropcopy_missing", o.cl_ord_id)
        self._send(m, mirror=fdc is None)
        if fdc:
            self._fire(fdc, eid)
        fd = self._fault("dup_fill", o.cl_ord_id)
        if fd:
            self._fire(fd, eid); self._send(FixMessage(fields=list(m.fields)), self.fill_latency + 0.5)
        fs = self._fault("latency_spike")
        if fs:
            self._fire(fs); self.latency_extra = fs.params.get("seconds", 2.0); self.latency_extra_until = self.clock() + fs.params.get("duration", 300)

    def _finish(self, o: AlgoOrder, t: float, sym):
        """at the algo's end time: CLOSE orders go to the auction, others get DoneForDay for the remainder"""
        if o.algo == "CLOSE" and o.leaves > 0:
            self._child(o, sym, o.leaves, t, at_close=True)
        if o.done:
            return
        o.done = True; o.status = O.ST_DFD
        self._send(self._report(o, O.EXEC_DFD, O.ST_DFD, t))

    def end_of_day(self, t: float):
        for o in self.orders.values():
            if not o.done and not o.stuck:
                o.done = True; o.status = O.ST_DFD; self._send(self._report(o, O.EXEC_DFD, O.ST_DFD, t))
        self.flush(t + 10)

    def stats(self) -> dict:
        os_ = list(self.orders.values())
        return {"orders": len(os_), "filled": sum(o.status == O.ST_FILLED for o in os_), "partial_dfd": sum(o.status == O.ST_DFD for o in os_), "rejected": sum(o.status == O.ST_REJECTED for o in os_), "canceled": sum(o.status == O.ST_CANCELED for o in os_), "fills": sum(o.n_fills for o in os_)}
