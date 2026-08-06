# REDUCE trade frequency to reduce friction losses
INTRADAY["max_signals_per_day"] = 3  # WAS 0 (unlimited)
INTRADAY["max_per_symbol_per_day"] = 1  # Keep as is

# INCREASE quality threshold
MIN_SCORE = 80  # WAS 55 - higher quality trades only

# REDUCE risk cap slightly (optional)
INTRADAY["max_risk_rupees"] = 1500  # Keep same