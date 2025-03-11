#!/usr/bin/env python3
"""report.pdf from results/summary.md and results/figures/*.png (fpdf2).   python scripts/report.py [results] [report.pdf]"""
import os
import sys

from fpdf import FPDF

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
R = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "results")
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "report.pdf")

INTRO = """Question. A systematic multi-asset fund sends hundreds of orders a day through brokers' algorithms. What has to be checked before, during and after the day for the book to be right, how quickly can a monitor catch what goes wrong, and what does execution cost? This stack answers with measured numbers: a fault-injection harness for the live monitor, a scored three-way reconciliation, a corporate-actions engine checked against twenty years of vendor data, exchange and contract calendars checked against the data, and transaction cost analysis with a broker wheel.

Method. Python package (xops) with its own FIX 4.4 engine (session layer with heartbeats, test requests, resend and gap fill, sequence persistence; NewOrderSingle, cancel, cancel-replace, ExecutionReport, OrderCancelReject) that runs identically on real sockets and on the simulator's virtual clock. Three simple systematic strategies on free daily data generate the flow; portfolio-to-orders nets across strategies, rounds, applies the restricted list, locates and participation caps and schedules by exchange session; three simulated brokers run VWAP, TWAP, POV, implementation-shortfall and close algorithms against a reduced-form intraday market with square-root impact and report every fill through FIX and a drop-copy session. The monitor keeps an order state machine per order and evaluates its rules every fifteen seconds. The end of day reconciles the blotter, the drop copy, a simulated prime-broker file with seeded breaks, and positions, in SQL on DuckDB. Fault days alternate with clean days so that detection and false-alert rates are both measured.

Caveats. Brokers, fills and the prime broker are simulated; the price data, corporate actions, calendars and the vendor's inconsistencies are real. The market model is reduced-form, so the transaction-cost levels are stylised; the monitor, reconciliation and corporate-action results do not depend on it."""

FIGS = [("day.png", "One fault day: every order on a timeline, coloured by broker, with injected faults (x) and the monitor's alerts."),
        ("monitor.png", "Fault injection: detection by fault type and time to detect, incidents per day on fault and clean days, alerts on clean days by rule."),
        ("recon.png", "End-of-day reconciliation: seeded breaks found by type, every break by how it was explained, fills reconciled per day."),
        ("corpact.png", "Corporate actions and calendars: dividend factors against Yahoo's adjusted close (and the LSE unit inconsistency), events in the feed, calendar-versus-data mismatches."),
        ("tca.png", "Transaction costs: implementation-shortfall decomposition, cost against size with the pre-trade model, broker wheel effects with intervals, and the flow a wheel needs."),
        ("orders.png", "Portfolio-to-orders: netting per day, algorithm mix, acknowledgement latency by broker, terminal states."),
        ("checks.png", "Start-of-day and end-of-day check outcomes over the run.")]


class PDF(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 9); self.set_text_color(120); self.cell(0, 6, "systematic-execution-ops - portfolio-to-orders, FIX order lifecycle, live monitor, reconciliation, corporate actions", align="R"); self.ln(8); self.set_text_color(0)

    def footer(self):
        self.set_y(-12); self.set_font("Helvetica", "", 8); self.set_text_color(120); self.cell(0, 6, f"{self.page_no()}", align="C")


def clean(s):
    return (s.replace("–", "-").replace("—", "-").replace("−", "-").replace("×", "x").replace("≥", ">=").replace("≤", "<=").replace("…", "...").replace("²", "^2").replace("±", "+/-").replace("**", "").replace("`", "")
             .replace("→", "->").replace("≈", "~").replace("é", "e").replace("ö", "o").replace("'", "'").replace("’", "'"))


def md_table(pdf, rows):
    cols = [c.strip() for c in rows[0].strip("|").split("|")]
    data = [[clean(c.strip()) for c in r.strip("|").split("|")] for r in rows[2:]]
    n = len(cols); w = (pdf.w - 20) / n; fs = 6.5 if n <= 7 else 5.2; cut = 42 if n <= 7 else 24
    pdf.set_font("Helvetica", "B", fs)
    for c in cols:
        pdf.cell(w, 5, clean(c)[:cut], border=1)
    pdf.ln(5); pdf.set_font("Helvetica", "", fs)
    for r in data[:80]:
        if pdf.get_y() > pdf.h - 20:
            pdf.add_page()
        for c in r:
            pdf.cell(w, 4.5, c[:cut], border=1)
        pdf.ln(4.5)
    pdf.ln(2)


def main():
    pdf = PDF(); pdf.set_auto_page_break(auto=True, margin=15); pdf.add_page()
    pdf.set_font("Helvetica", "B", 16); pdf.cell(0, 10, "Systematic Execution Operations Stack", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    for para in INTRO.split("\n\n"):
        pdf.multi_cell(0, 4.5, clean(para)); pdf.ln(2)
    for fn, cap in FIGS:
        p = os.path.join(R, "figures", fn)
        if not os.path.exists(p):
            continue
        if pdf.get_y() > pdf.h - 90:
            pdf.add_page()
        pdf.image(p, w=pdf.w - 20); pdf.set_font("Helvetica", "I", 8); pdf.multi_cell(0, 4, clean(cap)); pdf.ln(3); pdf.set_font("Helvetica", "", 9)
    sm = os.path.join(R, "summary.md")
    if os.path.exists(sm):
        pdf.add_page(); lines = open(sm, encoding="utf-8").read().splitlines(); i = 0
        while i < len(lines):
            l = lines[i]
            if l.startswith("## "):
                pdf.set_font("Helvetica", "B", 11); pdf.ln(2); pdf.cell(0, 7, clean(l[3:]), new_x="LMARGIN", new_y="NEXT"); pdf.set_font("Helvetica", "", 9); i += 1
            elif l.startswith("### "):
                pdf.set_font("Helvetica", "B", 9); pdf.cell(0, 6, clean(l[4:]), new_x="LMARGIN", new_y="NEXT"); pdf.set_font("Helvetica", "", 9); i += 1
            elif l.startswith("|"):
                j = i
                while j < len(lines) and lines[j].startswith("|"):
                    j += 1
                if j - i >= 2:
                    md_table(pdf, lines[i:j])
                i = j
            elif l.startswith("# "):
                i += 1
            elif l.strip():
                pdf.set_x(pdf.l_margin); pdf.multi_cell(0, 4.5, clean(l.strip())); i += 1
            else:
                i += 1
    pdf.output(OUT); print("wrote", OUT)


if __name__ == "__main__":
    main()
