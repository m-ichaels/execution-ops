"""Application messages of the order lifecycle: builders for D / G / F on the buy side, 8 / 9 on the broker side, and
the parsed view of an ExecutionReport."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from . import message as M
from .message import FixMessage

SIDE_BUY, SIDE_SELL, SIDE_SELL_SHORT = "1", "2", "5"
ORD_MARKET, ORD_LIMIT = "1", "2"
TIF_DAY, TIF_IOC, TIF_GTC = "0", "3", "1"
# ExecType (150) / OrdStatus (39)
EXEC_NEW, EXEC_DFD, EXEC_CANCELED, EXEC_REPLACED, EXEC_PENDING_CANCEL, EXEC_REJECTED, EXEC_PENDING_NEW, EXEC_EXPIRED, EXEC_RESTATED, EXEC_PENDING_REPLACE, EXEC_TRADE, EXEC_TRADE_CORRECT, EXEC_TRADE_CANCEL = "0", "3", "4", "5", "6", "8", "A", "C", "D", "E", "F", "G", "H"
ST_NEW, ST_PARTIAL, ST_FILLED, ST_DFD, ST_CANCELED, ST_REPLACED, ST_PENDING_CANCEL, ST_REJECTED, ST_PENDING_NEW, ST_EXPIRED, ST_PENDING_REPLACE = "0", "1", "2", "3", "4", "5", "6", "8", "A", "C", "E"
TERMINAL = {ST_FILLED, ST_DFD, ST_CANCELED, ST_REJECTED, ST_EXPIRED}
STATUS_NAME = {ST_NEW: "New", ST_PARTIAL: "PartiallyFilled", ST_FILLED: "Filled", ST_DFD: "DoneForDay", ST_CANCELED: "Canceled", ST_REPLACED: "Replaced", ST_PENDING_CANCEL: "PendingCancel", ST_REJECTED: "Rejected", ST_PENDING_NEW: "PendingNew", ST_EXPIRED: "Expired", ST_PENDING_REPLACE: "PendingReplace"}


def ts(t: float) -> str:
    return M.utc_timestamp(dt.datetime.fromtimestamp(t, dt.timezone.utc))


def new_order_single(cl_ord_id: str, symbol: str, side: str, qty: float, t: float, *, ord_type: str = ORD_MARKET, price: float | None = None, tif: str = TIF_DAY,
                     account: str = "FUND", exchange: str = "XNYS", currency: str = "USD", strategy: str = "VWAP", start: float | None = None, end: float | None = None, max_pct: float | None = None, urgency: str = "") -> FixMessage:
    m = FixMessage("D").set(M.ClOrdID, cl_ord_id).set(M.Account, account).set(M.HandlInst, "1").set(M.Symbol, symbol).set(M.SecurityExchange, exchange).set(M.Currency, currency)
    m.set(M.Side, side).set(M.TransactTime, ts(t)).set(M.OrderQty, qty).set(M.OrdType, ord_type).set(M.TimeInForce, tif).set(M.TargetStrategy, strategy)
    if price is not None:
        m.set(M.Price, price)
    if start is not None:
        m.set(M.EffectiveTime, ts(start)).set(M.AlgoStart, ts(start))
    if end is not None:
        m.set(M.ExpireTime, ts(end)).set(M.AlgoEnd, ts(end))
    if max_pct is not None:
        m.set(M.ParticipationRate, max_pct).set(M.AlgoMaxPct, max_pct)
    if urgency:
        m.set(M.AlgoUrgency, urgency)
    return m


def cancel_replace(orig_cl_ord_id: str, cl_ord_id: str, order_id: str, symbol: str, side: str, qty: float, t: float, *, ord_type: str = ORD_MARKET, price: float | None = None, max_pct: float | None = None) -> FixMessage:
    m = FixMessage("G").set(M.OrigClOrdID, orig_cl_ord_id).set(M.ClOrdID, cl_ord_id).set(M.OrderID, order_id).set(M.HandlInst, "1").set(M.Symbol, symbol).set(M.Side, side).set(M.TransactTime, ts(t)).set(M.OrderQty, qty).set(M.OrdType, ord_type)
    if price is not None:
        m.set(M.Price, price)
    if max_pct is not None:
        m.set(M.ParticipationRate, max_pct)
    return m


def cancel_request(orig_cl_ord_id: str, cl_ord_id: str, order_id: str, symbol: str, side: str, qty: float, t: float) -> FixMessage:
    return FixMessage("F").set(M.OrigClOrdID, orig_cl_ord_id).set(M.ClOrdID, cl_ord_id).set(M.OrderID, order_id).set(M.Symbol, symbol).set(M.Side, side).set(M.TransactTime, ts(t)).set(M.OrderQty, qty)


def execution_report(order_id: str, cl_ord_id: str, exec_id: str, exec_type: str, status: str, symbol: str, side: str, order_qty: float, cum_qty: float, avg_px: float, t: float, *,
                     last_qty: float = 0.0, last_px: float = 0.0, orig_cl_ord_id: str | None = None, text: str = "", reject_reason: int | None = None, last_mkt: str = "", liquidity: str = "", exec_ref_id: str = "") -> FixMessage:
    leaves = 0.0 if status in TERMINAL else max(0.0, order_qty - cum_qty)
    m = FixMessage("8").set(M.OrderID, order_id).set(M.ClOrdID, cl_ord_id).set(M.ExecID, exec_id).set(M.ExecType, exec_type).set(M.OrdStatus, status).set(M.Symbol, symbol).set(M.Side, side)
    m.set(M.OrderQty, order_qty).set(M.LastQty, last_qty).set(M.LastPx, last_px).set(M.LeavesQty, leaves).set(M.CumQty, cum_qty).set(M.AvgPx, avg_px).set(M.TransactTime, ts(t))
    if orig_cl_ord_id:
        m.set(M.OrigClOrdID, orig_cl_ord_id)
    if text:
        m.set(M.Text, text)
    if reject_reason is not None:
        m.set(M.OrdRejReason, reject_reason)
    if last_mkt:
        m.set(M.LastMkt, last_mkt)
    if liquidity:
        m.set(M.LastLiquidityInd, liquidity)
    if exec_ref_id:
        m.set(M.ExecRefID, exec_ref_id)
    return m


def cancel_reject(order_id: str, cl_ord_id: str, orig_cl_ord_id: str, status: str, response_to: str, reason: int, text: str, t: float) -> FixMessage:
    return FixMessage("9").set(M.OrderID, order_id).set(M.ClOrdID, cl_ord_id).set(M.OrigClOrdID, orig_cl_ord_id).set(M.OrdStatus, status).set(M.CxlRejResponseTo, response_to).set(M.CxlRejReason, reason).set(M.Text, text).set(M.TransactTime, ts(t))


@dataclass
class ExecReport:
    order_id: str; cl_ord_id: str; orig_cl_ord_id: str; exec_id: str; exec_type: str; status: str; symbol: str; side: str
    order_qty: float; last_qty: float; last_px: float; leaves_qty: float; cum_qty: float; avg_px: float; transact_time: str; text: str = ""; last_mkt: str = ""; liquidity: str = ""
    seq: int = 0; recv_time: float = 0.0; raw: FixMessage = field(default=None, repr=False)

    @classmethod
    def parse(cls, m: FixMessage, recv_time: float = 0.0) -> "ExecReport":
        return cls(m.get(M.OrderID, ""), m.get(M.ClOrdID, ""), m.get(M.OrigClOrdID, ""), m.get(M.ExecID, ""), m.get(M.ExecType, ""), m.get(M.OrdStatus, ""), m.get(M.Symbol, ""), m.get(M.Side, ""),
                   m.num(M.OrderQty), m.num(M.LastQty), m.num(M.LastPx), m.num(M.LeavesQty), m.num(M.CumQty), m.num(M.AvgPx), m.get(M.TransactTime, ""), m.get(M.Text, ""), m.get(M.LastMkt, ""), m.get(M.LastLiquidityInd, ""), m.int(M.MsgSeqNum), recv_time, m)

    @property
    def is_fill(self) -> bool:
        return self.exec_type == EXEC_TRADE and self.last_qty > 0
