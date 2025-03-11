#!/usr/bin/env python3
"""Figures from results/*.json, results/tca_orders.csv and the DuckDB store.   python scripts/plots.py [results] [results/figures]"""
import json
import os
import sys

import duckdb
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
R = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "results")
F = sys.argv[2] if len(sys.argv) > 2 else os.path.join(R, "figures")
DB = os.path.join(ROOT, "data", "derived", "xops.duckdb")
os.makedirs(F, exist_ok=True)
plt.rcParams.update({"font.size": 8, "axes.titlesize": 9, "axes.labelsize": 8, "legend.fontsize": 7, "figure.dpi": 130})
BCOL = {"BRK1": "C2", "BRK2": "C0", "BRK3": "C3"}


def load(name):
    p = os.path.join(R, name)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


def q(sql, *args):
    con = duckdb.connect(DB, read_only=True)
    try:
        return con.execute(sql, list(args)).df()
    finally:
        con.close()


def save(fig, name):
    fig.tight_layout(); fig.savefig(os.path.join(F, name)); plt.close(fig); print("wrote", os.path.join(F, name))


def fig_monitor():
    m = load("monitor.json"); run = load("run.json")
    if not m or not run:
        return
    fig, axes = plt.subplots(2, 2, figsize=(12, 6.8))
    ax = axes[0, 0]; types = [t for t in m["by_type"] if t != "dropcopy_missing"]; inj = [m["by_type"][t]["injected"] for t in types]; det = [m["by_type"][t]["detected"] for t in types]
    x = np.arange(len(types)); ax.bar(x, inj, color="lightgrey", label="injected"); ax.bar(x, det, color="C2", label="detected by the monitor")
    for i, t in enumerate(types):
        ttd = m["by_type"][t]["median_ttd_s"]
        ax.text(i, inj[i] + 0.5, f"{det[i]}/{inj[i]}" + (f"\n{ttd:.0f}s" if ttd is not None else ""), ha="center", fontsize=6.5)
    ax.set_xticks(x); ax.set_xticklabels(types, rotation=45, ha="right", fontsize=7); ax.set_ylabel("faults over the fault days"); ax.set_title(f"fault injection: {m.get('monitor_detected', m['detected'])} of {m.get('monitor_injected', m['injected'])} monitor-level faults caught ({100 * m.get('monitor_detection_rate', m['detection_rate']):.0f} %), median time to detect {m['median_ttd_s']:.1f} s"); ax.legend()
    ax = axes[0, 1]; f = q("select type, detected_time - time as ttd from faults where detected_time is not null")
    if len(f):
        order = [t for t in types if t in set(f["type"])]
        ax.boxplot([f.loc[f["type"] == t, "ttd"].clip(lower=0.05).values for t in order], tick_labels=order, vert=True, showfliers=True, widths=0.6)
        ax.set_yscale("log"); ax.set_ylabel("time to detect (s, log)"); ax.set_title("time to detect by fault type"); ax.tick_params(axis="x", rotation=45); ax.axhline(60, color="k", lw=0.5, ls="--"); ax.text(0.6, 65, "one minute", fontsize=6.5)
    ax = axes[1, 0]; days = run["days"]; x = np.arange(len(days))
    ax.bar(x, [d["incidents"] for d in days], color=["C3" if d["fault_day"] else "C2" for d in days])
    ax.set_xticks(x[::5]); ax.set_xticklabels([d["date"][5:] for d in days][::5], rotation=45, fontsize=6.5); ax.set_ylabel("alert incidents (rule x order or broker)")
    ax.set_title(f"incidents per day: fault days red (mean {np.mean([d['incidents'] for d in days if d['fault_day']]):.0f}), clean days green (mean {m['clean_incidents_per_day']:.1f})")
    ax = axes[1, 1]; rules = m.get("false_alerts_by_rule", {})
    if rules:
        ax.bar(range(len(rules)), list(rules.values()), color="C1"); ax.set_xticks(range(len(rules))); ax.set_xticklabels(list(rules), rotation=30, fontsize=7)
    ax.set_title(f"alerts on clean days: {m['false_alerts_per_clean_day']:.1f} per day over {m['clean_days']} days"); ax.set_ylabel("alerts")
    save(fig, "monitor.png")


