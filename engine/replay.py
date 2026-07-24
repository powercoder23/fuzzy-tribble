# -*- coding: utf-8 -*-
"""Offline formula replay (P0.4) — test weight changes before shipping them.

Re-scores every journaled decision from its stored factor_json under a variant
config, re-derives EMIT/REJECT (recomputing only what the variant can change:
score, grade floor, factors_not_contradicting, confluence), and grades the
counterfactual emit set against engine_outcomes. Decisions journaled before the
variant existed are the backtest; nothing is written anywhere.

Discipline (trader rules, enforced here):
  * Train / validation split by DAY — a variant is judged on days it was not
    tuned on. Default: validation = last 5 trading days of the journal.
  * REJECTED rows must be labeled too (labeler statuses=EMITTED,REJECTED),
    else newly-emitted counterfactuals have no outcomes = survivorship bias.
  * Only same-formula rows are replayed (FORMULA_VER stamps guard attribution).

Run:  python -m engine.replay
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from engine import config as cfg

TS_DAY = slice(0, 10)


# --------------------------------------------------------------------------- #
# Variant definition
# --------------------------------------------------------------------------- #
@dataclass
class Variant:
    name: str
    note: str = ""
    weights: dict = field(default_factory=dict)   # factor -> weight override
    w_premium_value: float | None = None          # None = keep cfg
    flip_bias: tuple = ()                         # factors whose CE/PE vote is inverted
    w_trigger: float | None = None

    def weight(self, factor: str) -> float:
        base = {"oi_flow": cfg.W_OI_FLOW, "trend": cfg.W_TREND,
                "sector_rs": cfg.W_SECTOR_RS, "inst_flow": cfg.W_INST_FLOW,
                "gap": cfg.W_GAP}[factor]
        return self.weights.get(factor, base)


VARIANTS = [
    Variant("baseline", "current v2.0-p0 formula (sanity check)"),
    Variant("inst0", "inst_flow weight -> 0 (EOD/BTST signal has no 60-min edge)",
            weights={"inst_flow": 0.0}),
    Variant("gap0", "gap weight -> 0 (kill the continuation vote)",
            weights={"gap": 0.0}),
    Variant("gapflip", "gap as FADE — invert its directional vote",
            flip_bias=("gap",)),
    Variant("pv0", "premium_value scores 0 (stays as EXPENSIVE gate only)",
            w_premium_value=0.0),
    Variant("combo_drop", "inst0 + gap0 + pv0",
            weights={"inst_flow": 0.0, "gap": 0.0}, w_premium_value=0.0),
    Variant("combo_fade", "inst0 + gapflip + pv0",
            weights={"inst_flow": 0.0}, flip_bias=("gap",), w_premium_value=0.0),
]


# --------------------------------------------------------------------------- #
# Pure re-scoring (mirrors conviction.score, parameterized)
# --------------------------------------------------------------------------- #
def _alignment(bias, strength, direction, flipped: bool) -> float:
    if bias not in ("CE", "PE"):
        return 0.0
    if flipped:
        bias = "PE" if bias == "CE" else "CE"
    return strength if bias == direction else -strength


def rescore(v: Variant, direction: str, trig_quality: float,
            factors: list[dict], vix) -> dict:
    """factors: parsed factor_json rows ({name,bias,strength,detail})."""
    w_trig = v.w_trigger if v.w_trigger is not None else cfg.W_TRIGGER
    total = w_trig * (trig_quality or 0.5)
    n_agree = 0
    net_align = 0.0                       # for factors_not_contradicting gate
    fmap = {f["name"]: f for f in factors}

    for name in ("oi_flow", "trend", "sector_rs", "inst_flow", "gap"):
        f = fmap.get(name)
        if not f:
            continue
        a = _alignment(f.get("bias"), f.get("strength") or 0.0,
                       direction, name in v.flip_bias)
        contrib = v.weight(name) * a
        total += contrib
        if contrib > 0:
            n_agree += 1
        # gate uses UNWEIGHTED alignment of all non-pv factors (conviction.run_gates)
        net_align += a

    pv = fmap.get("premium_value")
    w_pv = v.w_premium_value if v.w_premium_value is not None else cfg.W_PREMIUM_VALUE
    total += w_pv * ((pv.get("strength") or 0.0) if pv else 0.0)

    if n_agree >= cfg.CONFLUENCE_MIN_AGREE:
        total *= (1.0 + cfg.CONFLUENCE_BONUS)
    if vix is not None and cfg.VIX_ELEVATED <= vix < cfg.VIX_RED:
        total *= (1.0 - cfg.VIX_ELEVATED_PENALTY)

    s = max(0.0, min(100.0, total))
    grade = ("A+" if s >= cfg.GRADE_A_PLUS else
             "A" if s >= cfg.GRADE_A else
             "B" if s >= cfg.GRADE_B else None)
    return {"score": round(s, 1), "grade": grade, "net_align": net_align}


# --------------------------------------------------------------------------- #
# Data loading
# --------------------------------------------------------------------------- #
def load_rows(db_path: str, formula_ver: str | None = None):
    """Journaled EMITTED+REJECTED decisions with parsed factors, gate results,
    regime vix (ts-joined) and outcome edge. WATCH is skipped (no direction)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ver = formula_ver or cfg.FORMULA_VER
    rows = conn.execute("""
        SELECT d.id, d.ts, d.symbol, d.status, d.direction, d.score AS orig_score,
               d.grade AS orig_grade, d.trigger_quality, d.factor_json, d.gate_json,
               r.vix, o.edge_60, o.hit_60
        FROM engine_decisions d
        LEFT JOIN engine_regime r ON r.ts = d.ts
        LEFT JOIN engine_outcomes o ON o.decision_id = d.id
        WHERE d.status IN ('EMITTED','REJECTED') AND d.direction IS NOT NULL
          AND d.formula_ver = ?
        ORDER BY d.ts""", (ver,)).fetchall()
    conn.close()

    out = []
    for r in rows:
        try:
            factors = json.loads(r["factor_json"] or "[]")
            gates = {g["name"]: bool(g["passed"])
                     for g in json.loads(r["gate_json"] or "[]")}
        except (ValueError, TypeError):
            continue
        out.append({
            "id": r["id"], "ts": r["ts"], "day": r["ts"][TS_DAY],
            "symbol": r["symbol"], "status": r["status"],
            "direction": r["direction"], "tq": r["trigger_quality"],
            "factors": factors, "gates": gates, "vix": r["vix"],
            "edge": r["edge_60"], "hit": r["hit_60"],
        })
    return out


