# -*- coding: utf-8 -*-
"""Convex V2 — Conviction Engine (P0 strangler scaffold).

One decision funnel: Regime -> Factors -> Trigger -> Gates -> Conviction ->
Decision. See V2_BLUEPRINT.md. This package reads the existing *_history
tables written by V1 services; it makes ZERO broker calls.
"""
