"""FIX 4.4 engine: message framing on canonical vectors, session behaviour (heartbeats, test requests, gaps and resends,
silence and recovery, sequence persistence), the order state machine (transition table, invariants under random
report sequences), and the real-socket self-test."""
import random

import pytest

from xops.fix import message as M, orders as O
from xops.fix.message import FixMessage, checksum, split_frames
from xops.fix.session import Session
from xops.fix.state import OrderBook, OrderState
from xops.fix.transport import VirtualLink

SOH = "\x01"


# ---- messages --------------------------------------------------------------------------------------------------------
def test_encode_decode_round_trip_and_checksum():
    m = O.new_order_single("C1", "AAPL", O.SIDE_BUY, 100, 1_700_000_000.0, strategy="VWAP", max_pct=0.1)
    raw = m.encode("FUND", "BRKR", 7, sending_time="20231114-22:13:20.000")
    assert raw.startswith(b"8=FIX.4.4\x019=") and raw.endswith(SOH.encode())
    d = FixMessage.decode(raw)
    assert d.msg_type == "D" and d.get(M.ClOrdID) == "C1" and d.int(M.MsgSeqNum) == 7 and d.get(M.SenderCompID) == "FUND" and d.num(M.OrderQty) == 100
    body_len = int(d.get(M.BodyLength)); head = f"8=FIX.4.4{SOH}9={body_len}{SOH}".encode()
    assert len(raw) == len(head) + body_len + 7
    assert checksum(raw[:-7]) == int(d.get(M.CheckSum))


def test_canonical_message_checksum():
    # a hand-built logon: the checksum is the byte sum modulo 256 of everything before tag 10
    body = f"35=A{SOH}49=FUND{SOH}56=BRKR{SOH}34=1{SOH}52=20240101-00:00:00.000{SOH}98=0{SOH}108=30{SOH}"
    raw = f"8=FIX.4.4{SOH}9={len(body)}{SOH}{body}".encode()
    cs = sum(raw) % 256; full = raw + f"10={cs:03d}{SOH}".encode()
    m = FixMessage.decode(full); assert m.msg_type == "A" and m.int(M.HeartBtInt) == 30
    bad = raw + f"10={(cs + 1) % 256:03d}{SOH}".encode()
    with pytest.raises(ValueError):
        FixMessage.decode(bad)


def test_frame_splitting_handles_partial_and_concatenated_frames():
    a = FixMessage("0").encode("A", "B", 1, sending_time="20240101-00:00:00.000"); b = FixMessage("1").set(M.TestReqID, "x").encode("A", "B", 2, sending_time="20240101-00:00:00.000")
    frames, rest = split_frames(a + b[:10]); assert frames == [a] and rest == b[:10]
    frames, rest = split_frames(b[:10] + b[10:]); assert frames == [b] and rest == b""


# ---- sessions on the virtual link ------------------------------------------------------------------------------------
def pair(hb=5, latency=0.001):
    clock = {"t": 0.0}; link = VirtualLink(latency, latency); got = {"a": [], "b": []}
    A = Session("FUND", "BRKR", link.endpoint("a"), lambda: clock["t"], heartbeat=hb, initiator=True, on_app=lambda m, t: got["a"].append(m))
    B = Session("BRKR", "FUND", link.endpoint("b"), lambda: clock["t"], heartbeat=hb, initiator=False, on_app=lambda m, t: got["b"].append(m))
    link.endpoint("a").attach(A.on_bytes); link.endpoint("b").attach(B.on_bytes)

    def step(dt_=0.01):
        clock["t"] += dt_; link.now = clock["t"]; link.pump(clock["t"]); A.on_timer(); B.on_timer()
    return clock, link, A, B, got, step


def test_logon_orders_and_heartbeats():
    clock, link, A, B, got, step = pair()
    A.logon(); step(); step(); assert A.logged_on and B.logged_on
    for i in range(3):
        A.send(O.new_order_single(f"C{i}", "AAPL", O.SIDE_BUY, 100, clock["t"])); step()
    assert [m.get(M.ClOrdID) for m in got["b"]] == ["C0", "C1", "C2"]
    for _ in range(2000):
        step(0.01)          # 20 s idle: heartbeats every 5 s keep both sides alive
    assert A.logged_on and B.logged_on and not A.disconnected
    hb = sum(1 for _, _, mt in A.inbound_log if mt == "0"); assert hb >= 3


def test_gap_triggers_resend_and_possdup_is_ignored():
    clock, link, A, B, got, step = pair(); A.logon(); step(); step()
    link.faults["a"]["drop"] = 1
    A.send(O.new_order_single("C3", "MSFT", O.SIDE_BUY, 50, clock["t"])); step()
    A.send(O.new_order_single("C4", "MSFT", O.SIDE_BUY, 60, clock["t"]))
    for _ in range(5):
        step()
    assert [m.get(M.ClOrdID) for m in got["b"]] == ["C3", "C4"]
    kinds = [e.kind for e in B.events]; assert "gap_detected" in kinds and "resend_request_sent" in kinds and "gap_filled" in kinds
    assert A.n_resent >= 1 and B.in_seq == A.out_seq


def test_silence_then_recovery_delivers_every_fill_once():
    clock, link, A, B, got, step = pair(hb=30, latency=0.002); A.logon(); step(0.01); step(0.01)
    sent = []
    for t in range(1, 1200):
        if t % 5 == 0:
            eid = f"E{t}"; sent.append(eid); B.send(O.execution_report("O1", "C1", eid, O.EXEC_TRADE, O.ST_PARTIAL, "AAPL", "1", 10000, t, 100.0, clock["t"], last_qty=5, last_px=100.0))
        if t == 300:
            link.faults["b"]["silent"] = True
        if t == 600:
            link.faults["b"]["silent"] = False
        if A.disconnected and not link.faults["b"].get("silent"):
            A.logon(reset=False)
        step(1.0)
    for _ in range(30):
        step(1.0)
    ids = [m.get(M.ExecID) for m in got["a"]]
    assert sorted(ids) == sorted(sent) and len(ids) == len(set(ids))
    kinds = [e.kind for e in A.events]; assert "session_lost" in kinds and "gap_filled" in kinds and A.logged_on and B.logged_on


