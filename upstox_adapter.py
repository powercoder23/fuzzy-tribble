"""
UpstoxDhanAdapter
=================
Wraps the Upstox Python SDK and exposes the same interface that the
DiscountedPremiumScanner / strategies expect from the dhanhq client.

This means the strategy files (discount.py, momentum_strategy.py,
break_bounce_strategy.py, directional_iv_strategy.py) need NO logic
changes — just swap self.dhan with an UpstoxDhanAdapter instance.

Covered surface
---------------
  intraday_minute_data()   → HistoryV3Api.get_intra_day_candle_data
  historical_daily_data()  → HistoryV3Api.get_historical_candle_data
  option_chain()           → OptionsApi.get_put_call_option_chain
  expiry_list()            → complete.db query
  place_order()            → OrderApi.place_order

Constants (match dhanhq attribute names)
-----------------------------------------
  NSE_FNO, NSE_EQ, IDX_I
  BUY, SELL
  MARKET, SL_M
  INTRA
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone

import upstox_client
from upstox_client.rest import ApiException

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hardcoded index instrument keys (Upstox doesn't expose these via equity DB)
# ---------------------------------------------------------------------------
_INDEX_SECURITY_ID_TO_KEY = {
    "13":  "NSE_INDEX|Nifty 50",
    "14":  "NSE_INDEX|Nifty Bank",
    "2":   "NSE_INDEX|Nifty Fin Service",
    "25":  "NSE_INDEX|Nifty Midcap Select",
    "51":  "NSE_INDEX|Nifty Next 50",
}

_SEGMENT_TO_UPSTOX = {
    "IDX_I":   "NSE_INDEX",
    "NSE_EQ":  "NSE_EQ",
    "NSE_FNO": "NSE_FO",
}

_COMPLETE_DB = os.path.join(os.path.dirname(__file__), "data", "complete.db")


# ---------------------------------------------------------------------------
# Instrument mapper helpers
# ---------------------------------------------------------------------------

def _get_scrip_master_db() -> str:
    return os.path.join(os.path.dirname(__file__), "data", "api-scrip-master.db")


def _symbol_from_security_id(security_id: str) -> str | None:
    """Resolve a Dhan security_id to its trading symbol via the local scrip master."""
    sid = str(security_id)
    if sid in _INDEX_SECURITY_ID_TO_KEY:
        # Return a short name so the caller knows it's an index
        return _INDEX_SECURITY_ID_TO_KEY[sid].split("|")[1]  # e.g. "Nifty 50"
    try:
        conn = sqlite3.connect(_get_scrip_master_db())
        cur = conn.cursor()
        cur.execute(
            "SELECT SEM_TRADING_SYMBOL FROM scrip_master WHERE SEM_SMST_SECURITY_ID = ? LIMIT 1",
            (sid,),
        )
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as exc:
        logger.warning("_symbol_from_security_id(%s) failed: %s", security_id, exc)
        return None


def _underlying_key_from_security_id(security_id: str, exchange_segment: str) -> str | None:
    """
    Return the Upstox instrument_key for an underlying.
    For indices, uses a hardcoded map.
    For equities, queries complete.db.
    """
    sid = str(security_id)

    if sid in _INDEX_SECURITY_ID_TO_KEY:
        return _INDEX_SECURITY_ID_TO_KEY[sid]

    symbol = _symbol_from_security_id(sid)
    if not symbol:
        return None

    try:
        conn = sqlite3.connect(_COMPLETE_DB)
        cur = conn.cursor()
        cur.execute(
            """SELECT instrument_key FROM instruments
               WHERE trading_symbol = ? AND exchange = 'NSE'
                 AND instrument_type = 'EQ'
               LIMIT 1""",
            (symbol,),
        )
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as exc:
        logger.warning("_underlying_key_from_security_id(%s) failed: %s", security_id, exc)
        return None


def _option_instrument_key(underlying_symbol: str, expiry_date: str,
                            strike: float, option_type: str) -> str | None:
    """
    Look up the Upstox instrument_key for a specific option contract in complete.db.

    Args:
        underlying_symbol: e.g. "NIFTY", "BANKNIFTY", "RELIANCE"
        expiry_date:       "YYYY-MM-DD"
        strike:            e.g. 24000.0
        option_type:       "CE" or "PE"
    """
    try:
        # Upstox stores expiry as epoch milliseconds (integer)
        dt = datetime.strptime(expiry_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        expiry_epoch = int(dt.timestamp() * 1000)

        conn = sqlite3.connect(_COMPLETE_DB)
        cur = conn.cursor()
        cur.execute(
            """SELECT instrument_key FROM instruments
               WHERE underlying_symbol = ?
                 AND strike_price      = ?
                 AND expiry            = ?
                 AND instrument_type   = 'OPT'
                 AND trading_symbol LIKE ?
               LIMIT 1""",
            (underlying_symbol, float(strike), expiry_epoch, f"%{option_type}"),
        )
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as exc:
        logger.warning(
            "_option_instrument_key(%s %s %s %s) failed: %s",
            underlying_symbol, expiry_date, strike, option_type, exc,
        )
        return None


def _expiry_dates_for_underlying(underlying_symbol: str) -> list[str]:
    """Return sorted list of future expiry dates (YYYY-MM-DD) for an underlying."""
    try:
        now_epoch_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        conn = sqlite3.connect(_COMPLETE_DB)
        cur = conn.cursor()
        cur.execute(
            """SELECT DISTINCT expiry FROM instruments
               WHERE underlying_symbol = ?
                 AND instrument_type   = 'OPT'
                 AND expiry            > ?
               ORDER BY expiry""",
            (underlying_symbol, now_epoch_ms),
        )
        rows = cur.fetchall()
        conn.close()
        result = []
        for (ep,) in rows:
            try:
                dt = datetime.fromtimestamp(ep / 1000, tz=timezone.utc)
                result.append(dt.strftime("%Y-%m-%d"))
            except Exception:
                pass
        return sorted(set(result))
    except Exception as exc:
        logger.warning("_expiry_dates_for_underlying(%s) failed: %s", underlying_symbol, exc)
        return []


# ---------------------------------------------------------------------------
# Response transformers
# ---------------------------------------------------------------------------

def _candles_to_dhan_columnar(candles: list) -> dict:
    """
    Convert Upstox candle list [[ts, o, h, l, c, vol, oi], ...]
    to Dhan-style columnar dict {"timestamp": [...], "open": [...], ...}.
    """
    if not candles:
        return {}
    ts, opens, highs, lows, closes, volumes = [], [], [], [], [], []
    for row in candles:
        ts.append(row[0])        # ISO string e.g. "2025-05-26T09:15:00+05:30"
        opens.append(row[1])
        highs.append(row[2])
        lows.append(row[3])
        closes.append(row[4])
        volumes.append(row[5])
    return {
        "timestamp": ts,
        "open":      opens,
        "high":      highs,
        "low":       lows,
        "close":     closes,
        "volume":    volumes,
    }


def _option_chain_to_dhan_format(strikes_data, underlying_symbol: str) -> dict:
    """
    Transform the Upstox get_put_call_option_chain response into the
    Dhan-compatible option chain dict that the strategies iterate over.

    Dhan format (what strategies expect):
        {
          "status": "success",
          "data": {
            "underlying_price": float,
            "oc": {
              "<strike>": {
                "ce": {
                  "ltp": float, "bid": float, "ask": float,
                  "oi": float, "volume": float,
                  "implied_volatility": float,
                  "option_security_id": <Upstox instrument_key>,
                },
                "pe": { ... }
              },
              ...
            }
          }
        }
    """
    oc = {}
    spot_price = None

    for strike_obj in (strikes_data or []):
        try:
            strike = strike_obj.strike_price
            if strike is None:
                continue
            strike_key = str(int(strike)) if strike == int(strike) else str(strike)

            if spot_price is None and strike_obj.underlying_spot_price:
                spot_price = strike_obj.underlying_spot_price

            def _build_side(opt):
                if opt is None:
                    return {}
                md = opt.market_data or {}
                greeks = opt.option_greeks or {}
                # market_data is a Pydantic/dataclass; access as attributes
                ltp    = getattr(md, "ltp", None) or 0.0
                bid    = getattr(md, "bid_price", None) or 0.0
                ask    = getattr(md, "ask_price", None) or 0.0
                oi     = getattr(md, "oi", None) or 0.0
                volume = getattr(md, "volume", None) or 0.0
                iv     = getattr(greeks, "iv", None) or 0.0
                inst_key = getattr(opt, "instrument_key", None) or ""
                return {
                    "ltp":               float(ltp),
                    "bid":               float(bid),
                    "ask":               float(ask),
                    "oi":                float(oi),
                    "volume":            float(volume),
                    "implied_volatility": float(iv),
                    "option_security_id": inst_key,  # used by place_order
                }

            oc[strike_key] = {
                "ce": _build_side(strike_obj.call_options),
                "pe": _build_side(strike_obj.put_options),
            }

        except Exception as exc:
            logger.debug("Skipping strike due to parse error: %s", exc)

    return {
        "status": "success",
        "data": {
            "last_price": spot_price or 0.0,   # matches chain_data.get("last_price") in strategies
            "oc": oc,
        },
    }


# ---------------------------------------------------------------------------
# Main adapter class
# ---------------------------------------------------------------------------

class UpstoxDhanAdapter:
    """
    Drop-in replacement for the dhanhq client.
    Instantiate once per session with a valid Upstox access token.
    """

    # Constants matching dhanhq attribute names used in the strategies
    NSE_FNO = "NSE_FO"
    NSE_EQ  = "NSE_EQ"
    IDX_I   = "NSE_INDEX"
    BUY     = "BUY"
    SELL    = "SELL"
    MARKET  = "MARKET"
    SL_M    = "SL-M"
    INTRA   = "I"       # MIS / intraday in Upstox

    def __init__(self, access_token: str):
        cfg = upstox_client.Configuration()
        cfg.access_token = access_token
        client = upstox_client.ApiClient(cfg)

        self._history_api = upstox_client.HistoryV3Api(client)
        self._options_api = upstox_client.OptionsApi(client)
        self._order_api   = upstox_client.OrderApi(client)

    # ------------------------------------------------------------------
    # Candle data
    # ------------------------------------------------------------------

    def intraday_minute_data(self, security_id, exchange_segment,
                             instrument_type, from_date, to_date,
                             interval=1) -> dict:
        """Fetch intraday OHLCV; returns Dhan-compatible columnar dict."""
        inst_key = _underlying_key_from_security_id(security_id, exchange_segment)
        if not inst_key:
            logger.warning("intraday_minute_data: no instrument_key for security_id=%s", security_id)
            return {"status": "failure", "remarks": "instrument_key not found"}
        try:
            resp = self._history_api.get_intra_day_candle_data(
                instrument_key=inst_key,
                unit="minute",
                interval=int(interval),
            )
            candles = resp.data.candles if resp and resp.data else []
            return {
                "status": "success",
                "data":   _candles_to_dhan_columnar(candles),
            }
        except ApiException as exc:
            logger.error("intraday_minute_data ApiException: %s", exc)
            return {"status": "failure", "remarks": str(exc)}

    def historical_daily_data(self, security_id, exchange_segment,
                              instrument_type, from_date, to_date,
                              oi=False) -> dict:
        """Fetch daily OHLCV; returns Dhan-compatible list-of-dicts payload."""
        inst_key = _underlying_key_from_security_id(security_id, exchange_segment)
        if not inst_key:
            logger.warning("historical_daily_data: no instrument_key for security_id=%s", security_id)
            return {"status": "failure", "remarks": "instrument_key not found"}
        try:
            resp = self._history_api.get_historical_candle_data(
                instrument_key=inst_key,
                unit="day",
                interval=1,
                to_date=to_date,
                from_date=from_date,
            )
            candles = resp.data.candles if resp and resp.data else []
            col = _candles_to_dhan_columnar(candles)
            # Convert to list-of-dicts (Dhan historical_daily_data format)
            rows = [
                {
                    "timestamp": col["timestamp"][i],
                    "open":      col["open"][i],
                    "high":      col["high"][i],
                    "low":       col["low"][i],
                    "close":     col["close"][i],
                    "volume":    col["volume"][i],
                }
                for i in range(len(col.get("timestamp", [])))
            ]
            return {"status": "success", "data": rows}
        except ApiException as exc:
            logger.error("historical_daily_data ApiException: %s", exc)
            return {"status": "failure", "remarks": str(exc)}

    # ------------------------------------------------------------------
    # Option chain
    # ------------------------------------------------------------------

    def option_chain(self, under_security_id, under_exchange_segment,
                     expiry, **kwargs) -> dict:
        """Fetch option chain; returns Dhan-compatible oc dict."""
        underlying_key = _underlying_key_from_security_id(
            under_security_id, under_exchange_segment)
        if not underlying_key:
            logger.warning("option_chain: no instrument_key for security_id=%s", under_security_id)
            return {"status": "failure", "remarks": "instrument_key not found"}

        # Resolve underlying_symbol for fallback instrument key lookup
        sid = str(under_security_id)
        if sid in _INDEX_SECURITY_ID_TO_KEY:
            underlying_symbol = underlying_key.split("|")[1].replace(" ", "")
            # Normalise: "Nifty 50" → "NIFTY", "Nifty Bank" → "BANKNIFTY"
            _symbol_norm = {
                "Nifty50": "NIFTY", "NiftyBank": "BANKNIFTY",
                "NiftyFinService": "FINNIFTY",
            }
            underlying_symbol = _symbol_norm.get(underlying_symbol, underlying_symbol.upper())
        else:
            underlying_symbol = _symbol_from_security_id(sid) or ""

        try:
            resp = self._options_api.get_put_call_option_chain(
                instrument_key=underlying_key,
                expiry_date=expiry,
            )
            strikes = resp.data if resp and resp.data else []
            result = _option_chain_to_dhan_format(strikes, underlying_symbol)

            # If Upstox didn't populate instrument_keys in the response,
            # fall back to local complete.db lookup.
            oc = result["data"]["oc"]
            for strike_key, strike_val in oc.items():
                for side_key, side_code in [("ce", "CE"), ("pe", "PE")]:
                    side = strike_val.get(side_key, {})
                    if not side.get("option_security_id"):
                        ikey = _option_instrument_key(
                            underlying_symbol, expiry,
                            float(strike_key), side_code,
                        )
                        if ikey:
                            side["option_security_id"] = ikey

            return result

        except ApiException as exc:
            logger.error("option_chain ApiException: %s", exc)
            return {"status": "failure", "remarks": str(exc)}

    def expiry_list(self, under_security_id, under_exchange_segment, **kwargs) -> dict:
        """Return available expiry dates; returns Dhan-compatible dict."""
        sid = str(under_security_id)
        if sid in _INDEX_SECURITY_ID_TO_KEY:
            raw_name = _INDEX_SECURITY_ID_TO_KEY[sid].split("|")[1]
            _norm = {"Nifty 50": "NIFTY", "Nifty Bank": "BANKNIFTY",
                     "Nifty Fin Service": "FINNIFTY"}
            underlying_symbol = _norm.get(raw_name, raw_name.upper().replace(" ", ""))
        else:
            underlying_symbol = _symbol_from_security_id(sid) or ""

        if not underlying_symbol:
            return {"status": "failure", "remarks": "symbol not found"}

        expiries = _expiry_dates_for_underlying(underlying_symbol)
        return {"status": "success", "data": expiries}

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def place_order(self, security_id, exchange_segment,
                    transaction_type, quantity, order_type,
                    product_type, price, trigger_price=0.0,
                    tag="") -> dict:
        """
        Place a single order.

        When used via the Upstox adapter, `security_id` is expected to be
        an Upstox instrument_key (e.g. "NSE_FO|12345") — populated by
        option_chain() into strike_data["option_security_id"].
        """
        instrument_token = str(security_id)

        # Map product_type constant → Upstox product string
        product_map = {
            self.INTRA: "I",    # MIS
            "INTRA":    "I",
            "I":        "I",
            "CNC":      "D",
            "MARGIN":   "M",
        }
        product = product_map.get(str(product_type), "I")

        body = upstox_client.PlaceOrderRequest(
            quantity          = int(quantity),
            product           = product,
            validity          = "DAY",
            price             = float(price),
            instrument_token  = instrument_token,
            order_type        = str(order_type),       # "MARKET", "SL-M", "LIMIT", "SL"
            transaction_type  = str(transaction_type), # "BUY" or "SELL"
            disclosed_quantity = 0,
            trigger_price     = float(trigger_price) if trigger_price else 0.0,
            is_amo            = False,
            tag               = tag or None,
        )

        try:
            resp = self._order_api.place_order(body, api_version="2.0")
            order_data = resp.data if resp else {}
            order_id = getattr(order_data, "order_id", None) or ""
            return {"status": "success", "orderId": order_id, "data": order_data}
        except ApiException as exc:
            logger.error("place_order ApiException [%s %s %s qty=%s]: %s",
                         transaction_type, order_type, instrument_token, quantity, exc)
            return {"status": "failure", "remarks": str(exc)}
