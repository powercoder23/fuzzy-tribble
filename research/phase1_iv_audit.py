"""
Phase 1 IV research audit — builds a tidy (symbol, date) panel from the
tables that actually have enough history to be tested today, and runs
simple, hard-to-overfit univariate checks for hypotheses #1-4 from the
research audit (cross-sectional IV rank, IV momentum, OI-buildup
classification, gap behaviour). Hypotheses needing skew/term-structure or a
longer VIX/RV history are intentionally NOT attempted here — see the
"skipped" section printed at the end.

Every result is printed with its sample count (n). Read the n before the
number: a mean over 40 rows is a curiosity, not a strategy.

Usage:
    python -m research.phase1_iv_audit
"""

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

DB_PATH = Path("data") / "iv_history.db"
OUT_DIR = Path("research") / "output"
RV_WINDOWS = (5, 10, 20)
FORWARD_HORIZONS = (1, 3, 5)


def _connect():
    return sqlite3.connect(DB_PATH, timeout=30)


def load_iv_panel(conn) -> pd.DataFrame:
    """One row per (symbol, date). Dedups multiple 'daily' rows per calendar
    day the same way iv_store.get_iv_history does: keep the LAST row of the
    day (highest id), not an average — see iv_store.py's pollution note."""
    df = pd.read_sql("""
        SELECT id, symbol, DATE(timestamp) AS date, atm_iv, atm_call_iv, atm_put_iv,
               spot_price, total_call_oi, total_put_oi,
               total_call_volume, total_put_volume
        FROM iv_history
        WHERE data_type = 'daily' AND atm_iv BETWEEN 1.0 AND 200.0
        ORDER BY symbol, date, id
    """, conn)
    df = df.drop_duplicates(subset=["symbol", "date"], keep="last").drop(columns=["id"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def load_price_panel(conn) -> pd.DataFrame:
    df = pd.read_sql("""
        SELECT date, symbol, open, high, low, close, volume, deliv_qty, deliv_pct
        FROM delivery_daily
    """, conn)
    df["date"] = pd.to_datetime(df["date"])
    return df


def load_oi_buildup_panel(conn) -> pd.DataFrame:
    """OI buildup is scanned several times a day — take the LAST classification
    of the day (closest to close, most information)."""
    df = pd.read_sql("""
        SELECT id, symbol, DATE(timestamp) AS date, classification, bias, strength, pcr
        FROM oi_buildup_history
        ORDER BY symbol, date, id
    """, conn)
    df = df.drop_duplicates(subset=["symbol", "date"], keep="last").drop(columns=["id"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def load_gap_panel(conn) -> pd.DataFrame:
    """Gap is set at the open — take the FIRST row of the day."""
    df = pd.read_sql("""
        SELECT id, symbol, DATE(timestamp) AS date, direction, bias, extreme, gap_pct
        FROM gap_history
        ORDER BY symbol, date, id
    """, conn)
    df = df.drop_duplicates(subset=["symbol", "date"], keep="first").drop(columns=["id"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def build_panel() -> pd.DataFrame:
    conn = _connect()
    try:
        iv = load_iv_panel(conn)
        px = load_price_panel(conn)
        oi = load_oi_buildup_panel(conn)
        gap = load_gap_panel(conn)
    finally:
        conn.close()

    panel = iv.merge(px, on=["symbol", "date"], how="left", suffixes=("", "_px"))
    panel = panel.merge(oi, on=["symbol", "date"], how="left")
    panel = panel.merge(gap, on=["symbol", "date"], how="left", suffixes=("", "_gap"))
    panel = panel.sort_values(["symbol", "date"]).reset_index(drop=True)

    g = panel.groupby("symbol", group_keys=False)

    # Realized vol from delivery_daily close-to-close returns. min_periods=window
    # forces NaN until a full window exists — no partial-window RV masquerading
    # as a full estimate.
    panel["log_ret"] = g["close"].apply(lambda s: np.log(s / s.shift(1)))
    for w in RV_WINDOWS:
        panel[f"rv_{w}d"] = g["log_ret"].transform(
            lambda s, w=w: s.rolling(w, min_periods=w).std() * np.sqrt(252)
        )

    # IV momentum, absolute and % — raw signal, no smoothing.
    for h in (1, 3, 5):
        panel[f"iv_mom_{h}d"] = g["atm_iv"].transform(lambda s, h=h: s.diff(h))
        panel[f"iv_mom_{h}d_pct"] = g["atm_iv"].transform(lambda s, h=h: s.pct_change(h))

    # Time-series IV rank against the symbol's OWN history (expanding, so it's
    # only ever using data available up to that date). Flagged unreliable
    # under 30 observations rather than silently reported.
    def _ts_pct(s: pd.Series) -> pd.Series:
        return s.expanding(min_periods=1).apply(lambda w: (w.iloc[-1] > w).mean(), raw=False)

    panel["iv_ts_pct"] = g["atm_iv"].transform(_ts_pct)
    panel["iv_ts_pct_n"] = g["atm_iv"].cumcount() + 1
    panel["iv_ts_pct_reliable"] = panel["iv_ts_pct_n"] >= 30

    # Cross-sectional IV percentile: rank across ALL symbols on the SAME date.
    # This is the one ranking metric with real sample size today (~216 names).
    panel["iv_xs_pct"] = panel.groupby("date")["atm_iv"].rank(pct=True)
    panel["iv_xs_n"] = panel.groupby("date")["atm_iv"].transform("count")

    # Forward labels — shift NEGATIVE within each symbol, i.e. look ahead.
    # fwd_ret_Nd = cumulative log return from t to t+N (uses log_ret already computed).
    for h in FORWARD_HORIZONS:
        panel[f"fwd_ret_{h}d"] = g["log_ret"].transform(
            lambda s, h=h: s.shift(-1).rolling(h, min_periods=h).sum().shift(-(h - 1))
        )
    # Forward realized vol over the next 5/10 days (does IV momentum predict
    # a REAL subsequent vol expansion, not just a same-day co-movement).
    for w in (5, 10):
        panel[f"fwd_rv_{w}d"] = g["log_ret"].transform(
            lambda s, w=w: s.shift(-1).rolling(w, min_periods=w).std().shift(-(w - 1)) * np.sqrt(252)
        )

    return panel


def _report_quantile_test(df: pd.DataFrame, bucket_col: str, target_col: str, q: int, label: str):
    d = df[[bucket_col, target_col]].dropna()
    if len(d) < q * 20:
        print(f"  [{label}] SKIPPED — only {len(d)} usable rows, too few for {q}-way bucketing")
        return
    d = d.copy()
    d["bucket"] = pd.qcut(d[bucket_col], q, labels=False, duplicates="drop")
    summary = d.groupby("bucket")[target_col].agg(["mean", "count"])
    print(f"  [{label}] n_total={len(d)}")
    print(summary.to_string())
    if len(summary) >= 2:
        spread = summary["mean"].iloc[-1] - summary["mean"].iloc[0]
        print(f"  top-bucket minus bottom-bucket spread: {spread:.5f}")


def _report_categorical_test(df: pd.DataFrame, cat_col: str, target_col: str, label: str, min_n: int = 30):
    """Reports each category's mean/n/95% CI AND its excess over the
    unconditional baseline (all rows with a valid target_col, regardless of
    whether cat_col is populated) — so a signal can't be mistaken for
    same-period market drift. Also runs a Welch's t-test between the two
    largest categories, since that's usually the interesting comparison
    (e.g. the two opposite-biased classes)."""
    from scipy import stats

    baseline = df[target_col].dropna()
    baseline_mean = baseline.mean()

    d = df[[cat_col, target_col]].dropna()
    grouped = {cat: g[target_col].values for cat, g in d.groupby(cat_col)}
    rows = []
    for cat, vals in grouped.items():
        n = len(vals)
        mean = vals.mean()
        se = vals.std(ddof=1) / np.sqrt(n) if n > 1 else np.nan
        ci95 = 1.96 * se
        rows.append({
            "category": cat, "n": n, "mean": mean,
            "ci95_lo": mean - ci95, "ci95_hi": mean + ci95,
            "excess_vs_baseline": mean - baseline_mean,
        })
    summary = pd.DataFrame(rows).sort_values("n", ascending=False)

    print(f"  [{label}] n_total={len(d)} | unconditional baseline mean={baseline_mean:.5f} (n={len(baseline)})")
    print(summary.to_string(index=False, float_format=lambda x: f"{x:.5f}"))
    thin = summary[summary["n"] < min_n]
    if not thin.empty:
        print(f"  NOTE: categories below n={min_n} are noise, not signal: {list(thin['category'])}")

    top2 = summary.nlargest(2, "n")["category"].tolist()
    if len(top2) == 2:
        a, b = grouped[top2[0]], grouped[top2[1]]
        t_stat, p_val = stats.ttest_ind(a, b, equal_var=False)
        print(f"  Welch's t-test, '{top2[0]}' (n={len(a)}) vs '{top2[1]}' (n={len(b)}): "
              f"t={t_stat:.3f}, p={p_val:.4f}"
              f"{' -- NOT significant at 5%' if p_val >= 0.05 else ' -- significant at 5%'}")


def _report_rank_ic(df: pd.DataFrame, x_col: str, y_col: str, label: str):
    d = df[[x_col, y_col]].dropna()
    if len(d) < 50:
        print(f"  [{label}] SKIPPED — only {len(d)} usable rows")
        return
    ic = d[x_col].corr(d[y_col], method="spearman")
    print(f"  [{label}] rank-IC (spearman) = {ic:.4f}  (n={len(d)})")


def run_hypothesis_tests(panel: pd.DataFrame) -> None:
    print("\n" + "=" * 70)
    print("HYPOTHESIS 1 — cross-sectional IV percentile -> forward realized vol")
    print("(does a name cheap/expensive RELATIVE TO PEERS on IV today see")
    print(" different forward vol than the rest of the universe?)")
    print("=" * 70)
    _report_quantile_test(panel, "iv_xs_pct", "fwd_rv_10d", 5, "IV xs-percentile quintile -> fwd 10d RV")

    print("\n" + "=" * 70)
    print("HYPOTHESIS 2 — IV momentum -> forward realized vol / forward move")
    print("(this is the literal thesis behind the live Vol-Expansion strategy:")
    print(" does rising IV predict a REAL subsequent vol expansion?)")
    print("=" * 70)
    _report_rank_ic(panel, "iv_mom_3d", "fwd_rv_5d", "IV 3d-momentum vs fwd 5d RV")
    panel["fwd_abs_ret_5d"] = panel["fwd_ret_5d"].abs()
    _report_quantile_test(panel, "iv_mom_3d", "fwd_abs_ret_5d", 3, "IV 3d-momentum tercile -> fwd 5d |return|")

    print("\n" + "=" * 70)
    print("HYPOTHESIS 3 — OI-buildup classification -> forward return")
    print("=" * 70)
    _report_categorical_test(panel, "classification", "fwd_ret_5d", "OI classification -> fwd 5d return")

    print("\n" + "=" * 70)
    print("HYPOTHESIS 4 — gap direction/extremity -> forward return")
    print("=" * 70)
    _report_categorical_test(panel, "direction", "fwd_ret_5d", "Gap direction -> fwd 5d return")
    panel["extreme_label"] = panel["extreme"].map({1: "extreme", 0: "normal"})
    _report_categorical_test(panel, "extreme_label", "fwd_ret_1d", "Gap extremity -> fwd 1d return")

    print("\n" + "=" * 70)
    print("SKIPPED (data not adequate yet — see audit notes)")
    print("=" * 70)
    print("  H5 IV-RV spread / vol risk premium   — RV series only ~34d, too short for a regime read")
    print("  H8 term structure slope/inversion    — skew_snapshots only ~5 trading days of history")
    print("  H9 skew level/change                 — same skew_snapshots retention gap")
    print("  H10 IV vs VIX regime interaction     — vix_daily only 40 rows")


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    panel = build_panel()
    out_path = OUT_DIR / "phase1_panel.csv"
    panel.to_csv(out_path, index=False)
    print(f"Panel built: {len(panel)} rows, {panel['symbol'].nunique()} symbols, "
          f"{panel['date'].min().date()} -> {panel['date'].max().date()}")
    print(f"Saved to {out_path}")

    run_hypothesis_tests(panel)


if __name__ == "__main__":
    main()
