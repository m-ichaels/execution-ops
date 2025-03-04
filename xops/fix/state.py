"""Buy-side order state machine.  One `OrderState` per parent order, keyed by the current ClOrdID chain, updated from
ExecutionReports and OrderCancelRejects.  The transition table is explicit and every transition is checked; anything
outside the table is recorded as an anomaly rather than applied, which is what a monitor wants to see."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import orders as O

# (current OrdStatus, ExecType) -> allowed next OrdStatus values
TRANSITIONS: dict[tuple[str, str], set[str]] = {
    (O.ST_PENDING_NEW, O.EXEC_NEW): {O.ST_NEW}, (O.ST_PENDING_NEW, O.EXEC_REJECTED): {O.ST_REJECTED}, (O.ST_PENDING_NEW, O.EXEC_PENDING_NEW): {O.ST_PENDING_NEW},
    (O.ST_PENDING_NEW, O.EXEC_TRADE): {O.ST_PARTIAL, O.ST_FILLED},   # some brokers fill before the New is seen
    (O.ST_NEW, O.EXEC_TRADE): {O.ST_PARTIAL, O.ST_FILLED}, (O.ST_NEW, O.EXEC_CANCELED): {O.ST_CANCELED}, (O.ST_NEW, O.EXEC_PENDING_CANCEL): {O.ST_PENDING_CANCEL}, (O.ST_NEW, O.EXEC_PENDING_REPLACE): {O.ST_PENDING_REPLACE},
    (O.ST_NEW, O.EXEC_REPLACED): {O.ST_NEW, O.ST_REPLACED}, (O.ST_NEW, O.EXEC_DFD): {O.ST_DFD}, (O.ST_NEW, O.EXEC_EXPIRED): {O.ST_EXPIRED}, (O.ST_NEW, O.EXEC_RESTATED): {O.ST_NEW},
    (O.ST_PARTIAL, O.EXEC_TRADE): {O.ST_PARTIAL, O.ST_FILLED}, (O.ST_PARTIAL, O.EXEC_CANCELED): {O.ST_CANCELED}, (O.ST_PARTIAL, O.EXEC_PENDING_CANCEL): {O.ST_PENDING_CANCEL}, (O.ST_PARTIAL, O.EXEC_PENDING_REPLACE): {O.ST_PENDING_REPLACE},
    (O.ST_PARTIAL, O.EXEC_REPLACED): {O.ST_PARTIAL, O.ST_REPLACED, O.ST_FILLED}, (O.ST_PARTIAL, O.EXEC_DFD): {O.ST_DFD}, (O.ST_PARTIAL, O.EXEC_EXPIRED): {O.ST_EXPIRED}, (O.ST_PARTIAL, O.EXEC_RESTATED): {O.ST_PARTIAL}, (O.ST_PARTIAL, O.EXEC_TRADE_CANCEL): {O.ST_PARTIAL, O.ST_NEW},
    (O.ST_PENDING_CANCEL, O.EXEC_CANCELED): {O.ST_CANCELED}, (O.ST_PENDING_CANCEL, O.EXEC_TRADE): {O.ST_PENDING_CANCEL, O.ST_FILLED, O.ST_PARTIAL}, (O.ST_PENDING_CANCEL, O.EXEC_PENDING_CANCEL): {O.ST_PENDING_CANCEL},
    (O.ST_PENDING_REPLACE, O.EXEC_REPLACED): {O.ST_NEW, O.ST_PARTIAL, O.ST_REPLACED, O.ST_FILLED}, (O.ST_PENDING_REPLACE, O.EXEC_TRADE): {O.ST_PENDING_REPLACE, O.ST_PARTIAL, O.ST_FILLED}, (O.ST_PENDING_REPLACE, O.EXEC_PENDING_REPLACE): {O.ST_PENDING_REPLACE},
    (O.ST_PENDING_REPLACE, O.EXEC_CANCELED): {O.ST_CANCELED},
    (O.ST_FILLED, O.EXEC_TRADE_CORRECT): {O.ST_FILLED}, (O.ST_FILLED, O.EXEC_TRADE_CANCEL): {O.ST_PARTIAL, O.ST_FILLED}, (O.ST_FILLED, O.EXEC_DFD): {O.ST_DFD},
}


@dataclass
class OrderState:
    cl_ord_id: str; symbol: str; side: str; order_qty: float; created: float
    status: str = O.ST_PENDING_NEW; order_id: str = ""; cum_qty: float = 0.0; leaves_qty: float = 0.0; avg_px: float = 0.0
    fills: list = field(default_factory=list); exec_ids: set = field(default_factory=set); history: list = field(default_factory=list); anomalies: list = field(default_factory=list)
    ack_time: float | None = None; last_update: float = 0.0; pending_cl_ord_id: str | None = None; chain: list = field(default_factory=list)

    def __post_init__(self):
        self.leaves_qty = self.order_qty; self.chain = [self.cl_ord_id]; self.last_update = self.created

    @property
    def terminal(self) -> bool:
        return self.status in O.TERMINAL

    @property
    def is_pending(self) -> bool:
        return self.status in (O.ST_PENDING_NEW, O.ST_PENDING_CANCEL, O.ST_PENDING_REPLACE)

    def request_cancel(self, new_cl_ord_id: str):
        self.pending_cl_ord_id = new_cl_ord_id; self.chain.append(new_cl_ord_id)

    def request_replace(self, new_cl_ord_id: str):
        self.pending_cl_ord_id = new_cl_ord_id; self.chain.append(new_cl_ord_id)

    def apply(self, er: O.ExecReport) -> str | None:
        """Apply an ExecutionReport; returns an anomaly string when the report is inconsistent (state is left as is)."""
        key = (self.status, er.exec_type)
        anomaly = None
        if er.exec_id in self.exec_ids and er.exec_type == O.EXEC_TRADE:
            anomaly = f"duplicate ExecID {er.exec_id}"
        elif self.terminal and er.exec_type in (O.EXEC_TRADE, O.EXEC_NEW):
            anomaly = f"{er.exec_type} after terminal state {O.STATUS_NAME[self.status]}"
        elif key not in TRANSITIONS:
            anomaly = f"no transition from {O.STATUS_NAME.get(self.status, self.status)} on ExecType {er.exec_type}"
        elif er.status not in TRANSITIONS[key]:
            anomaly = f"OrdStatus {O.STATUS_NAME.get(er.status, er.status)} not allowed after {O.STATUS_NAME[self.status]} + ExecType {er.exec_type}"
        elif er.exec_type == O.EXEC_TRADE and er.cum_qty + 1e-9 < self.cum_qty:
            anomaly = f"CumQty went down {self.cum_qty} -> {er.cum_qty}"
        elif er.exec_type == O.EXEC_TRADE and abs((self.cum_qty + er.last_qty) - er.cum_qty) > 1e-6 and er.cum_qty <= er.order_qty + 1e-9:
            # the broker's cumulative is authoritative: book the difference, flag the report (a resync, not a drop)
            resync = er.cum_qty - self.cum_qty
            self.anomalies.append((er.recv_time, f"CumQty {er.cum_qty} != previous {self.cum_qty} + LastQty {er.last_qty}: resynced {resync:g}", er.exec_id))
            er = O.ExecReport(er.order_id, er.cl_ord_id, er.orig_cl_ord_id, er.exec_id, er.exec_type, er.status, er.symbol, er.side, er.order_qty, resync, er.last_px, er.leaves_qty, er.cum_qty, er.avg_px, er.transact_time, er.text, er.last_mkt, er.liquidity, er.seq, er.recv_time, er.raw)
            if resync <= 0:
                anomaly = "resync with non-positive quantity"
        elif er.exec_type == O.EXEC_TRADE and er.cum_qty > er.order_qty + 1e-9:
            anomaly = f"overfill: CumQty {er.cum_qty} > OrderQty {er.order_qty}"
        elif er.symbol != self.symbol or er.side != self.side:
            anomaly = f"symbol/side mismatch {er.symbol}/{er.side} vs {self.symbol}/{self.side}"
        elif er.status not in O.TERMINAL and er.exec_type in (O.EXEC_TRADE, O.EXEC_NEW, O.EXEC_REPLACED) and abs(er.leaves_qty - (er.order_qty - er.cum_qty)) > 1e-6:
            anomaly = f"LeavesQty {er.leaves_qty} != OrderQty {er.order_qty} - CumQty {er.cum_qty}"
        if anomaly:
            self.anomalies.append((er.recv_time, anomaly, er.exec_id)); return anomaly
        # apply
        self.history.append((er.recv_time, er.exec_type, er.status, er.last_qty, er.last_px))
        if er.exec_type == O.EXEC_NEW and self.ack_time is None:
            self.ack_time = er.recv_time
        if er.order_id:
            self.order_id = er.order_id
        if er.exec_type == O.EXEC_TRADE:
            self.exec_ids.add(er.exec_id); self.fills.append((er.recv_time, er.last_qty, er.last_px, er.exec_id, er.liquidity))
            self.avg_px = (self.avg_px * self.cum_qty + er.last_px * er.last_qty) / max(er.cum_qty, 1e-12); self.cum_qty = er.cum_qty
        if er.exec_type == O.EXEC_REPLACED:
            self.order_qty = er.order_qty; self.cl_ord_id = er.cl_ord_id; self.pending_cl_ord_id = None
        if er.exec_type in (O.EXEC_CANCELED, O.EXEC_REJECTED):
            if self.pending_cl_ord_id and er.cl_ord_id == self.pending_cl_ord_id:
                self.cl_ord_id = er.cl_ord_id
            self.pending_cl_ord_id = None
        self.status = er.status
        self.leaves_qty = 0.0 if self.terminal else max(0.0, self.order_qty - self.cum_qty)
        self.last_update = er.recv_time
        return None

    def apply_cancel_reject(self, status: str, t: float):
        self.history.append((t, "9", status, 0.0, 0.0)); self.pending_cl_ord_id = None; self.last_update = t
        if status in O.STATUS_NAME:
            self.status = status
        self.leaves_qty = 0.0 if self.terminal else max(0.0, self.order_qty - self.cum_qty)

    def check_invariants(self) -> list[str]:
        out = []
        if self.cum_qty > self.order_qty + 1e-9:
            out.append("cum > order qty")
        if not self.terminal and abs(self.leaves_qty - (self.order_qty - self.cum_qty)) > 1e-9:
            out.append("leaves != order - cum")
        if self.terminal and self.leaves_qty != 0.0:
            out.append("terminal with leaves")
        if self.status == O.ST_FILLED and abs(self.cum_qty - self.order_qty) > 1e-9:
            out.append("filled but cum != order qty")
        if any(q <= 0 for _, q, _, _, _ in self.fills):
            out.append("non-positive fill")
        return out


class OrderBook:
    """All parent orders of a session, indexed by every ClOrdID in their chains and by OrderID."""

    def __init__(self):
        self.orders: dict[str, OrderState] = {}; self.by_cl: dict[str, OrderState] = {}; self.by_oid: dict[str, OrderState] = {}; self.unknown: list = []

    def add(self, o: OrderState):
        self.orders[o.chain[0]] = o; self.by_cl[o.cl_ord_id] = o

    def link(self, cl_ord_id: str, o: OrderState):
        self.by_cl[cl_ord_id] = o

    def find(self, er: O.ExecReport) -> OrderState | None:
        o = self.by_cl.get(er.cl_ord_id) or self.by_cl.get(er.orig_cl_ord_id) or self.by_oid.get(er.order_id)
        return o

    def apply(self, er: O.ExecReport) -> tuple[OrderState | None, str | None]:
        o = self.find(er)
        if o is None:
            self.unknown.append(er); return None, f"unknown order {er.cl_ord_id}/{er.order_id}"
        a = o.apply(er)
        if er.order_id:
            self.by_oid[er.order_id] = o
        if er.exec_type == O.EXEC_REPLACED:
            self.by_cl[er.cl_ord_id] = o
        return o, a

    def open(self) -> list[OrderState]:
        return [o for o in self.orders.values() if not o.terminal]