def test_sequence_numbers_persist_across_restart():
    store = {}
    clock, link, A, B, got, step = pair()
    A2 = Session("FUND", "BRKR", link.endpoint("a"), lambda: clock["t"], heartbeat=5, initiator=True, seq_store=store); link.endpoint("a").attach(A2.on_bytes)
    A2.logon(); step(); step(); A2.send(FixMessage("D").set(M.ClOrdID, "x")); step()
    assert store["out"] == A2.out_seq and store["in"] == A2.in_seq
    A3 = Session("FUND", "BRKR", link.endpoint("a"), lambda: clock["t"], heartbeat=5, initiator=True, seq_store=store)
    assert A3.out_seq == A2.out_seq and A3.in_seq == A2.in_seq


# ---- order state machine ----------------------------------------------------------------------------------------------
def er(cl, et, st, qty, cum, last, px=100.0, eid=None, symbol="AAPL", side="1", leaves=None):
    m = O.execution_report("OID", cl, eid or f"E{cum}{et}", et, st, symbol, side, qty, cum, px, 1.0, last_qty=last, last_px=px)
    if leaves is not None:
        m.set(M.LeavesQty, leaves)
    return O.ExecReport.parse(m, 1.0)


def test_state_machine_happy_path_and_anomalies():
    o = OrderState("C1", "AAPL", "1", 100, 0.0)
    assert o.apply(er("C1", O.EXEC_NEW, O.ST_NEW, 100, 0, 0)) is None and o.status == O.ST_NEW
    assert o.apply(er("C1", O.EXEC_TRADE, O.ST_PARTIAL, 100, 40, 40)) is None and o.leaves_qty == 60
    assert "duplicate" in o.apply(er("C1", O.EXEC_TRADE, O.ST_PARTIAL, 100, 40, 40))          # same ExecID again
    assert "overfill" in o.apply(er("C1", O.EXEC_TRADE, O.ST_FILLED, 100, 140, 100, eid="X"))
    assert "mismatch" in o.apply(er("C1", O.EXEC_TRADE, O.ST_PARTIAL, 100, 50, 10, eid="Y", side="2"))
    assert "LeavesQty" in o.apply(er("C1", O.EXEC_TRADE, O.ST_PARTIAL, 100, 50, 10, eid="Z", leaves=5))
    assert o.apply(er("C1", O.EXEC_TRADE, O.ST_FILLED, 100, 100, 60, eid="F")) is None and o.terminal and o.leaves_qty == 0
    assert "terminal" in o.apply(er("C1", O.EXEC_TRADE, O.ST_FILLED, 100, 100, 1, eid="G"))
    assert not o.check_invariants()


def test_state_machine_cancel_replace_chain():
    book = OrderBook(); o = OrderState("C1", "AAPL", "1", 100, 0.0); book.add(o)
    book.apply(er("C1", O.EXEC_NEW, O.ST_NEW, 100, 0, 0)); book.apply(er("C1", O.EXEC_TRADE, O.ST_PARTIAL, 100, 30, 30))
    o.request_replace("C1-R"); book.link("C1-R", o)
    r = O.ExecReport.parse(O.execution_report("OID", "C1-R", "E-R", O.EXEC_REPLACED, O.ST_PARTIAL, "AAPL", "1", 150, 30, 100.0, 1.0, orig_cl_ord_id="C1"), 1.0)
    st, a = book.apply(r); assert a is None and o.order_qty == 150 and o.cl_ord_id == "C1-R" and o.leaves_qty == 120
    o.request_cancel("C1-C"); book.link("C1-C", o)
    c = O.ExecReport.parse(O.execution_report("OID", "C1-C", "E-C", O.EXEC_CANCELED, O.ST_CANCELED, "AAPL", "1", 150, 30, 100.0, 1.0, orig_cl_ord_id="C1-R"), 1.0)
    st, a = book.apply(c); assert a is None and o.terminal and o.leaves_qty == 0 and o.cum_qty == 30


def test_state_machine_invariants_under_random_report_streams():
    rng = random.Random(3)
    for trial in range(200):
        qty = rng.choice([100, 1000, 12345]); o = OrderState("C", "AAPL", "1", qty, 0.0); o.apply(er("C", O.EXEC_NEW, O.ST_NEW, qty, 0, 0))
        cum = 0; k = 0
        while cum < qty and rng.random() < 0.9:
            last = min(qty - cum, rng.randint(1, max(1, qty // 3))); cum += last; k += 1
            o.apply(er("C", O.EXEC_TRADE, O.ST_FILLED if cum == qty else O.ST_PARTIAL, qty, cum, last, eid=f"E{k}"))
            assert not o.check_invariants(), o.check_invariants()
        if not o.terminal and rng.random() < 0.5:
            o.apply(er("C", O.EXEC_CANCELED, O.ST_CANCELED, qty, cum, 0, eid="CX"))
            assert o.terminal and o.leaves_qty == 0 and not o.check_invariants()
        assert o.cum_qty == cum and (cum == 0 or abs(o.avg_px - 100.0) < 1e-9)


def test_socket_self_test():
    from xops.serve import selftest
    assert selftest(port=9891, n_orders=8, verbose=False)
