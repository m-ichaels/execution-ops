"""FIX 4.4 tag=value messages: encoding with BodyLength and CheckSum, decoding with validation, frame splitting."""
from __future__ import annotations

import datetime as dt

SOH = "\x01"
BEGIN_STRING = "FIX.4.4"

# tags used across the stack
BeginString, BodyLength, CheckSum, MsgType, MsgSeqNum, SenderCompID, TargetCompID, SendingTime = 8, 9, 10, 35, 34, 49, 56, 52
PossDupFlag, OrigSendingTime, PossResend = 43, 122, 97
HeartBtInt, EncryptMethod, ResetSeqNumFlag, TestReqID, BeginSeqNo, EndSeqNo, NewSeqNo, GapFillFlag, Text, RefSeqNum, SessionRejectReason = 108, 98, 141, 112, 7, 16, 36, 123, 58, 45, 373
ClOrdID, OrigClOrdID, OrderID, ExecID, ExecType, OrdStatus, Symbol, Side, OrderQty, OrdType, Price, TimeInForce, TransactTime, HandlInst, Account = 11, 41, 37, 17, 150, 39, 55, 54, 38, 40, 44, 59, 60, 21, 1
LastQty, LastPx, LeavesQty, CumQty, AvgPx, OrdRejReason, CxlRejResponseTo, CxlRejReason, LastMkt, LastLiquidityInd, ExecRefID, ExecRestatementReason = 32, 31, 151, 14, 6, 103, 434, 102, 30, 851, 19, 378
TargetStrategy, TargetStrategyParameters, ParticipationRate, EffectiveTime, ExpireTime, Currency, SecurityExchange, ExecInst, MaxFloor = 847, 848, 849, 168, 126, 15, 207, 18, 111
# custom range for algo parameters
AlgoStart, AlgoEnd, AlgoUrgency, AlgoMaxPct, AlgoDropCopy = 20001, 20002, 20003, 20004, 20005

MSG = {"0": "Heartbeat", "1": "TestRequest", "2": "ResendRequest", "3": "Reject", "4": "SequenceReset", "5": "Logout", "A": "Logon", "D": "NewOrderSingle", "F": "OrderCancelRequest", "G": "OrderCancelReplaceRequest", "8": "ExecutionReport", "9": "OrderCancelReject", "j": "BusinessMessageReject"}
ADMIN_TYPES = {"0", "1", "2", "3", "4", "5", "A"}


def utc_timestamp(t: dt.datetime | None = None, millis: bool = True) -> str:
    t = t or dt.datetime.now(dt.timezone.utc)
    s = t.strftime("%Y%m%d-%H:%M:%S")
    return f"{s}.{t.microsecond // 1000:03d}" if millis else s