def fig_recon():
    rc = load("recon.json"); run = load("run.json")
    if not rc or not run:
        return
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.8))
    ax = axes[0]; types = list(rc["by_type"]); x = np.arange(len(types))
    ax.bar(x, [rc["by_type"][t]["seeded"] for t in types], color="lightgrey", label="seeded"); ax.bar(x, [rc["by_type"][t]["found"] for t in types], color="C0", label="found and classified")
    ax.set_xticks(x); ax.set_xticklabels(types, rotation=45, ha="right", fontsize=7); ax.set_title(f"seeded breaks: {rc['found']} of {rc['seeded']} found, {rc['unexplained']} unexplained"); ax.legend()
    ax = axes[1]; lab = pd.DataFrame(rc["breaks_by_type_and_label"])
    if len(lab):
        lab["group"] = np.where(lab["seeded"] == "", "unexplained", np.where(lab["seeded"].str.startswith("fault:") | lab["seeded"].str.startswith("rejected") | lab["seeded"].str.startswith("resync"), "explained by a fault", np.where(lab["seeded"].str.startswith("late"), "late booking", "seeded")))
        piv = lab.groupby(["break_type", "group"])["n"].sum().unstack(fill_value=0)
        piv.plot(kind="bar", stacked=True, ax=ax, color={"seeded": "C0", "late booking": "C1", "explained by a fault": "C8", "unexplained": "C3"}); ax.set_ylabel("breaks over the run"); ax.set_title("every break by type and how it was explained"); ax.tick_params(axis="x", rotation=45); ax.legend(fontsize=6.5)
    ax = axes[2]; days = run["days"]; x = np.arange(len(days))
    ax.plot(x, [d["fills"] for d in days], "-", color="C0", label="internal fills"); ax.plot(x, [d["dropcopy_fills"] for d in days], "--", color="C2", label="drop-copy fills")
    ax.set_xticks(x[::5]); ax.set_xticklabels([d["date"][5:] for d in days][::5], rotation=45, fontsize=6.5); ax.set_ylabel("fills per day"); ax.set_title(f"{rc['fills_reconciled']:,} fills reconciled three ways"); ax.legend()
    save(fig, "recon.png")


def fig_corpact():
    c = load("corpact.json"); cal = load("calendar_check.json")
    if not c:
        return
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.8))
    ax = axes[0]; ex = c["as_reported"]["by_exchange"]; fx = c["lse_dividends_in_pounds"]["by_exchange"]; names = sorted(ex); x = np.arange(len(names)); w = 0.38
    ax.bar(x - w / 2, [ex[n]["median"] for n in names], width=w, color="C3", label="dividend amount as reported"); ax.bar(x + w / 2, [fx[n]["median"] for n in names], width=w, color="C2", label="LSE dividends read in pounds")
    ax.set_yscale("log"); ax.set_xticks(x); ax.set_xticklabels([f"{n}\n(n={ex[n]['n']})" for n in names]); ax.set_ylabel("median |factor error| (log)"); ax.set_title("engine vs Yahoo adjusted close, dividend factors"); ax.legend(fontsize=6.5)
    ax = axes[1]; ev = c["events_in_feed"]; ax.bar(range(len(ev)), list(ev.values()), color="C0"); ax.set_xticks(range(len(ev))); ax.set_xticklabels(list(ev), rotation=30); ax.set_yscale("log"); ax.set_title(f"corporate actions in the feed ({sum(ev.values()):,})")
    ax = axes[2]
    if cal:
        names = sorted(cal); ax.bar(range(len(names)), [cal[n]["mismatches"] for n in names], color=["C2" if cal[n]["mismatches"] <= 2 else "C1" for n in names])
        for i, n in enumerate(names):
            ax.text(i, cal[n]["mismatches"] + 0.3, f"{cal[n]['data_days']} days", ha="center", fontsize=6.5)
        ax.set_xticks(range(len(names))); ax.set_xticklabels(names); ax.set_ylabel("days where calendar and data disagree"); ax.set_title("exchange calendars vs the trading days in the data, 2007-2026")
    save(fig, "corpact.png")


