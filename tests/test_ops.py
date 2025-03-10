"""Portfolio-to-orders, the market model, the monitor rules, the reconciliation SQL on a synthetic day, TCA identities and
the pre-trade model; one short engine day on the committed data (fault injection and drop-copy agreement)."""
import datetime as dt
import os

import duckdb
import numpy as np
import pandas as pd
import pytest

from xops import data, market as MK, portfolio as P, recon as R, strategies as S, tca as T
from xops.monitor import Monitor, Thresholds

D = dt.date


def securities():
    return pd.DataFrame({"symbol": ["AAA", "BBB", "CCC", "BA"], "exchange": ["XNYS", "XNYS", "XLON", "XNYS"], "currency": ["USD", "USD", "GBp", "USD"], "region": ["US", "US", "EU", "US"], "price_scale": [1, 1, 0.01, 1], "lot_size": [1, 1, 1, 1]})


def stats():
    return pd.DataFrame({"symbol": ["AAA", "BBB", "CCC", "BA"], "close_prev": [100.0, 50.0, 2000.0, 200.0], "adv": [1e6, 2e5, 5e6, 1e6], "vol_day": [0.02, 0.03, 0.015, 0.02], "spread_bp": [2.0, 5.0, 3.0, 2.0], "dollar_adv": [1e8, 1e7, 1e8, 2e8], "last_date": ["2026-09-15"] * 4}).set_index("symbol")


def test_netting_rounding_caps_restricted_and_locates():
    d = D(2026, 9, 16); cfg = P.Config(max_pct_adv=0.10, min_notional=10_000, restricted=("BA",), no_locate=("CCC",))
    targets = {"MOM": pd.Series({"AAA": 1000.0, "BBB": 50_000.0, "BA": 500.0, "CCC": -1000.0}), "REV": pd.Series({"AAA": -300.0, "BBB": 10.0})}
    positions = {"MOM": {"AAA": 0.0, "BBB": 0.0}, "REV": {"AAA": 0.0}}
    orders, carry, rep = P.build_orders(d, targets, positions, stats(), securities(), cfg, strategy_specs=S.STRATEGIES, rng=np.random.default_rng(0))
    by = {o.symbol: o for o in orders}
    assert by["AAA"].qty == 700 and by["AAA"].side == "1" and by["AAA"].crossed == 300           # 1000 buy vs 300 sell nets to 700, 300 crossed internally
    assert by["BBB"].qty == 20_000 and rep["capped"] == 1 and any(c["symbol"] == "BBB" for c in carry)   # 10% of 200k ADV
    assert "BA" not in by and rep["restricted"] == ["BA"]
    assert "CCC" not in by and rep["locate_failed"] == ["CCC"]
    alloc = P.allocate_fills(by["AAA"], 700); assert abs(alloc["MOM"] - 1000) < 1e-9 and abs(alloc["REV"] + 300) < 1e-9
    assert by["AAA"].start_time < by["AAA"].end_time and by["AAA"].algo in ("VWAP", "IS", "POV", "CLOSE")


def test_session_times_use_exchange_zone_and_early_close():
    o, c = P.session_times("XNYS", D(2026, 9, 16)); assert (c - o) / 60 == 390
    o2, c2 = P.session_times("XNYS", D(2026, 11, 27)); assert (c2 - o2) / 60 == 210
    ol, cl_ = P.session_times("XLON", D(2026, 9, 16)); assert ol < o and (cl_ - ol) / 60 == 510


def market_for(seed=0):
    d = D(2026, 9, 16); bars = pd.DataFrame({"symbol": ["AAA"], "date": [d.isoformat()], "open": [100.0], "high": [101.0], "low": [99.0], "close": [101.0], "adjclose": [101.0], "volume": [1e6]})
    return MK.DayMarket(d, bars, stats(), securities(), np.random.default_rng(seed), kappa=0.3, psi=0.4)


