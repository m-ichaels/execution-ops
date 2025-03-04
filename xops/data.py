"""Loaders: universe, prices (parquet), Yahoo events, curated corporate actions, reference tables; and the DuckDB store
that everything downstream reads from and writes to."""
from __future__ import annotations

import csv
import datetime as dt
import os

import duckdb
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DER = os.path.join(ROOT, "data", "derived")
REF = os.path.join(ROOT, "data", "reference")
DB_PATH = os.path.join(DER, "xops.duckdb")


def sql_path(p: str) -> str:
    """a file path as a SQL string literal body: forward slashes, single quotes doubled (the workspace path has one)"""
    return p.replace(os.sep, "/").replace("'", "''")


def read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_universe() -> pd.DataFrame:
    u = pd.read_csv(os.path.join(ROOT, "data", "universe.csv"))
    u["lot_size"] = 1
    return u


def load_prices(symbols: list[str] | None = None, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    con = duckdb.connect()
    q = f"select * from read_parquet('{sql_path(os.path.join(DER, 'prices.parquet'))}')"
    conds = []
    if symbols:
        conds.append("symbol in (" + ",".join(f"'{s}'" for s in symbols) + ")")
    if start:
        conds.append(f"date >= '{start}'")
    if end:
        conds.append(f"date <= '{end}'")
    if conds:
        q += " where " + " and ".join(conds)
    df = con.execute(q + " order by symbol, date").df(); con.close()
    return df


def load_events() -> pd.DataFrame:
    """Yahoo dividends and splits plus the curated spin-offs, mergers and symbol changes, one row per event."""
    y = pd.read_csv(os.path.join(DER, "events_yahoo.csv"), dtype={"ratio_text": str, "note": str})
    y = y.rename(columns={"value": "amount"}); y["ratio"] = np.where(y["type"] == "split", y["amount"], np.nan); y.loc[y["type"] == "split", "amount"] = np.nan
    y["new_symbol"] = ""; y["source"] = "yahoo"
    c = pd.read_csv(os.path.join(REF, "corporate_actions_curated.csv"), dtype={"new_symbol": str, "note": str}).fillna({"new_symbol": "", "note": ""})
    c["source"] = "curated"
    cols = ["symbol", "ex_date", "type", "amount", "ratio", "new_symbol", "source", "note"]
    for d in (y, c):
        for col in cols:
            if col not in d:
                d[col] = np.nan if col in ("amount", "ratio") else ""
    ev = pd.concat([y[cols], c[cols]], ignore_index=True).sort_values(["symbol", "ex_date", "type"]).reset_index(drop=True)
    ev["id"] = range(1, len(ev) + 1)
    return ev


def load_reference(name: str) -> pd.DataFrame:
    return pd.read_csv(os.path.join(REF, f"{name}.csv"))


SCHEMA = """
create table if not exists securities (symbol varchar primary key, exchange varchar, currency varchar, region varchar, price_scale double, lot_size integer);
create table if not exists prices (symbol varchar, date date, open double, high double, low double, close double, adjclose double, volume double);
create table if not exists corporate_actions (id integer, symbol varchar, ex_date date, type varchar, amount double, ratio double, new_symbol varchar, source varchar, note varchar);
create table if not exists symbol_history (symbol varchar, old_symbol varchar, valid_from date);
create table if not exists targets (date date, strategy varchar, symbol varchar, target_shares double, target_notional double);
create table if not exists positions (date date, account varchar, symbol varchar, qty double, source varchar);
create table if not exists orders (date date, cl_ord_id varchar, order_id varchar, strategy varchar, symbol varchar, exchange varchar, side varchar, qty double, algo varchar, broker varchar, urgency varchar, max_pct double,
                                   decision_px double, arrival_px double, arrival_time double, start_time double, end_time double, status varchar, cum_qty double, avg_px double, adv double, spread_bp double, vol_day double, difficulty integer, n_fills integer, ack_latency_ms double, vwap_px double, close_px double);
create table if not exists executions (date date, source varchar, exec_id varchar, cl_ord_id varchar, order_id varchar, symbol varchar, side varchar, qty double, px double, time double, broker varchar, liquidity varchar, seq integer);
create table if not exists pb_file (date date, trade_id varchar, exec_id varchar, symbol varchar, side varchar, qty double, px double, trade_date date, settle_date date, broker varchar);
create table if not exists alerts (date date, time double, rule varchar, severity varchar, cl_ord_id varchar, symbol varchar, detail varchar);
create table if not exists faults (date date, time double, type varchar, cl_ord_id varchar, symbol varchar, detected_time double, detected_rule varchar);
create table if not exists recon_breaks (date date, check_name varchar, break_type varchar, symbol varchar, key varchar, detail varchar, seeded varchar);
create table if not exists checks (date date, phase varchar, check_name varchar, status varchar, detail varchar);
create table if not exists market_minutes (date date, symbol varchar, minute integer, mid double, volume double);
"""


def connect(path: str = DB_PATH, fresh: bool = False) -> duckdb.DuckDBPyConnection:
    if fresh and os.path.exists(path):
        os.remove(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = duckdb.connect(path)
    for stmt in SCHEMA.strip().split(";"):
        if stmt.strip():
            con.execute(stmt)
    return con


def build_store(con: duckdb.DuckDBPyConnection):
    """(Re)load the reference tables from the files: securities, prices, corporate actions, symbol history."""
    u = load_universe()
    con.execute("delete from securities"); con.execute("insert into securities select symbol, exchange, currency, region, price_scale, lot_size from u")
    con.execute("delete from prices"); con.execute(f"insert into prices select symbol, cast(date as date), open, high, low, close, adjclose, volume from read_parquet('{sql_path(os.path.join(DER, 'prices.parquet'))}')")
    ev = load_events()
    con.execute("delete from corporate_actions"); con.execute("insert into corporate_actions select id, symbol, cast(ex_date as date), type, amount, ratio, new_symbol, source, note from ev")
    con.execute("delete from symbol_history")
    sh = ev[ev["type"] == "symbol_change"][["new_symbol", "symbol", "ex_date"]].rename(columns={"new_symbol": "symbol", "symbol": "old_symbol", "ex_date": "valid_from"})
    if len(sh):
        con.execute("insert into symbol_history select symbol, old_symbol, cast(valid_from as date) from sh")
    return {"securities": len(u), "prices": con.execute("select count(*) from prices").fetchone()[0], "corporate_actions": len(ev)}


def trading_dates(con, exchange: str = "XNYS", start: str | None = None, end: str | None = None) -> list[dt.date]:
    q = "select distinct p.date from prices p join securities s using (symbol) where s.exchange = ?"
    args = [exchange]
    if start:
        q += " and p.date >= ?"; args.append(start)
    if end:
        q += " and p.date <= ?"; args.append(end)
    return [r[0] for r in con.execute(q + " order by 1", args).fetchall()]
