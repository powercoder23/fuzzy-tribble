# -*- coding: utf-8 -*-
"""market_context.feed — WebSocket ingest.

Only the market-context service imports this package. Strategy containers
never open a socket: they read market_context.get().
"""