def fig_tca():
    t = load("tca.json")
    p = os.path.join(R, "tca_orders.csv")
    if not t or not os.path.exists(p):
        return
    o = pd.read_csv(p); o = o[o["cum_qty"] > 0]
    fig, axes = plt.subplots(2, 2, figsize=(12, 7))
    ax = axes[0, 0]; s = t["summary"]; algos = list(s["by_algo"]); comps = ["is_delay_bp", "is_execution_bp", "is_opportunity_bp", "is_fees_bp"]
    tot = [s[c] for c in comps]; ax.bar(["delay", "execution", "opportunity", "fees", "total IS"], tot + [s["is_bp"]], color=["C1", "C0", "C4", "C7", "k"])
    ax.axhline(0, color="k", lw=0.5); ax.set_ylabel("bp of the decision price, notional-weighted"); ax.set_title(f"implementation shortfall on {s['n_orders']:,} orders (${s['notional_usd'] / 1e9:.1f}bn): Perold decomposition")
    ax = axes[0, 1]; sc = o[o["algo"] != "IS"]
    ax.scatter(100 * sc["pct_adv"], sc["benchmark_bp"].clip(-60, 60), s=6, alpha=0.4, c=[BCOL.get(b, "C7") for b in sc["broker"]])
    xs = np.geomspace(0.01, 15, 60); pm = t["pretrade"].get("coefficients")
    if pm:
        vol = float(sc["vol_day"].median()); spread = float(sc["spread_bp"].median()); ax.plot(xs, pm["const_bp"] + pm["impact"] * vol * 1e4 * np.sqrt(xs / 100) + pm["spread"] * spread, "k-", lw=1.2, label=f"pre-trade model (test RMSE {t['pretrade']['test_rmse_bp']:.1f} bp vs naive {t['pretrade']['naive_rmse_bp']:.1f})")
    ax.set_xscale("log"); ax.set_xlabel("order size, % of 20-day ADV (log)"); ax.set_ylabel("cost vs the algo's benchmark (bp)"); ax.set_title("schedule algos (VWAP, POV, close): cost vs size, coloured by broker"); ax.legend(fontsize=6.5)
    ax = axes[1, 0]; w = t["wheel"]
    if "brokers" in w:
        brokers = list(w["brokers"]); eff = [w["brokers"][b][[k for k in w["brokers"][b] if k.startswith("effect_bp")][0]] for b in brokers]; ci = [w["brokers"][b]["ci95"] for b in brokers]
        ax.bar(range(len(brokers)), eff, yerr=[[e - c[0] for e, c in zip(eff, ci)], [c[1] - e for e, c in zip(eff, ci)]], capsize=4, color=[BCOL.get(b, "C7") for b in brokers])
        for i, b in enumerate(brokers):
            ax.text(i, ci[i][1] + 0.3, f"P(best) {w['brokers'][b]['p_best']:.2f}\nn={w['brokers'][b]['n_orders']}", ha="center", fontsize=6.5)
        ax.set_ylim(min(0, min(c[0] for c in ci)) - 0.5, max(c[1] for c in ci) * 1.35 + 1)
        ax.set_xticks(range(len(brokers))); ax.set_xticklabels([f"{b} (true quality {q_})" for b, q_ in zip(brokers, ("0.85", "1.00", "1.25"))]); ax.axhline(0, color="k", lw=0.5); ax.set_ylabel(f"cost effect vs {brokers[0]} (bp), difficulty-adjusted"); ax.set_title(f"broker wheel on {w['n']} schedule-algo orders, residual sd {w['residual_sd_bp']:.0f} bp")
    ax = axes[1, 1]
    if "power" in w:
        deltas = [float(k.replace("bp", "")) for k in w["power"]]; ns = list(w["power"].values())
        ax.loglog(deltas, ns, "o-", color="C0"); ax.axhline(w["n"] / 3, color="C3", ls="--", lw=1, label=f"orders per broker in this run ({w['n'] // 3})")
        for d_, n_ in zip(deltas, ns):
            ax.annotate(f"{n_:,}", (d_, n_), textcoords="offset points", xytext=(4, 4), fontsize=6.5)
        ax.set_xlabel("difference to resolve (bp)"); ax.set_ylabel("orders per broker (80 % power, 5 % size)"); ax.set_title("how much flow a broker wheel needs"); ax.legend(fontsize=6.5)
    save(fig, "tca.png")