# --------------------------------------------------------------------------- #
# Replay one variant
# --------------------------------------------------------------------------- #
# Gates a variant can change; every other journaled gate result is reused as-is.
RECOMPUTED_GATES = ("factors_not_contradicting",)


def replay(v: Variant, rows: list[dict]):
    """-> list of counterfactually-EMITTED rows with new grade + outcome."""
    emitted = []
    for r in rows:
        res = rescore(v, r["direction"], r["tq"], r["factors"], r["vix"])
        # fixed gates from the journal (with_the_tape, cutoff, EXPENSIVE, EM floor…)
        fixed_ok = all(ok for name, ok in r["gates"].items()
                       if name not in RECOMPUTED_GATES)
        if not fixed_ok:
            continue
        if res["net_align"] < 0.0:           # factors_not_contradicting, recomputed
            continue
        if res["grade"] is None:             # score floor
            continue
        emitted.append({**r, "new_grade": res["grade"], "new_score": res["score"]})
    return emitted


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def ladder(emits: list[dict]) -> dict:
    by = defaultdict(list)
    for e in emits:
        if e["edge"] is not None:
            by[e["new_grade"]].append(e)
    out = {}
    for g in ("A+", "A", "B"):
        xs = by.get(g, [])
        out[g] = {
            "n": len(xs),
            "edge": round(sum(x["edge"] for x in xs) / len(xs), 3) if xs else None,
            "hit": round(sum(x["hit"] for x in xs) / len(xs) * 100, 1) if xs else None,
        }
    labeled = [e for e in emits if e["edge"] is not None]
    out["ALL"] = {
        "n": len(labeled),
        "edge": round(sum(e["edge"] for e in labeled) / len(labeled), 3) if labeled else None,
        "hit": round(sum(e["hit"] for e in labeled) / len(labeled) * 100, 1) if labeled else None,
    }
    ea, eb = out["A+"]["edge"], out["A"]["edge"]
    ec = out["B"]["edge"]
    out["monotone"] = (ea is not None and eb is not None and ec is not None
                       and ea > eb > ec)
    out["top_ok"] = (ea is not None and eb is not None and ec is not None
                     and ea >= eb and ea > ec)
    return out


def run(db_path: str = "data/iv_history.db", valid_days: int = 5):
    rows = load_rows(db_path)
    days = sorted({r["day"] for r in rows})
    split = days[-valid_days] if len(days) > valid_days else days[-1]
    train = [r for r in rows if r["day"] < split]
    valid = [r for r in rows if r["day"] >= split]
    labeled = sum(1 for r in rows if r["edge"] is not None)
    print(f"rows: {len(rows)} (labeled {labeled}) | days {days[0]}..{days[-1]} "
          f"| TRAIN < {split} <= VALID ({valid_days}d)")

    results = {}
    for v in VARIANTS:
        res = {}
        for tag, sub in (("train", train), ("valid", valid)):
            L = ladder(replay(v, sub))
            res[tag] = L
        results[v.name] = res
        t, w = res["train"], res["valid"]

        def fmt(L):
            def g(k):
                d = L[k]
                return f"{k} n{d['n']:>4} e{d['edge'] if d['edge'] is not None else '  — '} h{d['hit'] if d['hit'] is not None else '—'}"
            mono = "MONO" if L["monotone"] else ("top-ok" if L["top_ok"] else "INV/flat")
            return f"{g('A+')} | {g('A')} | {g('B')} | all e{L['ALL']['edge']} [{mono}]"
        print(f"\n{v.name:11} — {v.note}")
        print(f"  train: {fmt(t)}")
        print(f"  valid: {fmt(w)}")
    return results


if __name__ == "__main__":
    run()
