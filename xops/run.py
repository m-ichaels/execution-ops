"""Multi-day pipeline: warm-up, then one day at a time through the engine, the store, the reconciliation and the checks;
fault days alternate with clean days so that both detection and false-alert rates are measured.  Writes the DuckDB
tables and results/*.json."""
from __future__ import annotations

import datetime as dt
import json
import os
import time

import numpy as np
import pandas as pd

from . import calendar as cal, checks as CK, data, engine as E, faults as F, portfolio as P, recon as R, strategies as S, tca as T

RESULTS = os.path.join(data.ROOT, "results")


def to_json(obj, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)

    def default(o):
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return None if np.isnan(o) else float(o)
        if isinstance(o, (dt.date, dt.datetime)):
            return o.isoformat()
        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, set):
            return sorted(o)
        return str(o)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, default=default)


def write_day(con, r: E.DayResult, state: E.FundState, seeded: list, breaks: pd.DataFrame, pb: pd.DataFrame, checks: list, dets_summary: dict):
    d = r.date
    for tbl in ("orders", "executions", "positions", "alerts", "faults", "pb_file", "recon_breaks", "checks", "market_minutes"):
        con.execute(f"delete from {tbl} where date = ?", [d])
    rows = []
    for o in r.orders:
        st = r.specs[o.cl_ord_id]; ob = None
        sym = r.market.symbols.get(o.symbol)
        vwap = sym.vwap(o.start_time, o.end_time - 1) if sym else None; close = r.market.close_price(o.symbol) if sym else None
        rows.append((d, o.cl_ord_id, "", o.strategy, o.symbol, o.exchange, o.side, o.qty, o.algo, o.broker, o.urgency, o.max_pct, o.decision_px, None, o.start_time - 60, o.start_time, o.end_time, "", 0.0, 0.0, o.adv, o.spread_bp, o.vol_day, o.difficulty, 0, None, vwap, close))
    df = pd.DataFrame(rows, columns=["date", "cl_ord_id", "order_id", "strategy", "symbol", "exchange", "side", "qty", "algo", "broker", "urgency", "max_pct", "decision_px", "arrival_px", "arrival_time", "start_time", "end_time", "status", "cum_qty", "avg_px", "adv", "spread_bp", "vol_day", "difficulty", "n_fills", "ack_latency_ms", "vwap_px", "close_px"])
    # fill in what the monitor's state machines know
    mon_rows = {}
    for o in r.mon_orders:
        mon_rows[o["cl_ord_id"]] = o
    for i, row in df.iterrows():
        m = mon_rows.get(row["cl_ord_id"])
        if m:
            df.at[i, "order_id"] = m["order_id"]; df.at[i, "status"] = m["status"]; df.at[i, "cum_qty"] = m["cum_qty"]; df.at[i, "avg_px"] = m["avg_px"]; df.at[i, "n_fills"] = m["n_fills"]; df.at[i, "arrival_px"] = m["arrival_px"]; df.at[i, "ack_latency_ms"] = m["ack_latency_ms"]
    con.execute("insert into orders select * from df")
    ex = pd.DataFrame(r.exec_rows + r.dropcopy_rows)
    if len(ex):
        ex["seq"] = 0; ex = ex[["date", "source", "exec_id", "cl_ord_id", "order_id", "symbol", "side", "qty", "px", "time", "broker", "liquidity", "seq"]]
        con.execute("insert into executions select * from ex")
    pos = []
    for src, book in (("SOD", r.positions_sod), ("EOD", r.positions_eod)):
        fund = {}
        for strat, cur in book.items():
            for s, q in cur.items():
                pos.append((d, strat, s, q, src)); fund[s] = fund.get(s, 0.0) + q
        for s, q in fund.items():
            pos.append((d, "FUND", s, q, src))
    if pos:
        pdf = pd.DataFrame(pos, columns=["date", "account", "symbol", "qty", "source"]); con.execute("insert into positions select * from pdf")
    if r.alerts:
        al = pd.DataFrame([(d, a["time"], a["rule"], a["severity"], a["cl_ord_id"], a["symbol"], a["detail"][:300]) for a in r.alerts], columns=["date", "time", "rule", "severity", "cl_ord_id", "symbol", "detail"]); con.execute("insert into alerts select * from al")
    if r.detections:
        fd = pd.DataFrame([(d, x.t_fault, x.type, x.cl_ord_id, r.specs[x.cl_ord_id].symbol if x.cl_ord_id in r.specs else "", x.t_alert, x.rule) for x in r.detections], columns=["date", "time", "type", "cl_ord_id", "symbol", "detected_time", "detected_rule"]); con.execute("insert into faults select * from fd")
    if len(pb):
        pbf = pb.copy(); pbf["date"] = pd.to_datetime(d).date(); pbf = pbf[["date", "trade_id", "exec_id", "symbol", "side", "qty", "px", "trade_date", "settle_date", "broker"]]; con.execute("insert into pb_file select * from pbf")
    if len(breaks):
        con.execute("insert into recon_breaks select * from breaks")
    if checks:
        ck = pd.DataFrame(checks)[["date", "phase", "check_name", "status", "detail"]]; con.execute("insert into checks select * from ck")
    mm = r.market.minutes_table()
    if len(mm):
        con.execute("insert into market_minutes select * from mm")