def fig_orders():
    run = load("run.json")
    if not run:
        return
    days = run["days"]; x = np.arange(len(days))
    fig, axes = plt.subplots(2, 2, figsize=(12, 6.8))
    ax = axes[0, 0]; ax.bar(x, [d["netting"]["gross"] / 1e6 for d in days], color="lightgrey", label="gross strategy deltas"); ax.bar(x, [d["netting"]["net"] / 1e6 for d in days], color="C0", label="net orders sent")
    ax.plot(x, [d["netting"]["crossed"] / 1e6 for d in days], "C3.-", ms=3, lw=0.8, label="crossed internally")
    ax.set_xticks(x[::5]); ax.set_xticklabels([d["date"][5:] for d in days][::5], rotation=45, fontsize=6.5); ax.set_ylabel("$m"); ax.set_title("portfolio-to-orders: netting across strategies each day"); ax.legend(fontsize=6.5)
    o = q("select date, algo, difficulty, exchange, broker, qty, cum_qty, status, ack_latency_ms, end_time - start_time as window_s, qty / adv as pct_adv from orders")
    ax = axes[0, 1]; o["day"] = o["date"].astype(str).str[:10]; piv = o.groupby(["day", "algo"]).size().unstack(fill_value=0); piv.index = [d[5:] for d in piv.index]; piv.plot(kind="bar", stacked=True, ax=ax, width=0.8); ax.set_xticks(range(0, len(piv), 5)); ax.set_xticklabels(list(piv.index)[::5], rotation=45, fontsize=6.5); ax.set_ylabel("orders"); ax.set_title("orders per day by algorithm"); ax.legend(fontsize=6.5, ncol=4)
    ax = axes[1, 0]
    for b in sorted(o["broker"].unique()):
        lat = o.loc[(o["broker"] == b) & o["ack_latency_ms"].notna(), "ack_latency_ms"]
        ax.hist(lat.clip(0, 400), bins=40, alpha=0.6, color=BCOL.get(b, "C7"), label=f"{b}: median {lat.median():.0f} ms, p95 {lat.quantile(0.95):.0f} ms")
    ax.set_xlabel("acknowledgement latency (ms, virtual clock)"); ax.set_ylabel("orders"); ax.set_title("NewOrderSingle to ExecutionReport(New), by broker"); ax.legend(fontsize=6.5)
    ax = axes[1, 1]; st = o.groupby(["algo", "status"]).size().unstack(fill_value=0); st.plot(kind="bar", stacked=True, ax=ax, width=0.7); ax.set_ylabel("orders"); ax.set_title("terminal state by algorithm"); ax.tick_params(axis="x", rotation=0); ax.legend(fontsize=6.5)
    save(fig, "orders.png")


def fig_checks():
    run = load("run.json")
    if not run:
        return
    ck = pd.DataFrame(run["checks"])
    if ck.empty:
        return
    piv = ck.groupby(["check_name", "status"])["n"].sum().unstack(fill_value=0)
    piv["name"] = [n.split(":")[0] for n in piv.index]; piv = piv.groupby("name").sum()
    fig, ax = plt.subplots(figsize=(11, 3.6))
    piv.reindex(columns=["ok", "warn", "fail"], fill_value=0).plot(kind="bar", stacked=True, ax=ax, color={"ok": "C2", "warn": "C1", "fail": "C3"}, width=0.75)
    ax.set_ylabel("check outcomes over the run"); ax.set_title("start-of-day and end-of-day checks"); ax.tick_params(axis="x", rotation=45); ax.legend()
    save(fig, "checks.png")


def fig_day():
    """one fault day: orders on a timeline with fills, alerts and the fault markers"""
    run = load("run.json")
    if not run:
        return
    fd = [d["date"] for d in run["days"] if d["fault_day"]]
    if not fd:
        return
    d = fd[min(2, len(fd) - 1)]
    o = q("select cl_ord_id, symbol, exchange, side, qty, cum_qty, algo, broker, start_time, end_time from orders where date = ? order by start_time, cl_ord_id", d)
    al = q("select time, rule, severity, cl_ord_id from alerts where date = ?", d); fa = q("select time, type, cl_ord_id, detected_time from faults where date = ?", d)
    if o.empty:
        return
    t0 = o["start_time"].min(); o = o.reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(12, 7))
    for i, r in o.iterrows():
        ax.plot([(r["start_time"] - t0) / 3600, (r["end_time"] - t0) / 3600], [i, i], color=BCOL.get(r["broker"], "C7"), lw=1.2, alpha=0.6)
        ax.plot([(r["start_time"] - t0) / 3600 + (r["end_time"] - r["start_time"]) / 3600 * min(1, r["cum_qty"] / max(r["qty"], 1))], [i], "k|", ms=4)
    idx = {c: i for i, c in enumerate(o["cl_ord_id"])}
    for _, a in al.iterrows():
        y = idx.get(a["cl_ord_id"], -3 - (hash(a["rule"]) % 3)); ax.plot((a["time"] - t0) / 3600, y, "v" if a["severity"] in ("high", "critical") else ".", color="C3" if a["severity"] in ("high", "critical") else "C1", ms=4, alpha=0.8)
    for _, f in fa.iterrows():
        y = idx.get(f["cl_ord_id"], -3); ax.plot((f["time"] - t0) / 3600, y, "x", color="k", ms=6)
    ax.set_yticks([]); ax.set_xlabel("hours after the first order"); ax.set_title(f"{d}: {len(o)} orders (colour = broker, tick = filled fraction), x = injected fault, red = high alert, orange = other alert; session-level alerts below the axis")
    save(fig, "day.png")


if __name__ == "__main__":
    for f in (fig_monitor, fig_recon, fig_corpact, fig_tca, fig_orders, fig_checks, fig_day):
        try:
            f()
        except Exception as e:  # noqa: BLE001
            print(f"{f.__name__}: skipped ({type(e).__name__}: {e})")
