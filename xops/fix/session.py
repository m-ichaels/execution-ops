"""FIX 4.4 session layer, independent of I/O and of the clock.

A `Session` is fed inbound bytes (`on_bytes`) and a periodic `on_timer(now)`; it writes outbound bytes through the
transport it is given and calls back with application messages.  Implemented: Logon / Logout handshakes, Heartbeat
and TestRequest with the 1.2 x HeartBtInt tolerance, MsgSeqNum checking with ResendRequest on a gap (later messages
are queued until the gap is filled), resend from the outbound store with PossDupFlag / OrigSendingTime and
SequenceReset-GapFill in place of admin messages, SequenceReset (reset mode), PossDup handling on the inbound side,
session-level Reject for malformed messages, and sequence-number persistence so a restart resumes where it stopped.
The clock is injected so the same code runs on wall time over TCP and on the simulator's virtual clock in-process."""
from __future__ import annotations

import datetime as dt
from collections import deque
from typing import Callable

from . import message as M
from .message import FixMessage, split_frames


class SessionEvent:
    __slots__ = ("t", "kind", "detail")

    def __init__(self, t: float, kind: str, detail: str = ""):
        self.t, self.kind, self.detail = t, kind, detail

    def __repr__(self):
        return f"{self.kind}@{self.t:.3f} {self.detail}"


