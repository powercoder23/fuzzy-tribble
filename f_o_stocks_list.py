from datetime import datetime, timedelta
import gzip
import logging
from io import BytesIO
from pathlib import Path

import pandas as pd
import requests


logger = logging.getLogger(__name__)

FNO_CACHE_DIR = Path("data/fno_cache")
_FO_SYMBOL_CACHE = {}


def _resolve_trading_day(day_offset=1):
    target_day = datetime.today() - timedelta(days=day_offset)
    if target_day.weekday() == 5:
        target_day -= timedelta(days=1)
    elif target_day.weekday() == 6:
        target_day -= timedelta(days=2)
    return target_day


def _read_symbols_from_contract_bytes(raw_bytes):
    with gzip.open(BytesIO(raw_bytes), "rt") as handle:
        df = pd.read_csv(handle)
    df["FinInstrmNm_clean"] = df["FinInstrmNm"].astype(str).str.strip().str.upper()
    df_stock_futures = df[df["FinInstrmNm_clean"] == "FUTSTK"]
    final_df = df_stock_futures[["TckrSymb", "FinInstrmId", "XpryDt", "StockNm", "MinLot"]]
    return sorted(final_df["TckrSymb"].dropna().astype(str).unique())


def get_stock_futures(day=1) -> list:
    """
    Download and cache the NSE stock-futures universe from the daily F&O contract file.

    The result is cached in-memory for the current process and also persisted in
    ``data/fno_cache`` so repeated warmup runs do not keep downloading the same
    daily NSE file.
    """
    target_day = _resolve_trading_day(day)
    cache_key = target_day.date().isoformat()
    if cache_key in _FO_SYMBOL_CACHE:
        logger.info("Cache hit for NSE FO contract file: %s", cache_key)
        return list(_FO_SYMBOL_CACHE[cache_key])

    FNO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    date_str = target_day.strftime("%d%m%Y")
    symbols_cache_file = FNO_CACHE_DIR / f"nse_fo_symbols_{date_str}.csv"
    contract_cache_file = FNO_CACHE_DIR / f"NSE_FO_contract_{date_str}.csv.gz"

    if symbols_cache_file.exists():
        symbols = (
            pd.read_csv(symbols_cache_file)["symbol"]
            .dropna()
            .astype(str)
            .sort_values()
            .tolist()
        )
        _FO_SYMBOL_CACHE[cache_key] = symbols
        logger.info("Loaded NSE FO symbols from disk cache for %s", cache_key)
        return list(symbols)

    csv_url = f"https://archives.nseindia.com/content/fo/NSE_FO_contract_{date_str}.csv.gz"
    logger.info("Fetching NSE FO contract file from %s", csv_url)
    try:
        response = requests.get(csv_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        response.raise_for_status()
        contract_cache_file.write_bytes(response.content)
        symbols = _read_symbols_from_contract_bytes(response.content)
        pd.DataFrame({"symbol": symbols}).to_csv(symbols_cache_file, index=False)
        _FO_SYMBOL_CACHE[cache_key] = symbols
        return list(symbols)
    except Exception as exc:
        logger.warning("Unable to load NSE FO contract file for %s: %s", cache_key, exc)
        if contract_cache_file.exists():
            symbols = _read_symbols_from_contract_bytes(contract_cache_file.read_bytes())
            _FO_SYMBOL_CACHE[cache_key] = symbols
            logger.info("Recovered NSE FO symbols from raw contract cache for %s", cache_key)
            return list(symbols)
        return get_stock_futures(day=day + 1)