class FixMessage:
    __slots__ = ("fields",)

    def __init__(self, msg_type: str | None = None, fields: list[tuple[int, str]] | None = None):
        self.fields: list[tuple[int, str]] = list(fields or [])
        if msg_type is not None:
            self.set(MsgType, msg_type)

    # ---- field access ------------------------------------------------------------------------------------------------
    def get(self, tag: int, default=None):
        for t, v in self.fields:
            if t == tag:
                return v
        return default

    def get_all(self, tag: int) -> list[str]:
        return [v for t, v in self.fields if t == tag]

    def has(self, tag: int) -> bool:
        return any(t == tag for t, _ in self.fields)

    def num(self, tag: int, default: float = 0.0) -> float:
        v = self.get(tag)
        return float(v) if v not in (None, "") else default

    def int(self, tag: int, default: int = 0) -> int:
        v = self.get(tag)
        return int(v) if v not in (None, "") else default

    def set(self, tag: int, value) -> "FixMessage":
        s = format_value(value)
        for i, (t, _) in enumerate(self.fields):
            if t == tag:
                self.fields[i] = (tag, s)
                return self
        self.fields.append((tag, s))
        return self

    def remove(self, tag: int) -> "FixMessage":
        self.fields = [(t, v) for t, v in self.fields if t != tag]
        return self

    @property
    def msg_type(self) -> str:
        return self.get(MsgType, "")

    def body_fields(self) -> list[tuple[int, str]]:
        """everything but the standard header/trailer, in order"""
        skip = {BeginString, BodyLength, CheckSum, MsgType, MsgSeqNum, SenderCompID, TargetCompID, SendingTime, PossDupFlag, OrigSendingTime}
        return [(t, v) for t, v in self.fields if t not in skip]

    # ---- wire format -----------------------------------------------------------------------------------------------
    def encode(self, sender: str, target: str, seq_num: int, sending_time: str | None = None, poss_dup: bool = False, orig_sending_time: str | None = None) -> bytes:
        body = [f"{MsgType}={self.msg_type}", f"{SenderCompID}={sender}", f"{TargetCompID}={target}", f"{MsgSeqNum}={seq_num}", f"{SendingTime}={sending_time or utc_timestamp()}"]
        if poss_dup:
            body.append(f"{PossDupFlag}=Y")
            if orig_sending_time:
                body.append(f"{OrigSendingTime}={orig_sending_time}")
        body += [f"{t}={v}" for t, v in self.body_fields()]
        body_s = SOH.join(body) + SOH
        head = f"{BeginString}={BEGIN_STRING}{SOH}{BodyLength}={len(body_s.encode())}{SOH}"
        raw = (head + body_s).encode()
        return raw + f"{CheckSum}={checksum(raw):03d}{SOH}".encode()

    @classmethod
    def decode(cls, raw: bytes, validate: bool = True) -> "FixMessage":
        s = raw.decode("utf-8", errors="replace")
        parts = [p for p in s.split(SOH) if p]
        fields = []
        for p in parts:
            tag, _, val = p.partition("=")
            fields.append((int(tag), val))
        if validate:
            if not fields or fields[0][0] != BeginString or fields[0][1] != BEGIN_STRING:
                raise ValueError("bad BeginString")
            if len(fields) < 3 or fields[1][0] != BodyLength or fields[-1][0] != CheckSum:
                raise ValueError("bad framing")
            head_len = len(f"{BeginString}={fields[0][1]}{SOH}{BodyLength}={fields[1][1]}{SOH}".encode())
            body_len = int(fields[1][1])
            trailer = f"{CheckSum}={fields[-1][1]}{SOH}".encode()
            if head_len + body_len + len(trailer) != len(raw):
                raise ValueError(f"BodyLength mismatch: declared {body_len}, actual {len(raw) - head_len - len(trailer)}")
            if checksum(raw[:-len(trailer)]) != int(fields[-1][1]):
                raise ValueError("CheckSum mismatch")
        return cls(fields=fields)

    def __repr__(self) -> str:
        return "|".join(f"{t}={v}" for t, v in self.fields)


def checksum(raw: bytes) -> int:
    return sum(raw) % 256


def format_value(v) -> str:
    if isinstance(v, bool):
        return "Y" if v else "N"
    if isinstance(v, float):
        s = f"{v:.10f}".rstrip("0").rstrip(".")
        return s if s not in ("", "-0") else "0"
    if isinstance(v, dt.datetime):
        return utc_timestamp(v)
    return str(v)


def split_frames(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Complete frames (8=...|10=xxx|) and the unconsumed remainder."""
    frames = []
    while True:
        start = buffer.find(b"8=FIX")
        if start < 0:
            return frames, b""
        if start > 0:
            buffer = buffer[start:]
        bl = buffer.find(b"\x019=")
        if bl < 0:
            return frames, buffer
        end_bl = buffer.find(b"\x01", bl + 1)
        if end_bl < 0:
            return frames, buffer
        try:
            body_len = int(buffer[bl + 3:end_bl])
        except ValueError:
            buffer = buffer[1:]
            continue
        total = end_bl + 1 + body_len + 7          # "10=xxx" + SOH
        if len(buffer) < total:
            return frames, buffer
        frames.append(buffer[:total]); buffer = buffer[total:]