class Session:
    def __init__(self, sender: str, target: str, transport, clock: Callable[[], float], *, heartbeat: int = 30, initiator: bool = True,
                 on_app: Callable[[FixMessage, float], None] | None = None, on_event: Callable[[SessionEvent], None] | None = None, seq_store: dict | None = None, reset_on_logon: bool = False):
        self.sender, self.target, self.transport, self.clock = sender, target, transport, clock
        self.heartbeat, self.initiator = heartbeat, initiator
        self.on_app, self.on_event = on_app, on_event
        self.seq_store = seq_store if seq_store is not None else {}
        self.reset_on_logon = reset_on_logon
        self.out_seq = int(self.seq_store.get("out", 1)); self.in_seq = int(self.seq_store.get("in", 1))
        self.logged_on = False; self.logout_sent = False; self.disconnected = False
        self._buf = b""
        self._store: dict[int, tuple[str, bytes, str]] = {}          # out seq -> (msg_type, encoded, sending_time)
        self._pending: dict[int, FixMessage] = {}                     # inbound messages received during a gap
        self._resend_pending: tuple[int, int] | None = None
        self._last_sent = self._last_recv = self.clock()
        self._test_req_at: float | None = None
        self.events: list[SessionEvent] = []
        self.n_sent = self.n_recv = self.n_resent = self.n_gap_fills = 0
        self.inbound_log: deque = deque(maxlen=100000)

    # ---- helpers -------------------------------------------------------------------------------------------------
    def _now_ts(self) -> str:
        return M.utc_timestamp(dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc))

    def _event(self, kind: str, detail: str = ""):
        e = SessionEvent(self.clock(), kind, detail); self.events.append(e)
        if self.on_event:
            self.on_event(e)

    def _persist(self):
        self.seq_store["out"] = self.out_seq; self.seq_store["in"] = self.in_seq

    # ---- outbound ------------------------------------------------------------------------------------------------
    def send(self, msg: FixMessage, lose: bool = False) -> int:
        """encode, store for resend, transmit; `lose=True` stores without transmitting (a message lost on the wire, for tests)"""
        ts = self._now_ts(); raw = msg.encode(self.sender, self.target, self.out_seq, sending_time=ts)
        self._store[self.out_seq] = (msg.msg_type, raw, ts); seq = self.out_seq; self.out_seq += 1; self._persist()
        if not lose:
            self.transport.send(raw); self._last_sent = self.clock(); self.n_sent += 1
        return seq

    def _send_raw_resend(self, seq: int):
        mt, raw, ts = self._store[seq]
        msg = FixMessage.decode(raw, validate=False)
        self.transport.send(msg.encode(self.sender, self.target, seq, sending_time=self._now_ts(), poss_dup=True, orig_sending_time=ts)); self.n_resent += 1

    def logon(self, heartbeat: int | None = None, reset: bool | None = None):
        if heartbeat:
            self.heartbeat = heartbeat
        reset = self.reset_on_logon if reset is None else reset
        self.disconnected = False; self.logout_sent = False; self._test_req_at = None; self._last_recv = self.clock()
        if reset:
            self.out_seq = 1; self.in_seq = 1; self._store.clear(); self._persist()
        m = FixMessage("A").set(M.EncryptMethod, 0).set(M.HeartBtInt, self.heartbeat)
        if reset:
            m.set(M.ResetSeqNumFlag, True)
        self.send(m); self._event("logon_sent")

    def logout(self, text: str = ""):
        m = FixMessage("5")
        if text:
            m.set(M.Text, text)
        self.send(m); self.logout_sent = True; self._event("logout_sent", text)

    def heartbeat_msg(self, test_req_id: str | None = None):
        m = FixMessage("0")
        if test_req_id:
            m.set(M.TestReqID, test_req_id)
        self.send(m)

    def test_request(self):
        rid = f"TR{int(self.clock() * 1000)}"; self.send(FixMessage("1").set(M.TestReqID, rid)); self._test_req_at = self.clock(); self._event("test_request_sent", rid)

    def resend_request(self, begin: int, end: int = 0):
        self.send(FixMessage("2").set(M.BeginSeqNo, begin).set(M.EndSeqNo, end)); self._resend_pending = (begin, end); self._event("resend_request_sent", f"{begin}-{end}")

    def reject(self, ref_seq: int, reason: int, text: str):
        self.send(FixMessage("3").set(M.RefSeqNum, ref_seq).set(M.SessionRejectReason, reason).set(M.Text, text)); self._event("reject_sent", text)

    # ---- inbound -------------------------------------------------------------------------------------------------
    def on_bytes(self, data: bytes):
        self._buf += data
        frames, self._buf = split_frames(self._buf)
        for f in frames:
            self._on_frame(f)

    def _on_frame(self, raw: bytes):
        self._last_recv = self.clock(); self.n_recv += 1
        try:
            msg = FixMessage.decode(raw, validate=True)
        except ValueError as e:
            self._event("garbled", str(e)); return
        if msg.get(M.SenderCompID) != self.target or msg.get(M.TargetCompID) != self.sender:
            self._event("compid_mismatch", repr(msg)[:80]); self.logout("CompID problem"); return
        seq = msg.int(M.MsgSeqNum); mt = msg.msg_type; poss_dup = msg.get(M.PossDupFlag) == "Y"
        self.inbound_log.append((self.clock(), seq, mt))
        # Logon and SequenceReset-Reset are processed before sequence checking
        if mt == "A" and not self.logged_on:
            if msg.get(M.ResetSeqNumFlag) == "Y":
                self.in_seq = 1; self.out_seq = 1; self._store.clear()
            self.heartbeat = msg.int(M.HeartBtInt, self.heartbeat) or self.heartbeat
        if mt == "4" and msg.get(M.GapFillFlag) != "Y":
            new = msg.int(M.NewSeqNo)
            if new >= self.in_seq:
                self._event("seq_reset", f"{self.in_seq}->{new}"); self.in_seq = new; self._persist()
            else:
                self.reject(seq, 5, "NewSeqNo below expected")
            return
        if seq > self.in_seq:
            # gap: queue this message, ask for the missing ones (once per gap)
            self._pending[seq] = msg
            if self._resend_pending is None:
                self._event("gap_detected", f"expected {self.in_seq} got {seq}")
                if mt == "A" and not self.logged_on:
                    self._handle_admin(msg)   # answer the logon first, then request the resend
                self.resend_request(self.in_seq, 0)
            return
        if seq < self.in_seq:
            if poss_dup:
                self._event("possdup_ignored", f"seq {seq}"); return
            self._event("seq_too_low", f"expected {self.in_seq} got {seq}"); self.logout(f"MsgSeqNum too low, expecting {self.in_seq} but received {seq}"); return
        self._dispatch(msg, seq)
        self._drain_pending()

    def _drain_pending(self):
        """drop queued messages the sequence has moved past, deliver the ones now in order, close the gap state"""
        for s in [s for s in self._pending if s < self.in_seq]:
            del self._pending[s]
        while self.in_seq in self._pending:
            m = self._pending.pop(self.in_seq); self._dispatch(m, self.in_seq)
        if self._resend_pending and not self._pending and self.in_seq > self._resend_pending[0]:
            self._resend_pending = None; self._event("gap_filled")

    def _dispatch(self, msg: FixMessage, seq: int):
        self.in_seq = seq + 1; self._persist()
        mt = msg.msg_type
        if mt in M.ADMIN_TYPES:
            self._handle_admin(msg)
        elif self.on_app:
            self.on_app(msg, self.clock())

    def _handle_admin(self, msg: FixMessage):
        mt = msg.msg_type
        if mt == "A":
            self._test_req_at = None
            if not self.logged_on:
                self.logged_on = True; self.disconnected = False; self._event("logged_on")
                if not self.initiator:
                    self.send(FixMessage("A").set(M.EncryptMethod, 0).set(M.HeartBtInt, self.heartbeat))
            elif not self.initiator:
                self._event("relogon"); self.send(FixMessage("A").set(M.EncryptMethod, 0).set(M.HeartBtInt, self.heartbeat))
        elif mt == "0":
            if msg.has(M.TestReqID):
                self._test_req_at = None; self._event("test_request_answered")
        elif mt == "1":
            self.heartbeat_msg(msg.get(M.TestReqID))
        elif mt == "2":
            self._on_resend_request(msg.int(M.BeginSeqNo), msg.int(M.EndSeqNo))
        elif mt == "4":   # gap fill
            new = msg.int(M.NewSeqNo)
            if new > self.in_seq - 1:
                self.in_seq = new; self._persist(); self.n_gap_fills += 1
            self._drain_pending()
        elif mt == "5":
            if not self.logout_sent:
                self.send(FixMessage("5")); self._event("logout_reply")
            self.logged_on = False; self.disconnected = True; self._event("logged_out", msg.get(M.Text, ""))
        elif mt == "3":
            self._event("reject_received", msg.get(M.Text, ""))

    def _on_resend_request(self, begin: int, end: int):
        end = end or self.out_seq - 1; self._event("resend_request_received", f"{begin}-{end}")
        seq = begin; gap_start = None
        while seq <= end:
            entry = self._store.get(seq)
            if entry is None or entry[0] in M.ADMIN_TYPES:
                if gap_start is None:
                    gap_start = seq
            else:
                if gap_start is not None:
                    self._gap_fill(gap_start, seq); gap_start = None
                self._send_raw_resend(seq)
            seq += 1
        if gap_start is not None:
            self._gap_fill(gap_start, end + 1)

    def _gap_fill(self, begin: int, new_seq: int):
        raw = FixMessage("4").set(M.GapFillFlag, True).set(M.NewSeqNo, new_seq).encode(self.sender, self.target, begin, sending_time=self._now_ts(), poss_dup=True)
        self.transport.send(raw); self.n_gap_fills += 1

    # ---- timers ------------------------------------------------------------------------------------------------------
    def on_timer(self, now: float | None = None):
        now = self.clock() if now is None else now
        if not self.logged_on or self.disconnected:
            return
        hb = self.heartbeat
        if now - self._last_sent >= hb:
            self.heartbeat_msg()
        if self._test_req_at is not None and now - self._test_req_at >= hb * 1.2:
            self._event("session_lost", "no reply to TestRequest"); self.disconnected = True; self.logged_on = False; return
        if self._test_req_at is None and now - self._last_recv >= hb * 1.2:
            self.test_request()

    def seconds_since_recv(self, now: float | None = None) -> float:
        return (self.clock() if now is None else now) - self._last_recv

    def status(self) -> dict:
        return {"logged_on": self.logged_on, "out_seq": self.out_seq, "in_seq": self.in_seq, "sent": self.n_sent, "received": self.n_recv, "resent": self.n_resent, "gap_fills": self.n_gap_fills, "pending_gap": self._resend_pending is not None, "disconnected": self.disconnected}
