# -*- coding: utf-8 -*-
"""
pre_market_gate_config.py — thresholds for the pre-market entry gate.

All values are overridable via environment variables so you can tune per
deployment without touching code.

Gate map
────────
Gate 1 │ IVR ≤ MAX_IVR             option cheap on 52-week IV history
Gate 2 │ IV/HV ≤ MAX_IV_HV_RATIO   IV not expensive vs realised volatility
Gate 3 │ OTM% ≤ MAX_OTM_PCT        strike reachable within session
Gate 4 │ PCR direction check        CE → PCR ≥ MIN_PCR_CE (not call-overbought)
        │                           PE → PCR ≤ MAX_PCR_PE (no squeeze risk)
Gate 5 │ open positions < MAX_SIM   hard simultaneous cap
"""

import os

# ── Mode ─────────────────────────────────────────────────────────────────── #
# off  → always allow, zero lookups (parity with entry_gate default)
# soft → evaluate and log, but never block
# hard → block candidates that fail any gate
GATE_MODE = os.getenv("PMG_GATE_MODE", "hard").strip().lower()

# ── Gate 1: IV Rank cap ──────────────────────────────────────────────────── #
MAX_IVR = float(os.getenv("PMG_MAX_IVR", "35"))

# If iv_rank is absent from the signal, skip this gate rather than block.
SKIP_IVR_IF_MISSING = os.getenv("PMG_SKIP_IVR_IF_MISSING", "true").strip().lower() == "true"

# ── Gate 2: IV / HV ratio ────────────────────────────────────────────────── #
# IV above realised vol → sellers' edge, not buyers'. Block when ratio > 1.0.
MAX_IV_HV_RATIO = float(os.getenv("PMG_MAX_IV_HV_RATIO", "1.0"))

# If IV or HV is absent from the signal, skip this gate rather than block.
SKIP_IV_HV_IF_MISSING = os.getenv("PMG_SKIP_IV_HV_IF_MISSING", "true").strip().lower() == "true"

# ── Gate 3: OTM% cap ─────────────────────────────────────────────────────── #
# A 5.4% OTM strike needs a 5.4% spot move just to get ATM. Cap at 2.5%.
MAX_OTM_PCT = float(os.getenv("PMG_MAX_OTM_PCT", "2.5"))

# ── Gate 4: PCR sanity ───────────────────────────────────────────────────── #
# CE entry: PCR ≥ 0.55 — enough put OI relative to calls (market not overbought on calls)
MIN_PCR_CE = float(os.getenv("PMG_MIN_PCR_CE", "0.55"))
# PE entry: PCR ≤ 1.50 — not extreme put pileup that risks a short squeeze
MAX_PCR_PE = float(os.getenv("PMG_MAX_PCR_PE", "1.50"))
# If OI data is missing from iv_history, pass the gate rather than block.
PCR_FAIL_OPEN = os.getenv("PMG_PCR_FAIL_OPEN", "true").strip().lower() == "true"

# ── Gate 5: simultaneous position cap ────────────────────────────────────── #
MAX_SIMULTANEOUS = int(os.getenv("PMG_MAX_SIMULTANEOUS", "2"))