def test_market_bridge_and_impact_are_sane():
    m = market_for(); s = m.symbols["AAA"]
    assert abs(s.mid[0] - 100.0) < 1e-9 and abs(s.mid[-1] - 101.0) < 1e-9 and abs(s.vol_curve.sum() - 1e6) < 1e-6
    t = s.t_open + 3600; mid = s.mid_at(t)
    f1, p1, c1 = m.execute("AAA", +1, 100, t); f2, p2, c2 = m.execute("AAA", +1, 2000, t + 60)
    assert f1 == 100 and p1 > mid and c2 > c1                          # bigger child, bigger temporary impact
    assert s.perm_shift > 0                                             # buying pushes the mid up
    big = m.execute("AAA", +1, 1e7, t + 120); assert big[0] < 1e7      # capped by the minute's volume
    assert abs(s.vwap(s.t_open, s.t_close - 1) - (s.mid * s.vol_curve).sum() / s.vol_curve.sum()) < 1e-9


class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t


def test_monitor_flags_unacked_stalled_collar_and_anomaly():
    m = market_for(); s = m.symbols["AAA"]; clock = Clock(s.t_open)
    spec = P.OrderSpec("C1", "2026-09-16", "MOM", "AAA", "XNYS", "USD", "1", 10_000, "VWAP", "low", 0.2, s.t_open + 300, s.t_close - 300, 100.0, 1e6, 2.0, 0.02, "BRK1", 1)
    mon = Monitor(clock, m, {"C1": spec}, {}, Thresholds())
    mon.on_order_sent(spec, s.t_open); clock.t = s.t_open + 20; mon.step(clock.t)
    assert [a["rule"] for a in mon.alerts] == ["UNACKED"]
    from xops.fix import orders as O
    ack = O.execution_report("O1", "C1", "E0", O.EXEC_NEW, O.ST_NEW, "AAA", "1", 10_000, 0, 0, clock.t); mon.on_app("BRK1", ack, clock.t)
    fill = O.execution_report("O1", "C1", "E1", O.EXEC_TRADE, O.ST_PARTIAL, "AAA", "1", 10_000, 100, 100.0, clock.t, last_qty=100, last_px=s.mid_at(clock.t) * 1.05); mon.on_app("BRK1", fill, clock.t)
    assert "PRICE_COLLAR" in [a["rule"] for a in mon.alerts]
    dup = O.execution_report("O1", "C1", "E1", O.EXEC_TRADE, O.ST_PARTIAL, "AAA", "1", 10_000, 200, 100.0, clock.t, last_qty=100, last_px=100.0); mon.on_app("BRK1", dup, clock.t)
    assert "STATE_ANOMALY" in [a["rule"] for a in mon.alerts]
    clock.t = s.t_open + 2 * 3600; mon.step(clock.t)                    # nothing filled for two hours while VWAP expected progress
    assert "STALLED" in [a["rule"] for a in mon.alerts]


