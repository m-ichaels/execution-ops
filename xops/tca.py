"""Transaction cost analysis on the day's orders: implementation shortfall and its Perold decomposition (delay,
execution, opportunity), benchmark slippage (arrival, interval VWAP, close), the broker and algo wheel (slippage
regressed on order difficulty with broker fixed effects, bootstrap intervals, power analysis) and the square-root
pre-trade cost model fitted on the executions and tested out of sample."""
from __future__ import annotations

import numpy as np
import pandas as pd

FEE_BP = 0.5


def order_tca(orders: pd.DataFrame) -> pd.DataFrame:
    """orders: one row per parent order with side, qty, cum_qty, avg_px, decision_px, arrival_px, vwap_px, close_px, adv, vol_day, spread_bp"""
    o = orders.copy()
    sign = np.where(o["side"] == "1", 1.0, -1.0); filled = o["cum_qty"].clip(lower=0); frac = (filled / o["qty"]).clip(0, 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        dec = o["decision_px"]
        # Perold: everything in basis points of the decision price so that the parts add up exactly
        o["is_delay_bp"] = sign * (o["arrival_px"] - dec) / dec * 1e4
        o["is_execution_bp"] = sign * (o["avg_px"] - o["arrival_px"]) / dec * 1e4 * frac
        o["is_opportunity_bp"] = sign * (o["close_px"] - o["arrival_px"]) / dec * 1e4 * (1 - frac)
        o["is_fees_bp"] = FEE_BP * frac
        o["is_bp"] = o["is_delay_bp"] + o["is_execution_bp"] + o["is_opportunity_bp"] + o["is_fees_bp"]
        o["delay_bp"] = o["is_delay_bp"]; o["opportunity_bp"] = o["is_opportunity_bp"]
        # execution quality of what was filled, against the arrival mid
        o["execution_bp"] = sign * (o["avg_px"] / o["arrival_px"] - 1) * 1e4
        o["arrival_bp"] = o["execution_bp"]
        o["vwap_bp"] = sign * (o["avg_px"] / o["vwap_px"] - 1) * 1e4
        o["close_bp"] = sign * (o["avg_px"] / o["close_px"] - 1) * 1e4
    o["fill_rate"] = frac; o["pct_adv"] = o["qty"] / o["adv"].clip(lower=1)
    o["pretrade_bp"] = 0.5 * o["spread_bp"] + 0.6 * o["vol_day"] * 1e4 * np.sqrt(o["pct_adv"])
    o["benchmark_bp"] = np.where(o["algo"] == "IS", o["arrival_bp"], np.where(o["algo"] == "CLOSE", o["close_bp"], o["vwap_bp"]))
    for c in ("execution_bp", "vwap_bp", "close_bp", "benchmark_bp", "arrival_bp"):
        o.loc[filled <= 0, c] = np.nan
    return o


def summarize(tca: pd.DataFrame) -> dict:
    t = tca[tca["cum_qty"] > 0]
    w = t["cum_qty"] * t["avg_px"]
    def wmean(col, mask=None):
        m = t if mask is None else t[mask]; ww = m["cum_qty"] * m["avg_px"]
        return float(np.average(m[col].fillna(0), weights=ww)) if len(m) and ww.sum() > 0 else float("nan")
    out = {"n_orders": int(len(t)), "notional_usd": float(w.sum()), "is_bp": wmean("is_bp"), "is_delay_bp": wmean("is_delay_bp"), "is_execution_bp": wmean("is_execution_bp"), "is_opportunity_bp": wmean("is_opportunity_bp"), "is_fees_bp": wmean("is_fees_bp"), "execution_bp": wmean("execution_bp"), "vwap_bp": wmean("vwap_bp"), "fill_rate": float(np.average(t["fill_rate"], weights=t["qty"])) if len(t) else float("nan")}
    out["by_algo"] = {a: {"n": int(len(g)), "benchmark_bp": float(np.average(g["benchmark_bp"].fillna(0), weights=g["cum_qty"] * g["avg_px"])), "execution_bp": float(np.average(g["execution_bp"].fillna(0), weights=g["cum_qty"] * g["avg_px"])), "vwap_bp": float(np.average(g["vwap_bp"].fillna(0), weights=g["cum_qty"] * g["avg_px"])), "fill_rate": float(g["fill_rate"].mean())} for a, g in t.groupby("algo")}
    out["by_difficulty"] = {int(k): {"n": int(len(g)), "execution_bp": float(np.average(g["execution_bp"].fillna(0), weights=g["cum_qty"] * g["avg_px"])), "vwap_bp": float(np.average(g["vwap_bp"].fillna(0), weights=g["cum_qty"] * g["avg_px"])), "pretrade_bp": float(g["pretrade_bp"].mean())} for k, g in t.groupby("difficulty")}
    return out


SCHEDULE_ALGOS = ("VWAP", "POV", "CLOSE", "TWAP")


def wheel(tca: pd.DataFrame, brokers: list[str] | None = None, B: int = 2000, seed: int = 7, algos: tuple = SCHEDULE_ALGOS, y: str = "benchmark_bp") -> dict:
    """Broker fixed effects on each order's cost against its own benchmark (interval VWAP for VWAP and POV, the close
    for close orders; implementation-shortfall orders are excluded because arrival-relative cost carries the day's drift)
    after controlling for difficulty: sqrt(%ADV) x vol, spread, algo, urgency, region.  Bootstrap CIs over orders; the
    ranking with the probability each broker is best; and the power analysis: orders per broker needed to resolve a
    given difference at the observed residual dispersion."""
    t = tca[(tca["cum_qty"] > 0) & tca[y].notna() & tca["algo"].isin(algos)].copy(); t["y"] = t[y]
    if len(t) < 30:
        return {"n": int(len(t))}
    brokers = brokers or sorted(t["broker"].unique()); rng = np.random.default_rng(seed)
    t["x_impact"] = t["vol_day"] * 1e4 * np.sqrt(t["pct_adv"]); t["x_spread"] = t["spread_bp"]
    algos = sorted(t["algo"].unique()); regions = sorted(t["exchange"].unique())

    def design(df):
        cols = [np.ones(len(df)), df["x_impact"].values, df["x_spread"].values, (df["urgency"] == "high").astype(float).values]
        names = ["const", "impact", "spread", "urgent"]
        for a in algos[1:]:
            cols.append((df["algo"] == a).astype(float).values); names.append(f"algo_{a}")
        for r in regions[1:]:
            cols.append((df["exchange"] == r).astype(float).values); names.append(f"ex_{r}")
        for b in brokers[1:]:
            cols.append((df["broker"] == b).astype(float).values); names.append(f"broker_{b}")
        return np.column_stack(cols), names

    def fit(df):
        X, names = design(df); y = df["y"].values; w = np.sqrt(df["cum_qty"].values * df["avg_px"].values)
        beta = np.linalg.lstsq(X * w[:, None], y * w, rcond=None)[0]; return dict(zip(names, beta)), y - X @ beta

    coef, resid = fit(t)
    effects = {brokers[0]: 0.0} | {b: float(coef[f"broker_{b}"]) for b in brokers[1:]}
    boots = {b: [] for b in brokers}; best = {b: 0 for b in brokers}
    for _ in range(B):
        s = t.iloc[rng.integers(0, len(t), len(t))]
        try:
            c, _ = fit(s)
        except np.linalg.LinAlgError:
            continue
        e = {brokers[0]: 0.0} | {b: float(c[f"broker_{b}"]) for b in brokers[1:]}
        for b in brokers:
            boots[b].append(e[b])
        best[min(e, key=e.get)] += 1
    nb = max(1, sum(best.values()))
    out = {"n": int(len(t)), "coefficients": {k: float(v) for k, v in coef.items()}, "residual_sd_bp": float(np.std(resid)),
           "algos": list(algos), "benchmark": y,
           "brokers": {b: {"effect_bp_vs_" + brokers[0]: effects[b], "ci95": [float(np.quantile(boots[b], 0.025)), float(np.quantile(boots[b], 0.975))] if boots[b] else [None, None], "p_best": best[b] / nb, "n_orders": int((t["broker"] == b).sum()), "raw_cost_bp": float(np.average(t.loc[t["broker"] == b, "y"], weights=(t.loc[t["broker"] == b, "cum_qty"] * t.loc[t["broker"] == b, "avg_px"]))),
                           "by_algo_bp": {a: float(np.average(g["y"], weights=g["cum_qty"] * g["avg_px"])) for a, g in t[t["broker"] == b].groupby("algo")}} for b in brokers}}
    # power: two-sample difference of means with the residual sd; n per broker to detect delta at 80% power, 5% size
    sd = float(np.std(resid)); z = 1.96 + 0.84
    out["power"] = {f"{delta}bp": int(np.ceil(2 * (z * sd / delta) ** 2)) for delta in (0.5, 1.0, 2.0, 5.0)}
    out["ranking"] = sorted(brokers, key=lambda b: effects[b])
    return out


def pretrade_model(tca: pd.DataFrame, seed: int = 7, algos: tuple = SCHEDULE_ALGOS) -> dict:
    """cost_bp = a + b * sigma_day_bp * sqrt(Q/ADV) + c * spread_bp on the schedule-algo orders against their benchmark;
    fitted on the first two thirds of the days, tested on the last third."""
    t = tca[(tca["cum_qty"] > 0) & tca["benchmark_bp"].notna() & tca["algo"].isin(algos)].copy().sort_values("date")
    if len(t) < 40:
        return {"n": int(len(t))}
    t["x_impact"] = t["vol_day"] * 1e4 * np.sqrt(t["pct_adv"])
    days = sorted(t["date"].unique()); cut = days[int(len(days) * 2 / 3)]
    tr, te = t[t["date"] < cut], t[t["date"] >= cut]
    X = lambda df: np.column_stack([np.ones(len(df)), df["x_impact"].values, df["spread_bp"].values])
    beta = np.linalg.lstsq(X(tr), tr["benchmark_bp"].values, rcond=None)[0]
    pred = X(te) @ beta; err = te["benchmark_bp"].values - pred
    naive = te["benchmark_bp"].values - tr["benchmark_bp"].mean()
    return {"n_train": int(len(tr)), "n_test": int(len(te)), "coefficients": {"const_bp": float(beta[0]), "impact": float(beta[1]), "spread": float(beta[2])}, "test_rmse_bp": float(np.sqrt(np.mean(err ** 2))), "naive_rmse_bp": float(np.sqrt(np.mean(naive ** 2))), "test_corr": float(np.corrcoef(pred, te["benchmark_bp"].values)[0, 1]) if len(te) > 3 else None, "cut_date": str(cut)}
