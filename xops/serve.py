"""The FIX layer over real TCP sockets on the wall clock: a simulated-broker acceptor that any FIX 4.4 initiator can
connect to (`xops serve`), and the loopback self-test (`xops selftest`) that runs the same Session class on both ends
over sockets: logon, orders, acks and fills, heartbeats, a lost message recovered through ResendRequest / gap fill,
and a clean logout."""
from __future__ import annotations

import datetime as dt
import threading
import time

import numpy as np

from . import data, portfolio as P
from .broker import BrokerSim
from .fix import message as M, orders as O
from .fix.message import FixMessage
from .fix.session import Session
from .fix.transport import SocketTransport, connect, listen
from .market import DayMarket


class WallClock:
    def __call__(self) -> float:
        return time.time()


def serve(port: int = 9880, d: dt.date | None = None, speed: float = 60.0):
    """Accept one FIX 4.4 initiator and run the simulated broker for date d, one virtual minute per wall second by default."""
    px = data.load_prices(start="2026-01-01"); uni = data.load_universe()
    d = d or dt.date.fromisoformat(str(px["date"].max())[:10])
    bars = px[px["date"] == d.isoformat()]; stats = P.symbol_stats(px, d)
    rng = np.random.default_rng(1); market = DayMarket(d, bars, stats, uni, rng, kappa=0.3, psi=0.4)
    t_open = min(s.t_open for s in market.symbols.values()); start = time.time()
    vclock = lambda: t_open - 600 + (time.time() - start) * speed
    ls = listen(port); print(f"[xops serve] FIX 4.4 acceptor on 127.0.0.1:{port}, broker day {d}, {len(market.symbols)} symbols, {speed:.0f}x time; CompIDs CETK/FUND -> use SenderCompID=FUND TargetCompID=BRKR", flush=True)
    sock, _ = ls.accept(); tr = SocketTransport(sock)
    sess = Session("BRKR", "FUND", tr, vclock, heartbeat=30, initiator=False)
    bs = BrokerSim("BRKR", market, vclock, sess, None, rng=rng, quality=1.0)
    sess.on_app = bs.on_app; lock = threading.Lock()

    def on_bytes(b):
        with lock:
            sess.on_bytes(b)
    tr.reader(on_bytes)
    last_min = None
    try:
        while not sess.disconnected and not tr.closed:
            with lock:
                now = vclock(); k = int(now // 60)
                if k != last_min:
                    bs.step(now); last_min = k
                bs.flush(now); sess.on_timer(now)
            time.sleep(0.05)
    finally:
        tr.close(); ls.close(); print("[xops serve] session ended", bs.stats(), flush=True)


def selftest(port: int = 9881, n_orders: int = 20, verbose: bool = True) -> bool:
    """Same session code on both ends of a real socket.  The acceptor answers orders with New and two fills, and once
    skips a sequence number to simulate a lost message; the initiator must recover it through the resend protocol."""
    clock = WallClock(); ls = listen(port); received = []; acc_state = {}

    def acceptor():
        sock, _ = ls.accept(); tr = SocketTransport(sock); lock = threading.Lock()
        s = Session("BRKR", "FUND", tr, clock, heartbeat=2, initiator=False); acc_state["session"] = s; eid = [0]

        def on_app(m, t):
            if m.msg_type != "D":
                return
            oid = "O" + m.get(M.ClOrdID); cl = m.get(M.ClOrdID); q = m.num(M.OrderQty)
            s.send(O.execution_report(oid, cl, f"E{eid[0]}", O.EXEC_NEW, O.ST_NEW, m.get(M.Symbol), m.get(M.Side), q, 0, 0, t)); eid[0] += 1
            # the partial fill of C5 is lost on the wire: stored but never transmitted, so the initiator must recover it
            s.send(O.execution_report(oid, cl, f"E{eid[0]}", O.EXEC_TRADE, O.ST_PARTIAL, m.get(M.Symbol), m.get(M.Side), q, q / 2, 100.0, t, last_qty=q / 2, last_px=100.0), lose=(cl == "C5")); eid[0] += 1
            s.send(O.execution_report(oid, cl, f"E{eid[0]}", O.EXEC_TRADE, O.ST_FILLED, m.get(M.Symbol), m.get(M.Side), q, q, 100.0, t, last_qty=q / 2, last_px=100.0)); eid[0] += 1
        s.on_app = on_app

        def on_bytes(b):
            with lock:
                s.on_bytes(b)
        tr.reader(on_bytes)
        while not s.disconnected and not tr.closed:
            with lock:
                s.on_timer()
            time.sleep(0.02)
        tr.close()

    th = threading.Thread(target=acceptor, daemon=True); th.start()
    sock = connect(port); tr = SocketTransport(sock); lock = threading.Lock()
    fills = []; init = Session("FUND", "BRKR", tr, clock, heartbeat=2, initiator=True, on_app=lambda m, t: fills.append((m.get(M.ClOrdID), m.get(M.ExecID), m.get(M.ExecType))))

    def on_bytes(b):
        with lock:
            init.on_bytes(b)
    tr.reader(on_bytes)
    with lock:
        init.logon()
    t0 = time.time()
    while not init.logged_on and time.time() - t0 < 5:
        time.sleep(0.01)
    for i in range(n_orders):
        with lock:
            init.send(O.new_order_single(f"C{i}", "AAPL", O.SIDE_BUY, 100, time.time()))
        time.sleep(0.02)
    t0 = time.time()
    while time.time() - t0 < 6:
        with lock:
            init.on_timer()
        if len([f for f in fills if f[2] == O.EXEC_TRADE]) >= 2 * n_orders:
            time.sleep(2.5)     # let heartbeats and test requests run
            break
        time.sleep(0.02)
    with lock:
        init.logout("self-test done")
    t0 = time.time()
    while not init.disconnected and time.time() - t0 < 3:
        time.sleep(0.02)
    tr.close(); th.join(timeout=3); ls.close()
    trades = [f for f in fills if f[2] == O.EXEC_TRADE]; news = [f for f in fills if f[2] == O.EXEC_NEW]
    ok = len(news) == n_orders and len(trades) == 2 * n_orders and len(set(f[1] for f in fills)) == len(fills) and init.status()["pending_gap"] is False
    ev = [e.kind for e in init.events]
    if verbose:
        print(f"orders {n_orders}: acks {len(news)}, fills {len(trades)}, unique exec ids {len(set(f[1] for f in fills))}, gap events {[e for e in ev if 'gap' in e or 'resend' in e]}, heartbeats received {sum(1 for _, _, mt in init.inbound_log if mt == '0')}, logged out {init.disconnected}")
    return ok and "gap_detected" in ev and "gap_filled" in ev
