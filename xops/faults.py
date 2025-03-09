"""Fault injection: the catalogue of things that go wrong between a fund and its brokers, a schedule generator that
plants them in a day's orders, and the scorer that turns the monitor's alerts into detection rates and times."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .broker import Fault

# fault type -> alert rules that count as a detection; empty = not the monitor's job (found by the EOD reconciliation)
RULES_FOR = {
    "drop_ack": ["UNACKED"],
    "stuck": ["STALLED", "BEHIND_SCHEDULE", "LEAVES_AFTER_END"],
    "dup_fill": ["STATE_ANOMALY"],
    "bad_price": ["PRICE_COLLAR", "SLIPPAGE"],
    "over_participation": ["PARTICIPATION", "AHEAD_SCHEDULE"],
    "wrong_side": ["STATE_ANOMALY"],
    "symbol_mismatch": ["STATE_ANOMALY", "UNKNOWN_ORDER"],
    "late_fill_after_cancel": ["STATE_ANOMALY"],
    "reject_storm": ["REJECT_RATE"],
    "stale_leaves": ["STATE_ANOMALY"],
    "overfill": ["STATE_ANOMALY"],
    "silent_session": ["SESSION_SILENT", "SESSION_LOST"],
    "seq_gap": ["SEQ_GAP"],
    "latency_spike": ["LATENCY"],
    "dropcopy_missing": [],
}
ORDER_LEVEL = {"drop_ack", "stuck", "dup_fill", "bad_price", "over_participation", "wrong_side", "symbol_mismatch", "late_fill_after_cancel", "stale_leaves", "overfill", "dropcopy_missing"}
SESSION_LEVEL = {"silent_session", "seq_gap", "latency_spike", "reject_storm"}
DEFAULT_MIX = {"drop_ack": 2, "stuck": 2, "dup_fill": 2, "bad_price": 2, "over_participation": 1, "wrong_side": 1, "symbol_mismatch": 1, "late_fill_after_cancel": 1, "reject_storm": 1, "stale_leaves": 1, "overfill": 1, "silent_session": 1, "seq_gap": 1, "latency_spike": 1, "dropcopy_missing": 2}


def generate_schedule(specs: list, rng: np.random.Generator, mix: dict[str, int] | None = None, brokers: tuple = ("BRK1", "BRK2", "BRK3")) -> dict[str, list[Fault]]:
    """Plant faults on a random subset of the day's orders (one fault per order, orders with at least two hours of
    window) and on random brokers for the session-level ones.  Returns faults per broker."""
    mix = dict(mix or DEFAULT_MIX)
    eligible = [s for s in specs if s.end_time - s.start_time >= 2 * 3600 and s.symbol not in ("BA",)]
    rng.shuffle(eligible)
    # faults whose detection needs a schedule to measure against go on schedule algos with enough size
    big = [s for s in eligible if s.qty >= 5000 and s.algo in ("VWAP", "TWAP", "POV")]
    used: set = set(); out = {b: [] for b in brokers}
    for ftype, n in mix.items():
        for _ in range(n):
            if ftype in ORDER_LEVEL:
                pool = big if ftype in ("stuck", "over_participation") else eligible
                cands = [s for s in pool if s.cl_ord_id not in used]
                if not cands:
                    break
                s = cands[0]; used.add(s.cl_ord_id)
                lo, hi = s.start_time + 15 * 60, s.end_time - 30 * 60
                t = float(rng.uniform(lo, hi)) if hi > lo else s.start_time + 60
                if ftype == "drop_ack":
                    t = s.start_time - 120          # armed before the order goes out
                if ftype in ("stuck", "dup_fill", "bad_price", "wrong_side", "symbol_mismatch", "stale_leaves", "overfill", "dropcopy_missing"):
                    t = max(t, s.start_time + 20 * 60)
                if ftype in ("stuck", "over_participation"):
                    t = s.start_time + float(rng.uniform(20, 60)) * 60     # early enough that the schedule still expects most of the order
                if ftype == "late_fill_after_cancel":
                    t = s.start_time - 120          # armed; fires on the cancel the fund sends for this order
                params = {"pct": 0.03} if ftype == "bad_price" else {"symbol": "ZZZZ"} if ftype == "symbol_mismatch" else {}
                out[s.broker].append(Fault(ftype, t, s.cl_ord_id, params))
            else:
                b = brokers[int(rng.integers(0, len(brokers)))]
                times = [s.start_time for s in specs if s.broker == b] or [specs[0].start_time]
                t0 = float(min(times)) + 3600; t1 = float(max(s.end_time for s in specs if s.broker == b)) - 3600 if any(s.broker == b for s in specs) else t0 + 3600
                t = float(rng.uniform(t0, max(t0 + 60, t1)))
                if ftype == "reject_storm":
                    t = float(min(times)) - 120        # armed before the first orders go out, so there are orders to reject
                params = {"n": 5} if ftype == "reject_storm" else {"seconds": 2.0, "duration": 300} if ftype == "latency_spike" else {}
                out[b].append(Fault(ftype, t, "", params))
    return out


@dataclass
class Detection:
    type: str; cl_ord_id: str; broker: str; t_fault: float; t_alert: float | None; rule: str | None

    @property
    def detected(self) -> bool:
        return self.t_alert is not None

    @property
    def ttd(self) -> float | None:
        return None if self.t_alert is None else self.t_alert - self.t_fault


def score(faults: dict[str, list[Fault]], alerts: list, spec_broker: dict[str, str], window: float = 3600.0, cancel_times: dict[str, float] | None = None) -> list[Detection]:
    """First alert of a matching rule after the fault fired (order-level: same order; session-level: same broker),
    within `window` seconds; faults that never fired (e.g. the order was already done) are not counted."""
    out = []; cancel_times = cancel_times or {}
    for broker, fl in faults.items():
        for f in fl:
            if f.fired_at is None and f.type not in ("silent_session", "seq_gap"):
                continue
            t0 = f.fired_at if f.fired_at is not None else f.t
            if f.type == "late_fill_after_cancel":
                t0 = max(t0, cancel_times.get(f.cl_ord_id, t0))
            rules = RULES_FOR.get(f.type, [])
            best = None
            for a in alerts:
                if a["rule"] not in rules or a["time"] < t0 - 1 or a["time"] > t0 + window:
                    continue
                if f.type in ORDER_LEVEL and a["cl_ord_id"] not in (f.cl_ord_id, "") and not (f.type == "symbol_mismatch"):
                    continue
                if f.type in SESSION_LEVEL and a.get("broker", broker) != broker:
                    continue
                if best is None or a["time"] < best["time"]:
                    best = a
            out.append(Detection(f.type, f.cl_ord_id, broker, t0, best["time"] if best else None, best["rule"] if best else None))
    return out


def summarize(dets: list[Detection], clean_alerts: list | None = None, n_clean_days: int = 0) -> dict:
    by = {}
    for d in dets:
        s = by.setdefault(d.type, {"injected": 0, "detected": 0, "ttd": []})
        s["injected"] += 1
        if d.detected:
            s["detected"] += 1; s["ttd"].append(d.ttd)
    for k, s in by.items():
        s["median_ttd_s"] = float(np.median(s["ttd"])) if s["ttd"] else None; s["p90_ttd_s"] = float(np.quantile(s["ttd"], 0.9)) if s["ttd"] else None; s.pop("ttd")
    tot_inj = sum(s["injected"] for s in by.values()); tot_det = sum(s["detected"] for s in by.values())
    mon_inj = sum(s["injected"] for t, s in by.items() if RULES_FOR.get(t)); mon_det = sum(s["detected"] for t, s in by.items() if RULES_FOR.get(t))
    all_ttd = [d.ttd for d in dets if d.detected]
    out = {"by_type": by, "injected": tot_inj, "detected": tot_det, "detection_rate": tot_det / tot_inj if tot_inj else None, "monitor_injected": mon_inj, "monitor_detected": mon_det, "monitor_detection_rate": mon_det / mon_inj if mon_inj else None,
           "median_ttd_s": float(np.median(all_ttd)) if all_ttd else None, "p90_ttd_s": float(np.quantile(all_ttd, 0.9)) if all_ttd else None}
    if clean_alerts is not None:
        rules = {}
        for a in clean_alerts:
            rules[a["rule"]] = rules.get(a["rule"], 0) + 1
        out["false_alerts_per_clean_day"] = (len(clean_alerts) / n_clean_days) if n_clean_days else None; out["false_alerts_by_rule"] = rules
    return out
