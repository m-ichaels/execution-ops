#!/usr/bin/env python3
"""Free data for the execution-operations stack.   python tools/download.py [--skip-prices] [--skip-fi]

  Yahoo chart API   20 years of daily bars, dividends and splits for the universe in data/universe.csv (no key)
                    -> data/raw/yahoo/<symbol>.json, data/derived/prices.parquet, data/derived/events.csv
  BLS               CPI-U (CUUR0000SA0), for the TIPS reference-CPI check       -> data/reference/cpi_u.csv
  SIFMA             TBA notification and settlement dates by class                -> data/reference/sifma_tba.csv
The curated corporate actions that Yahoo does not carry (spin-offs, mergers, symbol changes) are in
data/reference/corporate_actions_curated.csv and are checked in.
"""
import csv
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")
DER = os.path.join(ROOT, "data", "derived")
REF = os.path.join(ROOT, "data", "reference")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

# ---- universe --------------------------------------------------------------------------------------------------------
US = ("AAPL MSFT NVDA AMZN GOOGL META BRK-B TSLA AVGO JPM LLY V UNH XOM MA JNJ PG COST HD ABBV WMT BAC NFLX CRM KO CVX MRK AMD "
      "PEP TMO ORCL CSCO ACN ADBE MCD LIN ABT WFC TXN DHR IBM GE CAT PM INTU QCOM VZ AMGN ISRG DIS NOW CMCSA SPGI PFE GS UBER "
      "AMAT UNP RTX BKNG LOW AXP T HON NKE BLK SYK PGR ELV NEE TJX LMT BA MDT COP DE ADP SBUX GILD C PLD MMC BX MDLZ ADI VRTX "
      "CB REGN LRCX SCHW ETN MU KLAC CI CMG ANET PANW MO SO BSX ZTS WM CVS TGT ITW EQIX MS APH BDX EOG CL DUK PNC SLB ICE "
      "PYPL CME NOC FDX SHW EMR AON USB PSA MCK CSX GD APD ORLY MAR AJG WELL ROP HCA NXPI AZO AFL CTAS OKE SRE ECL TRV PCAR "
      "F GM DAL WBD GEV GEHC SOLV KVUE MMM SMCI DECK FAST XRX AMC").split()
UK = "SHEL.L AZN.L HSBA.L ULVR.L BP.L RIO.L GSK.L DGE.L REL.L BATS.L LSEG.L NG.L BARC.L LLOY.L VOD.L GLEN.L AAL.L PRU.L TSCO.L BA.L".split()
EU = "SAP.DE SIE.DE ALV.DE DTE.DE BAS.DE BAYN.DE BMW.DE MC.PA OR.PA TTE.PA SAN.PA AIR.PA BNP.PA ASML.AS".split()


def universe():
    rows = []
    for s in US:
        rows.append({"symbol": s, "exchange": "XNYS", "currency": "USD", "region": "US", "price_scale": 1})
    for s in UK:
        rows.append({"symbol": s, "exchange": "XLON", "currency": "GBp", "region": "EU", "price_scale": 0.01})
    for s in EU:
        ex = {"DE": "XETR", "PA": "XPAR", "AS": "XAMS"}[s.split(".")[-1]]
        rows.append({"symbol": s, "exchange": ex, "currency": "EUR", "region": "EU", "price_scale": 1})
    return rows


def get(url, retries=3, timeout=60):
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json,text/html"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            if k == retries - 1:
                print("  failed", url[:80], e)
                return None
            time.sleep(2 * (k + 1))


def yahoo(sym):
    raw = get(f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}?range=20y&interval=1d&events=div,splits", retries=2, timeout=30)
    if raw is None:
        return None
    try:
        r = json.loads(raw)["chart"]["result"][0]
    except Exception:  # noqa: BLE001
        return None
    return r