def test_recon_finds_and_classifies_every_seeded_break(tmp_path):
    con = data.connect(str(tmp_path / "t.duckdb"), fresh=True); d = D(2026, 9, 16); rng = np.random.default_rng(1)
    n = 200; ex = pd.DataFrame({"date": [d] * n, "source": ["internal"] * n, "exec_id": [f"B-E{i}" for i in range(n)], "cl_ord_id": [f"C{i % 20}" for i in range(n)], "order_id": [f"O{i % 20}" for i in range(n)], "symbol": ["AAA", "BRK-B", "BBB", "META"] * 50, "side": [("1" if (i // 4) % 2 == 0 else "2") for i in range(n)], "qty": [100.0] * n, "px": [100.0 + i * 0.01 for i in range(n)], "time": [1.0] * n, "broker": ["B"] * n, "liquidity": ["R"] * n, "seq": [0] * n})
    dc = ex.copy(); dc["source"] = "dropcopy"
    con.execute("insert into executions select * from ex"); con.execute("insert into executions select * from dc")
    pos = pd.DataFrame({"date": [d] * 2, "account": ["FUND"] * 2, "symbol": ["AAA", "AAA"], "qty": [0.0, 0.0], "source": ["SOD", "EOD"]}); con.execute("insert into positions select * from pos")
    pb, seeded, late = R.simulate_pb_file(d, dc, securities().assign(symbol=["AAA", "BRK-B", "BBB", "META"]), rng)
    pbf = pb.copy(); pbf["date"] = d; pbf = pbf[["date", "trade_id", "exec_id", "symbol", "side", "qty", "px", "trade_date", "settle_date", "broker"]]; con.execute("insert into pb_file select * from pbf")
    breaks = R.run_recon(con, d, seeded); score = R.score_recon(breaks, seeded)
    assert score["seeded"] == 11 and score["found"] == 11, score
    assert score["unexplained"] == 0, score                                                        # every symbol nets to zero, so no position break either
    con.close()


def test_tca_identity_and_pretrade_fit():
    n = 120; rng = np.random.default_rng(2)
    o = pd.DataFrame({"date": [D(2026, 9, 1) + dt.timedelta(days=i % 30) for i in range(n)], "side": rng.choice(["1", "2"], n), "qty": 1000.0, "cum_qty": rng.choice([1000.0, 600.0], n), "decision_px": 100.0, "arrival_px": 100.2, "vwap_px": 100.3, "close_px": 100.5, "adv": 1e6, "vol_day": 0.02, "spread_bp": 3.0, "algo": rng.choice(["VWAP", "IS"], n), "difficulty": 1, "broker": rng.choice(["B1", "B2"], n), "exchange": "XNYS", "urgency": "low"})
    o["avg_px"] = 100.2 * (1 + np.where(o["side"] == "1", 1, -1) * (5 + 0.02 * 1e4 * 0.6 * np.sqrt(o["qty"] / o["adv"]) + rng.normal(0, 2, n)) * 1e-4)
    t = T.order_tca(o)
    assert np.allclose(t["is_bp"], t["is_delay_bp"] + t["is_execution_bp"] + t["is_opportunity_bp"] + t["is_fees_bp"], atol=1e-9)
    full = t[t["cum_qty"] == 1000.0]; assert np.allclose(full["is_opportunity_bp"], 0.0) and np.allclose(full["is_fees_bp"], T.FEE_BP)
    s = T.summarize(t); assert s["n_orders"] == n and "VWAP" in s["by_algo"]
    w = T.wheel(t, ["B1", "B2"], B=100); assert set(w["brokers"]) == {"B1", "B2"} and w["power"]["1.0bp"] > w["power"]["2.0bp"]
    p = T.pretrade_model(t); assert p["n_test"] > 0 and p["test_rmse_bp"] < 12


@pytest.mark.skipif(not os.path.exists(os.path.join(data.DER, "prices.parquet")), reason="derived data not present")
def test_engine_day_on_real_data_with_faults():
    from xops import engine as E, faults as F
    px = data.load_prices(start="2025-06-01", end="2026-09-16"); uni = data.load_universe(); ev = data.load_events()
    adj = S.panel(px, "adjclose"); close = S.panel(px, "close"); region = uni.set_index("symbol")["region"]
    cfg = P.Config(); state = E.FundState()
    E.run_day(D(2026, 9, 14), px, uni, state, cfg, adj=adj, close=close, region=region, events=ev, warm=True)
    r = E.run_day(D(2026, 9, 15), px, uni, state, cfg, adj=adj, close=close, region=region, events=ev, fault_mix={"drop_ack": 1, "bad_price": 1, "dup_fill": 1, "seq_gap": 1}, seed=3)
    assert r is not None and len(r.orders) > 20 and len(r.exec_rows) > 1000
    ids_int = {e["exec_id"] for e in r.exec_rows}; ids_dc = {e["exec_id"] for e in r.dropcopy_rows}
    assert ids_int == ids_dc                                                                    # drop copy agrees with the blotter on a day without drop-copy faults
    det = F.summarize(r.detections); assert det["injected"] == 4 and det["detected"] == 4, det
    assert all(s["pending_gap"] is False for s in r.session_stats.values())
    eod = sum(sum(v.values()) for v in r.positions_eod.values()); sod = sum(sum(v.values()) for v in r.positions_sod.values())
    net = sum((e["qty"] if e["side"] == "1" else -e["qty"]) for e in r.exec_rows); assert abs((eod - sod) - net) < 1e-6
