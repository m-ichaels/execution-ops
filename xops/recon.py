"""End-of-day reconciliation in SQL: internal executions against the broker drop copy, both against the prime broker's
trade file, and start-of-day positions plus fills against end-of-day positions.  The prime-broker file is simulated
from the drop copy with seeded breaks of every type the taxonomy knows, so the reconciliation can be scored: every
seeded break must be found and classified, and a clean day must reconcile with no unexplained break."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from . import calendar as cal

BREAK_TYPES = ["missing_at_pb", "extra_at_pb", "price_diff", "qty_diff", "side_diff", "symbol_diff", "late_booking", "duplicate_at_pb", "missing_in_dropcopy", "missing_internal", "position_break", "ca_not_applied"]
DEFAULT_SEED_MIX = {"missing_at_pb": 2, "extra_at_pb": 1, "price_diff": 2, "qty_diff": 1, "side_diff": 1, "symbol_diff": 1, "late_booking": 2, "duplicate_at_pb": 1}
VENDOR_SYMBOL = {"BRK-B": "BRK.B", "META": "FB"}   # the prime broker's own codes for a couple of names


def settle_date(exchange: str, d: dt.date) -> dt.date:
    return cal.next_trading_day(exchange, d, 1 if exchange in ("XNYS", "XNAS") else 2)


def simulate_pb_file(d: dt.date, dropcopy: pd.DataFrame, securities: pd.DataFrame, rng: np.random.Generator, mix: dict | None = None, late_from_prev: pd.DataFrame | None = None) -> tuple[pd.DataFrame, list[dict], pd.DataFrame]:
    """The prime broker's trade file for day d: the drop-copy fills (what the brokers really did), with seeded breaks.
    Returns the file and the list of seeded breaks (type, exec_id / symbol)."""
    mix = dict(DEFAULT_SEED_MIX if mix is None else mix); sec = securities.set_index("symbol") if "symbol" in securities.columns else securities
    cols = ["trade_id", "exec_id", "symbol", "side", "qty", "px", "trade_date", "settle_date", "broker"]
    base = dropcopy.drop_duplicates("exec_id")
    if len(base) == 0:
        return pd.DataFrame(columns=cols), [], pd.DataFrame(columns=cols)
    settle = {ex: settle_date(ex, d) for ex in ("XNYS", "XLON", "XETR", "XPAR", "XAMS")}
    rows = [{"trade_id": "PB" + d.strftime("%Y%m%d") + f"-{i:06d}", "exec_id": r.exec_id, "symbol": r.symbol, "side": r.side, "qty": float(r.qty), "px": round(float(r.px), 4), "trade_date": d, "settle_date": settle.get(sec.loc[r.symbol, "exchange"] if r.symbol in sec.index else "XNYS", settle["XNYS"]), "broker": r.broker} for i, r in enumerate(base.itertuples())]
    seeded = []; order = list(rng.permutation(len(rows))); k = 0

    def take():
        nonlocal k
        i = order[k % len(order)]; k += 1; return rows[i]

    removed = set(); extra = []; late = []
    for _ in range(mix.get("missing_at_pb", 0)):
        r = take(); removed.add(r["exec_id"]); seeded.append({"break_type": "missing_at_pb", "key": r["exec_id"], "symbol": r["symbol"]})
    for _ in range(mix.get("price_diff", 0)):
        r = take(); r["px"] = round(r["px"] * 1.002, 4); seeded.append({"break_type": "price_diff", "key": r["exec_id"], "symbol": r["symbol"]})
    for _ in range(mix.get("qty_diff", 0)):
        r = take(); r["qty"] += 100; seeded.append({"break_type": "qty_diff", "key": r["exec_id"], "symbol": r["symbol"]})
    for _ in range(mix.get("side_diff", 0)):
        r = take(); r["side"] = "2" if r["side"] == "1" else "1"; seeded.append({"break_type": "side_diff", "key": r["exec_id"], "symbol": r["symbol"]})
    for _ in range(mix.get("symbol_diff", 0)):
        cands = [r for r in rows if r["symbol"] in VENDOR_SYMBOL and r["exec_id"] not in removed]
        if cands:
            r = cands[int(rng.integers(0, len(cands)))]; old = r["symbol"]; r["symbol"] = VENDOR_SYMBOL[old]; seeded.append({"break_type": "symbol_diff", "key": r["exec_id"], "symbol": old})
    for _ in range(mix.get("late_booking", 0)):
        r = take(); removed.add(r["exec_id"]); late.append(dict(r)); seeded.append({"break_type": "late_booking", "key": r["exec_id"], "symbol": r["symbol"]})
    for _ in range(mix.get("extra_at_pb", 0)):
        r = dict(take()); r["exec_id"] += "-X"; r["trade_id"] += "X"; extra.append(r); seeded.append({"break_type": "extra_at_pb", "key": r["exec_id"], "symbol": r["symbol"]})
    for _ in range(mix.get("duplicate_at_pb", 0)):
        r = dict(take()); r["trade_id"] += "D"; extra.append(r); seeded.append({"break_type": "duplicate_at_pb", "key": r["exec_id"], "symbol": r["symbol"]})
    out = [r for r in rows if r["exec_id"] not in removed] + extra
    if late_from_prev is not None and len(late_from_prev):
        for r in late_from_prev.to_dict("records"):
            r = dict(r); r["trade_date"] = d; out.append(r)
    f = pd.DataFrame(out, columns=cols); late_df = pd.DataFrame(late, columns=cols)
    return f, seeded, late_df


RECON_SQL = {
    "missing_in_dropcopy": """select i.symbol, i.exec_id as key, 'internal fill absent from the drop copy' as detail from executions i
        left join executions d on d.exec_id = i.exec_id and d.source = 'dropcopy' and d.date = i.date where i.source = 'internal' and i.date = ? and d.exec_id is null""",
    "missing_internal": """select d.symbol, d.exec_id as key, 'drop-copy fill not booked internally' as detail from (select distinct exec_id, symbol, date from executions where source = 'dropcopy') d
        left join executions i on i.exec_id = d.exec_id and i.source = 'internal' and i.date = d.date where d.date = ? and i.exec_id is null""",
    "missing_at_pb": """select i.symbol, i.exec_id as key, 'internal fill absent from the prime broker file' as detail from executions i
        left join pb_file p on p.exec_id = i.exec_id and p.trade_date = i.date where i.source = 'internal' and i.date = ? and p.exec_id is null""",
    "extra_at_pb": """select p.symbol, p.exec_id as key, 'prime broker trade with no internal fill' as detail from pb_file p
        left join executions i on i.exec_id = p.exec_id and i.source = 'internal' and i.date = p.trade_date
        left join executions d on d.exec_id = p.exec_id and d.source = 'dropcopy' and d.date = p.trade_date where p.trade_date = ? and i.exec_id is null and d.exec_id is null and p.date = p.trade_date""",
    "duplicate_at_pb": """select symbol, exec_id as key, 'exec id appears ' || count(*) || ' times in the prime broker file' as detail from pb_file where trade_date = ? group by symbol, exec_id having count(*) > 1""",
    "price_diff": """select i.symbol, i.exec_id as key, 'price ' || i.px || ' internal vs ' || p.px || ' at the prime broker' as detail from executions i join pb_file p on p.exec_id = i.exec_id and p.trade_date = i.date
        where i.source = 'internal' and i.date = ? and abs(i.px - p.px) > 0.00011 * greatest(i.px, 1)""",
    "qty_diff": """select i.symbol, i.exec_id as key, 'quantity ' || i.qty || ' internal vs ' || p.qty || ' at the prime broker' as detail from executions i join pb_file p on p.exec_id = i.exec_id and p.trade_date = i.date
        where i.source = 'internal' and i.date = ? and abs(i.qty - p.qty) > 0.5""",
    "side_diff": """select i.symbol, i.exec_id as key, 'side ' || i.side || ' internal vs ' || p.side || ' at the prime broker' as detail from executions i join pb_file p on p.exec_id = i.exec_id and p.trade_date = i.date
        where i.source = 'internal' and i.date = ? and i.side <> p.side and not (i.side = '5' and p.side = '2')""",
    "symbol_diff": """select i.symbol, i.exec_id as key, 'symbol ' || i.symbol || ' internal vs ' || p.symbol || ' at the prime broker' as detail from executions i join pb_file p on p.exec_id = i.exec_id and p.trade_date = i.date
        where i.source = 'internal' and i.date = ? and i.symbol <> p.symbol""",
}


def run_recon(con, d: dt.date, seeded: list[dict], prev_missing: set[str] | None = None, explained: dict[str, str] | None = None) -> pd.DataFrame:
    """All checks for day d; each break is labelled with the seeded type it matches, 'late_booking' when a missing-at-PB
    break from the previous day now appears, the fault or rejected report that explains it, or '' when unexplained."""
    rows = []; explained = explained or {}
    seeded_by_key = {(s["break_type"], s["key"]): s for s in seeded}
    for name, sql in RECON_SQL.items():
        for symbol, key, detail in con.execute(sql, [d]).fetchall():
            label = ""
            if (name, key) in seeded_by_key:
                label = name
            elif name == "missing_at_pb" and ("late_booking", key) in seeded_by_key:
                label = "late_booking"
            elif name == "extra_at_pb" and prev_missing and key in prev_missing:
                label = "late_booking_resolved"
            elif key in explained:
                label = explained[key]
            rows.append({"date": d, "check_name": name, "break_type": name, "symbol": symbol, "key": key, "detail": detail, "seeded": label})
    # positions: SOD + signed internal fills = EOD, per symbol at fund level
    pos = con.execute("""with sod as (select symbol, sum(qty) q from positions where date = ? and account = 'FUND' and source = 'SOD' group by symbol),
        eod as (select symbol, sum(qty) q from positions where date = ? and account = 'FUND' and source = 'EOD' group by symbol),
        fills as (select symbol, sum(case when side = '1' then qty else -qty end) q from executions where source = 'internal' and date = ? group by symbol)
        select coalesce(s.symbol, e.symbol, f.symbol) symbol, coalesce(s.q, 0) sod, coalesce(f.q, 0) fills, coalesce(e.q, 0) eod from sod s full outer join eod e on s.symbol = e.symbol full outer join fills f on coalesce(s.symbol, e.symbol) = f.symbol
        where abs(coalesce(s.q, 0) + coalesce(f.q, 0) - coalesce(e.q, 0)) > 0.5""", [d, d, d]).fetchall()
    for symbol, sod, fills, eod in pos:
        rows.append({"date": d, "check_name": "positions", "break_type": "position_break", "symbol": symbol, "key": symbol, "detail": f"SOD {sod:g} + fills {fills:g} != EOD {eod:g}", "seeded": ""})
    return pd.DataFrame(rows, columns=["date", "check_name", "break_type", "symbol", "key", "detail", "seeded"])


def score_recon(breaks: pd.DataFrame, seeded: list[dict]) -> dict:
    found = {(b.break_type, b.key) for b in breaks.itertuples()}
    per_type: dict[str, dict] = {}
    for s in seeded:
        t = per_type.setdefault(s["break_type"], {"seeded": 0, "found": 0}); t["seeded"] += 1
        hit = (s["break_type"], s["key"]) in found or (s["break_type"] == "late_booking" and ("missing_at_pb", s["key"]) in found)
        t["found"] += int(hit)
    unexplained = breaks[breaks["seeded"] == ""]
    return {"by_type": per_type, "seeded": sum(t["seeded"] for t in per_type.values()), "found": sum(t["found"] for t in per_type.values()), "n_breaks": len(breaks), "unexplained": len(unexplained), "unexplained_by_type": unexplained["break_type"].value_counts().to_dict()}
