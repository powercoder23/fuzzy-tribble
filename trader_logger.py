"""
trader_logger.py
─────────────────────────────────────────────────────────────────────────────
Structured EOD-reviewable logger for the Discounted Premium Scanner.

Design principle: capture DECISIONS, not data.

What this logs (option buyer's perspective):
  • Symbol context once per symbol: regime, IV state, HV state, DTE
  • Every quality gate rejection with the value and the threshold
  • Every fully-scored candidate (accepted, capped, or gated)
  • Data quality flags (zero OI, IV mismatch, missing greeks, etc.)
  • Per-scan summary: gate counts, top false-negatives, accepted alerts

Output:
  • logs/scan_YYYY-MM-DD.jsonl       Machine-readable, one event per line
  • logs/scan_YYYY-MM-DD_summary.txt Human-readable EOD summary

EOD review flow:
  1. Read the summary.txt for the bird's-eye view
  2. Identify suspect gate counts (one gate killing 80% of candidates = miscalibrated)
  3. Check top_5_rejected_by_score in the summary — these are potential false negatives
  4. grep the jsonl for specific symbols to trace the full decision
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _safe_round(value, digits=4):
    """Round if numeric, pass through otherwise. Handles None and NaN."""
    if value is None:
        return None
    try:
        f = float(value)
        if f != f:  # NaN check
            return None
        return round(f, digits)
    except (TypeError, ValueError):
        return value


class TraderLogger:
    """
    Structured logger scoped to one scan cycle.

    Instantiate at the start of each scan; call log methods at decision points;
    call log_scan_summary() at the end.
    """

    LOG_DIR = Path("logs")

    def __init__(self, scan_type: str = "eod"):
        self.scan_type = scan_type
        self.scan_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.date_tag = datetime.now().strftime("%Y-%m-%d")
        self.LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.LOG_DIR / f"scan_{self.date_tag}.jsonl"
        self.summary_path = self.LOG_DIR / f"scan_{self.date_tag}_summary.txt"

        # Per-scan counters and collectors
        self.gate_counts: dict[str, int] = {}
        self.rejections: list[dict] = []   # gated candidates with full payload
        self.accepted: list[dict] = []     # alerts that fired
        self.data_quality_issues: list[dict] = []
        self.symbols_scanned: set[str] = set()

    # ── core writer ──────────────────────────────────────────────────────

    def _write(self, event: dict) -> None:
        event["scan_id"] = self.scan_id
        event["scan_type"] = self.scan_type
        event["ts"] = datetime.now().isoformat(timespec="seconds")
        try:
            with open(self.jsonl_path, "a") as f:
                f.write(json.dumps(event, default=str) + "\n")
        except Exception:
            logger.exception("TraderLogger failed to write event")

    # ── scan lifecycle ───────────────────────────────────────────────────

    def log_scan_start(self, vix: Optional[float] = None,
                       capital: Optional[float] = None,
                       universe_size: Optional[int] = None,
                       notes: Optional[str] = None) -> None:
        self._write({
            "event": "scan_start",
            "vix": _safe_round(vix, 2),
            "capital": _safe_round(capital, 0),
            "universe_size": universe_size,
            "notes": notes,
        })

    def log_scan_abort(self, reason: str, **details) -> None:
        """Whole-scan abort (e.g., VIX too high, market closed)."""
        self._write({
            "event": "scan_abort",
            "reason": reason,
            "details": details,
        })

    # ── per-symbol context ───────────────────────────────────────────────

    def log_symbol_context(self, symbol: str, **context) -> None:
        """
        Call once per symbol BEFORE scoring its strikes.
        This is what an option buyer needs to see at EOD to understand
        why a symbol produced (or did not produce) signals.

        Recommended fields:
          spot, dte, expiry, atm_iv, iv_rank, iv_percentile, iv_regime,
          weighted_hv, iv_vs_hv_edge_pct, iv_history_samples,
          ema_trend, adx, pcr, sentiment_bias
        """
        self.symbols_scanned.add(symbol)
        payload = {"event": "symbol_context", "symbol": symbol}
        for k, v in context.items():
            payload[k] = _safe_round(v, 2) if isinstance(v, (int, float)) else v
        self._write(payload)

    # ── gate rejections (the most important log type) ────────────────────

    def log_gate_reject(self, symbol: str, gate: str,
                        value: Any, threshold: Any,
                        strike: Optional[float] = None,
                        opt_type: Optional[str] = None,
                        extra: Optional[dict] = None) -> None:
        """
        Log a quality gate rejection.

        Args:
            symbol: Symbol (NIFTY, RELIANCE, etc.)
            gate: Gate name (dte, premium, spread, delta, conviction, rr, ...)
            value: The actual measured value that failed
            threshold: The threshold it failed against
            strike: Strike if per-strike gate
            opt_type: 'CALL' / 'PUT' if per-strike gate
            extra: Anything else worth recording

        This is the highest-signal log type — at EOD, the distribution of
        gate rejections tells you which filter is miscalibrated.
        """
        self.gate_counts[gate] = self.gate_counts.get(gate, 0) + 1
        payload = {
            "event": "gate_reject",
            "symbol": symbol,
            "gate": gate,
            "value": _safe_round(value, 4),
            "threshold": threshold,
            "strike": _safe_round(strike, 2),
            "type": opt_type,
        }
        if extra:
            payload["extra"] = extra
        self._write(payload)

    # ── candidate (fully-scored option) ──────────────────────────────────

    def log_candidate(self, candidate: dict, decision: str) -> None:
        """
        Log a fully-scored candidate with its decision.

        decision: 'accepted' | 'capped' | 'gated'
          - accepted: passed all gates, going to alert
          - capped: passed gates but hit MAX_ALERTS_PER_SCAN cap
          - gated: rejected by a quality gate (also logged separately)

        Only the option-buyer-relevant fields are extracted — not the full row.
        """
        compact = {
            "event": "candidate",
            "decision": decision,
            "symbol": candidate.get("symbol"),
            "strike": _safe_round(candidate.get("strike"), 2),
            "type": candidate.get("type"),
            "expiry": str(candidate.get("expiry", "")),
            "dte": candidate.get("dte"),
            "score": _safe_round(candidate.get("score"), 2),
            "score_components": candidate.get("component_scores")
                                or candidate.get("score_breakdown"),
            "iv": _safe_round(candidate.get("iv"), 2),
            "iv_rank": _safe_round(candidate.get("iv_rank"), 2),
            "iv_percentile": _safe_round(candidate.get("iv_percentile"), 2),
            "iv_regime": candidate.get("iv_regime"),
            "delta": _safe_round(candidate.get("delta"), 3),
            "vega": _safe_round(candidate.get("vega"), 3),
            "theta": _safe_round(candidate.get("theta"), 3),
            "oi": candidate.get("oi"),
            "volume": candidate.get("volume"),
            "spread_pct": _safe_round(candidate.get("spread"), 4),
            "entry": _safe_round(candidate.get("entry"), 2),
            "stop_loss": _safe_round(candidate.get("stop_loss"), 2),
            "target": _safe_round(candidate.get("target"), 2),
            "rr": _safe_round(candidate.get("risk_reward"), 2),
            "conviction": candidate.get("conviction"),
            "trade_type": candidate.get("trade_type"),
            "buildup_type": candidate.get("buildup_type"),
            "triggers_fired": [
                k for k, v in (candidate.get("triggers") or {}).get("flags", {}).items() if v
            ],
            "trigger_strength": _safe_round(
                (candidate.get("triggers") or {}).get("strength_score"), 3
            ),
            "trigger_direction": (candidate.get("triggers") or {}).get("direction"),
        }
        if decision == "accepted":
            self.accepted.append(compact)
        elif decision == "gated":
            self.rejections.append(compact)
        self._write(compact)

    # ── data quality flags ───────────────────────────────────────────────

    def log_data_quality(self, symbol: str, issue: str, **details) -> None:
        """
        Flag suspicious data — these often explain weird scan behavior.

        Common issues:
          - 'iv_mismatch': CE IV vs PE IV at ATM differ by > 5%
          - 'zero_oi': strike has zero open interest
          - 'stale_quote': last_trade_time > 5 min ago
          - 'missing_greeks': delta or vega is None
          - 'spot_drift': spot moved > 1% between chain fetch and scoring
          - 'iv_history_short': fewer than 30 samples for iv_rank calc
        """
        record = {
            "event": "data_quality",
            "symbol": symbol,
            "issue": issue,
            "details": details,
        }
        self.data_quality_issues.append(record)
        self._write(record)

    # ── EOD summary ──────────────────────────────────────────────────────

    def log_scan_summary(self, total_alerts: int) -> None:
        """Write structured + human summary at scan end."""
        top_rejected = sorted(
            self.rejections,
            key=lambda x: x.get("score") or 0,
            reverse=True,
        )[:5]

        summary_event = {
            "event": "scan_summary",
            "symbols_scanned": len(self.symbols_scanned),
            "total_alerts": total_alerts,
            "gate_counts": dict(sorted(self.gate_counts.items(), key=lambda x: -x[1])),
            "data_quality_issue_count": len(self.data_quality_issues),
            "top_5_rejected_by_score": top_rejected,
            "accepted_count": len(self.accepted),
        }
        self._write(summary_event)
        self._write_human_summary(summary_event)

    def _write_human_summary(self, summary_event: dict) -> None:
        try:
            with open(self.summary_path, "a") as f:
                f.write(f"\n{'=' * 70}\n")
                f.write(f"SCAN {self.scan_id} [{self.scan_type}]  "
                        f"{datetime.now().strftime('%H:%M:%S')}\n")
                f.write(f"{'=' * 70}\n")
                f.write(f"Symbols scanned: {summary_event['symbols_scanned']}\n")
                f.write(f"Alerts fired:    {summary_event['total_alerts']}\n")
                f.write(f"Data quality issues: {summary_event['data_quality_issue_count']}\n\n")

                f.write("GATE REJECTIONS (ranked by count):\n")
                if not summary_event["gate_counts"]:
                    f.write("  (none)\n")
                for gate, count in summary_event["gate_counts"].items():
                    bar = "█" * min(40, count)
                    f.write(f"  {gate:22s} {count:4d}  {bar}\n")

                f.write("\nTOP 5 FALSE-NEGATIVE CANDIDATES "
                        "(high score, gated — review these):\n")
                if not summary_event["top_5_rejected_by_score"]:
                    f.write("  (none)\n")
                for r in summary_event["top_5_rejected_by_score"]:
                    sym = r.get("symbol")
                    strike = r.get("strike")
                    opt = (r.get("type") or "")[:1]
                    score = r.get("score")
                    conv = r.get("conviction") or "—"
                    iv_rank = r.get("iv_rank")
                    f.write(f"  {sym:10s} {strike} {opt}  "
                            f"score={score}  conv={conv}  iv_rank={iv_rank}\n")

                f.write("\nACCEPTED ALERTS:\n")
                if not self.accepted:
                    f.write("  (none)\n")
                for a in self.accepted:
                    sym = a.get("symbol")
                    strike = a.get("strike")
                    opt = (a.get("type") or "")[:1]
                    score = a.get("score")
                    conv = a.get("conviction") or "—"
                    rr = a.get("rr")
                    triggers = ",".join(a.get("triggers_fired") or []) or "none"
                    f.write(f"  {sym:10s} {strike} {opt}  "
                            f"score={score}  conv={conv}  rr={rr}  "
                            f"triggers=[{triggers}]\n")
                f.write("\n")
        except Exception:
            logger.exception("Failed to write human summary")


# ─── EOD analysis helper ─────────────────────────────────────────────────

def load_scan_events(date_str: str, log_dir: str = "logs") -> list[dict]:
    """
    Load all events from a day's scan log for EOD review.

    Usage:
        from trader_logger import load_scan_events
        events = load_scan_events("2026-05-14")
        gate_rejects = [e for e in events if e['event'] == 'gate_reject']
    """
    path = Path(log_dir) / f"scan_{date_str}.jsonl"
    if not path.exists():
        return []
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events
