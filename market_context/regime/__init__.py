# -*- coding: utf-8 -*-
"""market_context.regime — six independent axis classifiers.

The engine DESCRIBES the market. It does not decide trades: there is no bias,
no size multiplier, no veto and no exit verdict anywhere in this package, and
a test asserts their absence. Each strategy consumes the axes it needs and
makes its own call.
"""
