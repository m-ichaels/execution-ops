"""xops command line.

  python -m xops build-store                    load prices, events and reference tables into data/derived/xops.duckdb
  python -m xops calendar-check                 exchange calendars against the trading days in the data -> results/calendar_check.json
  python -m xops corpact-check                  corporate-actions engine against Yahoo's adjusted closes  -> results/corpact.json
  python -m xops fi-check                       futures / TBA / CDX / option / TIPS calendars               -> results/fi_check.json
  python -m xops run --from D0 --to D1 [--faults alternate|all|none] [--seed N]      the day pipeline -> results/run.json ...
  python -m xops sod --date D                   start-of-day checks for a date (no trading)
  python -m xops recon --date D                 re-run the reconciliation for a date already in the store
  python -m xops tca                            recompute the transaction-cost analysis, the broker wheel and the pre-trade model from the store
  python -m xops serve --port 9880 --date D     FIX 4.4 acceptor (simulated broker) on TCP, wall clock
  python -m xops selftest --port 9881           the session layer over real sockets: logon, orders, gap and resend, logout
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

from . import data


def parse_date(s: str) -> dt.date:
    return dt.date.today() if s == "today" else dt.date.fromisoformat(s)


def cmd_build_store(a):
    con = data.connect(fresh=True); print(data.build_store(con)); con.close()


def cmd_calendar_check(a):
    from . import checks as CK
    from .run import to_json
    px = data.load_prices(start="2007-01-01"); uni = data.load_universe()
    out = CK.calendar_vs_data(px, uni)
    for ex, v in out.items():
        print(f"{ex}: {v['symbols']} symbols, {v['calendar_days']} calendar days vs {v['data_days']} data days {v['from']}..{v['to']}; open-but-no-data {v['calendar_open_but_no_data']}; data-on-closed {v['data_on_calendar_closed']}")
    to_json(out, os.path.join(data.ROOT, "results", "calendar_check.json"))


def cmd_corpact_check(a):
    from . import corpact
    from .run import to_json
    px = data.load_prices(); ev = data.load_events(); uni = data.load_universe(); ex_of = uni.set_index("symbol")["exchange"].to_dict()
    raw = corpact.validate_against_yahoo(px, ev, exchange_of=ex_of)
    fixed = corpact.validate_against_yahoo(px, ev, exchange_of=ex_of, div_scale={"XLON": 0.01})
    counts = ev["type"].value_counts().to_dict()
    out = {"events_in_feed": counts, "dividends_checked": raw.n_events, "as_reported": {"within_tol": raw.n_matched, "median_rel_err": raw.median_rel_err, "max_rel_err": raw.max_rel_err, "by_exchange": raw.by_type, "unexplained_adjustments": raw.n_unexplained, "worst": raw.worst[:8]},
           "lse_dividends_in_pounds": {"within_tol": fixed.n_matched, "median_rel_err": fixed.median_rel_err, "max_rel_err": fixed.max_rel_err, "by_exchange": fixed.by_type, "unexplained_adjustments": fixed.n_unexplained},
           "note": "Yahoo's close series is already split-adjusted, so dividends are the only events its adjusted close can check; LSE prices are in pence while Yahoo adjusts with the dividend in pounds, a factor-100 vendor inconsistency that the check finds"}
    print(json.dumps({k: v for k, v in out.items() if k != "as_reported"}, indent=1, default=str)[:1500])
    to_json(out, os.path.join(data.ROOT, "results", "corpact.json"))


def cmd_fi_check(a):
    from . import fi
    from .run import to_json
    out = {"futures": {}, "options": {}, "cdx": {}, "tba": {}, "tips": {}}
    for code in ("ZNZ25", "ZNH26", "ZNM26", "ZNU26", "ZNZ26", "ZTZ25", "ZBZ25", "SR3Z25", "SR3H26", "ESZ25", "ESH26", "ESU26", "6EZ25", "GCZ25", "CLX26", "CLZ26", "NGX26"):
        root, y, m = fi.parse_contract(code); out["futures"][code] = {k: (v.isoformat() if v else None) for k, v in fi.futures_dates(root, y, m).items()}
    for y in (2024, 2025, 2026):
        out["options"][y] = [fi.option_expiry(y, m).isoformat() for m in range(1, 13)]; out["cdx"][y] = [d.isoformat() for d in fi.cdx_roll_dates(y)]
    t = fi.tba_calendar()
    out["tba"] = {f"{m} {c}": {k: v.isoformat() for k, v in fi.tba_dates(m, c, t).items()} for m in sorted(t["settlement_month"].unique())[:12] for c in "ABCD"}
    cpi = fi.cpi_table(); d = dt.date.today()
    out["tips"] = {"months_of_cpi": len(cpi), "reference_cpi_today": fi.reference_cpi(d, cpi), "reference_cpi_first_of_month_equals_cpi_three_months_back": abs(fi.reference_cpi(d.replace(day=1), cpi) - cpi[(d.year if d.month > 3 else d.year - 1, (d.month - 3 - 1) % 12 + 1)]) < 1e-9}
    out["front_today"] = {r: fi.front_contract(r, d, quarterly=r != "CL") for r in ("ZN", "ES", "SR3", "CL")}
    print(json.dumps(out["futures"], indent=1)); print(out["front_today"], out["tips"])
    to_json(out, os.path.join(data.ROOT, "results", "fi_check.json"))


def cmd_run(a):
    from .run import run_range
    out = run_range(parse_date(a.frm), parse_date(a.to), fault_days=a.faults, seed=a.seed)
    print(json.dumps({k: out[k] for k in ("monitor", "recon") if k in out}, indent=1, default=str)[:3000])


def cmd_sod(a):
    from . import checks as CK, engine as E, portfolio as P, strategies as S
    d = parse_date(a.date); px = data.load_prices(start=(d - dt.timedelta(days=420)).isoformat(), end=d.isoformat()); uni = data.load_universe(); ev = data.load_events()
    adj = S.panel(px, "adjclose"); close = S.panel(px, "close"); region = uni.set_index("symbol")["region"]; cfg = P.Config(); state = E.FundState()
    from . import calendar as cal
    E.run_day(cal.next_trading_day("XNYS", d, -1), px, uni, state, cfg, adj=adj, close=close, region=region, events=ev, warm=True)
    stats = P.symbol_stats(px, d); targets = S.targets_for(adj, close, region, d, cfg.aum, state.prev_targets)
    orders, carry, rep = P.build_orders(d, targets, state.positions, stats, uni, cfg, strategy_specs=S.STRATEGIES)
    for c in CK.sod_checks(d, state, stats, orders, px[px["date"] == d.isoformat()], prices_hist=px, securities=uni, events=ev, cfg=cfg):
        print(f"[{c['status']:4s}] {c['check_name']}: {c['detail']}")
    print(f"{len(orders)} orders would go out; netting: {rep}")


def cmd_tca(a):
    from .run import tca_from_store, to_json, RESULTS
    con = data.connect(); frm = con.execute("select min(date), max(date) from orders").fetchone()
    tca, summary, wheel, pre = tca_from_store(con, frm[0], frm[1], ["BRK1", "BRK2", "BRK3"]); con.close()
    to_json({"summary": summary, "wheel": wheel, "pretrade": pre}, os.path.join(RESULTS, "tca.json")); tca.to_csv(os.path.join(RESULTS, "tca_orders.csv"), index=False)
    print(json.dumps({"summary": {k: v for k, v in summary.items() if not isinstance(v, dict)}, "wheel": {b: {k: v for k, v in x.items() if k != "by_algo_bp"} for b, x in wheel.get("brokers", {}).items()}, "power": wheel.get("power"), "pretrade": pre}, indent=1, default=str))


def cmd_recon(a):
    from . import recon as R
    con = data.connect(); d = parse_date(a.date)
    br = R.run_recon(con, d, [])
    print(br.groupby(["break_type", "seeded"]).size().to_string() if len(br) else "no breaks"); con.close()


def cmd_serve(a):
    from .serve import serve
    serve(a.port, parse_date(a.date) if a.date else None)


def cmd_selftest(a):
    from .serve import selftest
    ok = selftest(a.port); print("FIX TCP SELF-TEST", "PASSED" if ok else "FAILED"); sys.exit(0 if ok else 1)


def main(argv=None):
    p = argparse.ArgumentParser(prog="xops"); sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build-store").set_defaults(fn=cmd_build_store)
    sub.add_parser("calendar-check").set_defaults(fn=cmd_calendar_check)
    sub.add_parser("corpact-check").set_defaults(fn=cmd_corpact_check)
    sub.add_parser("fi-check").set_defaults(fn=cmd_fi_check)
    r = sub.add_parser("run"); r.add_argument("--from", dest="frm", required=True); r.add_argument("--to", required=True); r.add_argument("--faults", default="alternate"); r.add_argument("--seed", type=int, default=1); r.set_defaults(fn=cmd_run)
    s = sub.add_parser("sod"); s.add_argument("--date", required=True); s.set_defaults(fn=cmd_sod)
    rc = sub.add_parser("recon"); rc.add_argument("--date", required=True); rc.set_defaults(fn=cmd_recon)
    sub.add_parser("tca").set_defaults(fn=cmd_tca)
    sv = sub.add_parser("serve"); sv.add_argument("--port", type=int, default=9880); sv.add_argument("--date", default=None); sv.set_defaults(fn=cmd_serve)
    st = sub.add_parser("selftest"); st.add_argument("--port", type=int, default=9881); st.set_defaults(fn=cmd_selftest)
    a = p.parse_args(argv); a.fn(a)


if __name__ == "__main__":
    main()