def prices():
    os.makedirs(os.path.join(RAW, "yahoo"), exist_ok=True); os.makedirs(DER, exist_ok=True)
    uni = universe()
    with open(os.path.join(ROOT, "data", "universe.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(uni[0])); w.writeheader(); w.writerows(uni)
    bars, events = [], []
    for u in uni:
        sym = u["symbol"]; p = os.path.join(RAW, "yahoo", f"{sym}.json")
        if os.path.exists(p):
            r = json.load(open(p))
        else:
            r = yahoo(sym)
            if r is None:
                print("  no data", sym); continue
            json.dump(r, open(p, "w")); time.sleep(0.25)
        ts = r.get("timestamp") or []; q = r["indicators"]["quote"][0]; adj = r["indicators"].get("adjclose", [{}])[0].get("adjclose", [None] * len(ts))
        n = 0
        for i, t in enumerate(ts):
            c = q["close"][i]
            if c is None:
                continue
            d = dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat()
            bars.append((sym, d, q["open"][i] if q["open"][i] is not None else c, q["high"][i] if q["high"][i] is not None else c, q["low"][i] if q["low"][i] is not None else c, c, adj[i] if adj[i] is not None else c, q["volume"][i] or 0)); n += 1
        ev = r.get("events", {})
        for t, e in (ev.get("dividends") or {}).items():
            events.append((sym, dt.datetime.fromtimestamp(int(e.get("date", t)), dt.timezone.utc).date().isoformat(), "dividend", e["amount"], "", ""))
        for t, e in (ev.get("splits") or {}).items():
            events.append((sym, dt.datetime.fromtimestamp(int(e.get("date", t)), dt.timezone.utc).date().isoformat(), "split", e["numerator"] / e["denominator"], e.get("splitRatio", ""), ""))
        print(f"  {sym}: {n} bars, {len(ev.get('dividends') or {})} dividends, {len(ev.get('splits') or {})} splits")
    import pyarrow as pa
    import pyarrow.parquet as pq
    cols = list(zip(*bars))
    table = pa.table({"symbol": cols[0], "date": cols[1], "open": pa.array(cols[2], pa.float64()), "high": pa.array(cols[3], pa.float64()), "low": pa.array(cols[4], pa.float64()), "close": pa.array(cols[5], pa.float64()), "adjclose": pa.array(cols[6], pa.float64()), "volume": pa.array(cols[7], pa.float64())})
    pq.write_table(table, os.path.join(DER, "prices.parquet"), compression="zstd")
    events.sort(key=lambda e: (e[0], e[1]))
    with open(os.path.join(DER, "events_yahoo.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["symbol", "ex_date", "type", "value", "ratio_text", "note"]); w.writerows(events)
    print(f"prices: {len(bars)} bars, {len(events)} events -> {DER}")


def cpi():
    os.makedirs(REF, exist_ok=True)
    rows = []
    for y0 in (2005, 2015, 2025):
        raw = get(f"https://api.bls.gov/publicAPI/v1/timeseries/data/CUUR0000SA0?startyear={y0}&endyear={y0 + 9}")
        if raw is None:
            continue
        for s in json.loads(raw)["Results"]["series"]:
            for d in s["data"]:
                if d["period"].startswith("M") and d["value"] not in ("-", ""):
                    rows.append((int(d["year"]), int(d["period"][1:]), float(d["value"])))
    rows = sorted(set(rows))
    with open(os.path.join(REF, "cpi_u.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["year", "month", "cpi_u_nsa"]); w.writerows(rows)
    print(f"cpi: {len(rows)} months")


def sifma():
    os.makedirs(REF, exist_ok=True)
    raw = get("https://www.sifma.org/resources/general/mbs-notification-and-settlement-dates/")
    if raw is None:
        return
    t = raw.decode("utf-8", errors="ignore").encode().decode("unicode_escape", errors="ignore")
    rows = []
    for tb in re.findall(r"<table.*?</table>", t, flags=re.S):
        for r in re.findall(r"<tr.*?</tr>", tb, flags=re.S):
            cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, flags=re.S)]
            if len(cells) == 6 and cells[1] in ("Notification", "Settlement"):
                try:
                    m = dt.datetime.strptime(cells[0], "%b-%y")
                except ValueError:
                    continue
                for k, cls in enumerate("ABCD"):
                    d = dt.datetime.strptime(cells[2 + k], "%m/%d/%Y").date().isoformat()
                    rows.append((m.strftime("%Y-%m"), cells[1].lower(), cls, d))
    rows = sorted(set(rows))
    with open(os.path.join(REF, "sifma_tba.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["settlement_month", "date_type", "class", "date"]); w.writerows(rows)
    print(f"sifma: {len(rows)} rows")


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--skip-prices" not in a:
        prices()
    if "--skip-fi" not in a:
        cpi(); sifma()
    print("done")