def tca_from_store(con, start, end, brokers):
    orders = con.execute("select * from orders where date between ? and ?", [start, end]).df()
    tca = T.order_tca(orders); summary = T.summarize(tca); wheel = T.wheel(tca, brokers); wheel_all = T.wheel(tca, brokers, algos=("VWAP", "POV", "CLOSE", "TWAP", "IS"), y="vwap_bp"); pre = T.pretrade_model(tca)
    wheel["all_orders_vs_vwap"] = {k: v for k, v in wheel_all.items() if k in ("n", "residual_sd_bp", "brokers", "power", "ranking")}
    return tca, summary, wheel, pre


def run_range(start: dt.date, end: dt.date, *, fault_days: str = "alternate", seed: int = 1, cfg: P.Config | None = None, db_path: str | None = None, verbose: bool = True) -> dict:
    cfg = cfg or P.Config()
    con = data.connect(db_path or data.DB_PATH)
    if con.execute("select count(*) from prices").fetchone()[0] == 0:
        data.build_store(con)
    prices = data.load_prices(start=(start - dt.timedelta(days=420)).isoformat(), end=end.isoformat()); uni = data.load_universe(); events = data.load_events()
    adj = S.panel(prices, "adjclose"); close = S.panel(prices, "close"); region = uni.set_index("symbol")["region"]
    state = E.FundState(); rng = np.random.default_rng(seed)
    # warm-up: the book as of the day before the window
    warm_day = cal.next_trading_day("XNYS", start, -1)
    E.run_day(warm_day, prices, uni, state, cfg, adj=adj, close=close, region=region, events=events, warm=True)
    days = sorted({d for ex in ("XNYS", "XLON", "XETR") for d in cal.trading_days(ex, start, end)})
    per_day = []; late_prev = pd.DataFrame(); prev_missing: set[str] = set(); all_dets = []; clean_alerts = []; n_clean = 0; t0 = time.time()
    for i, d in enumerate(days):
        fault = (i % 2 == 1) if fault_days == "alternate" else (fault_days == "all")
        mix = F.DEFAULT_MIX if fault else None
        stats_now = P.symbol_stats(prices, d)

        def checks_fn(phase, dd, st, stats, orders, bars, mon=None, sessions=None):
            if phase == "SOD":
                return CK.sod_checks(dd, st, stats, orders, bars, prices_hist=prices, securities=uni, events=events, cfg=cfg)
            return CK.eod_checks(dd, st, mon, sessions, orders)
        r = E.run_day(d, prices, uni, state, cfg, adj=adj, close=close, region=region, events=events, fault_mix=mix, seed=seed, checks_fn=checks_fn)
        if r is None:
            continue
        # prime broker file from the drop copy, reconciliation, scoring
        dc = pd.DataFrame(r.dropcopy_rows)
        pb, seeded, late_df = R.simulate_pb_file(d, dc, uni, rng, late_from_prev=late_prev) if len(dc) else (pd.DataFrame(), [], pd.DataFrame())
        write_day(con, r, state, seeded, pd.DataFrame(), pb, r.checks, {})
        explained = {}
        for b, fl in r.faults.items():
            for f in fl:
                if f.params.get("exec_id"):
                    explained[f.params["exec_id"]] = f"fault:{f.type}"
        for eid, label in r.rejected_reports.items():
            explained.setdefault(eid, label)
        breaks = R.run_recon(con, d, seeded, prev_missing, explained) if len(dc) else pd.DataFrame(columns=["date", "check_name", "break_type", "symbol", "key", "detail", "seeded"])
        if len(breaks):
            con.execute("delete from recon_breaks where date = ?", [d]); con.execute("insert into recon_breaks select * from breaks")
        rscore = R.score_recon(breaks, seeded)
        prev_missing = set(breaks.loc[breaks["break_type"] == "missing_at_pb", "key"]); late_prev = late_df
        dsum = F.summarize(r.detections) if fault else None
        if not fault:
            clean_alerts += r.alerts; n_clean += 1
        all_dets += r.detections
        per_day.append({"date": d.isoformat(), "fault_day": fault, "orders": len(r.orders), "fills": len(r.exec_rows), "dropcopy_fills": len(r.dropcopy_rows), "alerts": len(r.alerts), "incidents": len({(a["rule"], a["cl_ord_id"] or a["broker"]) for a in r.alerts}),
                        "faults_injected": dsum["injected"] if dsum else 0, "faults_detected": dsum["detected"] if dsum else 0, "recon": rscore, "netting": {k: (round(v) if isinstance(v, float) else v) for k, v in r.netting.items()}, "ca_applied": len(r.ca_applied),
                        "checks_warn": sum(c["status"] == "warn" for c in r.checks), "checks_fail": sum(c["status"] == "fail" for c in r.checks), "sessions": {b: {k: v for k, v in s.items() if k in ("sent", "received", "resent", "gap_fills")} | {"dropped": s["link"]["dropped"]} for b, s in r.session_stats.items()}, "broker": r.broker_stats})
        if verbose:
            print(f"[{d}] {'fault' if fault else 'clean'} day: {len(r.orders)} orders, {len(r.exec_rows)} fills, {len(r.alerts)} alerts, faults {per_day[-1]['faults_detected']}/{per_day[-1]['faults_injected']}, recon seeded {rscore['found']}/{rscore['seeded']} found, {rscore['unexplained']} unexplained, {time.time() - t0:.0f}s", flush=True)
    # ---- summaries -------------------------------------------------------------------------------------------------------
    fault_days_n = sum(p["fault_day"] for p in per_day)
    monitor = F.summarize([x for x in all_dets], clean_alerts, n_clean)
    monitor["fault_days"] = fault_days_n; monitor["clean_days"] = n_clean
    monitor["clean_incidents_per_day"] = (len({(a["rule"], a["cl_ord_id"] or a["broker"]) for a in clean_alerts}) / n_clean) if n_clean else None
    tca, tca_summary, wheel, pre = tca_from_store(con, start, end, list(cfg.brokers))
    recon_all = con.execute("select break_type, seeded, count(*) n from recon_breaks where date between ? and ? group by 1, 2 order by 1, 2", [start, end]).df()
    recon = {"days": len(per_day), "seeded": sum(p["recon"]["seeded"] for p in per_day), "found": sum(p["recon"]["found"] for p in per_day), "unexplained": sum(p["recon"]["unexplained"] for p in per_day),
             "by_type": {}, "breaks_by_type_and_label": recon_all.to_dict("records"), "fills_reconciled": int(con.execute("select count(*) from executions where source = 'internal' and date between ? and ?", [start, end]).fetchone()[0])}
    for p in per_day:
        for k, v in p["recon"]["by_type"].items():
            e = recon["by_type"].setdefault(k, {"seeded": 0, "found": 0}); e["seeded"] += v["seeded"]; e["found"] += v["found"]
    checks = con.execute("select phase, check_name, status, count(*) n from checks where date between ? and ? group by 1, 2, 3 order by 1, 2, 3", [start, end]).df().to_dict("records")
    out = {"from": start.isoformat(), "to": end.isoformat(), "days": per_day, "monitor": monitor, "recon": recon, "tca": tca_summary, "wheel": wheel, "pretrade": pre, "checks": checks, "config": {"aum": cfg.aum, "brokers": list(cfg.brokers), "max_pct_adv": cfg.max_pct_adv, "seed": seed}, "runtime_s": time.time() - t0}
    to_json(out, os.path.join(RESULTS, "run.json")); to_json(monitor, os.path.join(RESULTS, "monitor.json")); to_json(recon, os.path.join(RESULTS, "recon.json")); to_json({"summary": tca_summary, "wheel": wheel, "pretrade": pre}, os.path.join(RESULTS, "tca.json"))
    tca.to_csv(os.path.join(RESULTS, "tca_orders.csv"), index=False)
    con.close()
    return out
