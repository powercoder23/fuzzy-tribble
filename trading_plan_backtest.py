from __future__ import annotations

import argparse
import csv
import math
import os
import random
import time
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    from dhanhq import DhanContext, dhanhq
except ImportError:
    DhanContext = None
    dhanhq = None


TIMEFRAME_MINUTES = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
    "1w": 10080,
    "1M": 43200,
}

DEFAULT_NIFTY_SECURITY_ID = "13"
DEFAULT_NIFTY_EXCHANGE_SEGMENT = "IDX_I"
DEFAULT_NIFTY_INSTRUMENT_TYPE = "INDEX"
DEFAULT_STOCK_EXCHANGE_SEGMENT = "NSE_EQ"
DEFAULT_STOCK_INSTRUMENT_TYPE = "EQUITY"
DHAN_SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"


@dataclass
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class SwingPoint:
    index: int
    timestamp: datetime
    price: float
    kind: str


@dataclass
class StructureEvent:
    index: int
    timestamp: datetime
    direction: str
    level: float
    source_index: int
    kind: str


@dataclass
class Zone:
    kind: str
    direction: str
    timeframe: str
    created_index: int
    created_at: datetime
    low: float
    high: float
    meta: Dict[str, float | str] = field(default_factory=dict)

    def contains_price(self, price: float) -> bool:
        return self.low <= price <= self.high

    def touched_by(self, candle: Candle) -> bool:
        return candle.low <= self.high and candle.high >= self.low


@dataclass
class Trade:
    direction: str
    entry_time: datetime
    entry_price: float
    stop_price: float
    target_price: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    setup_zone: Optional[Zone] = None

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry_price - self.stop_price)

    @property
    def pnl_r(self) -> Optional[float]:
        if self.exit_price is None:
            return None
        signed = (self.exit_price - self.entry_price) if self.direction == "bullish" else (self.entry_price - self.exit_price)
        if self.risk_per_unit == 0:
            return 0.0
        return signed / self.risk_per_unit


@dataclass
class BacktestConfig:
    base_timeframe: str = "15m"
    bias_timeframes: Tuple[str, ...] = ("1M", "1w", "1d")
    poi_timeframe: str = "4h"
    confirmation_timeframes: Tuple[str, ...] = ("1h", "15m")
    confirmation_mode: str = "bos_or_choch"
    confirmation_lookback_bars: int = 3
    swing_lookback: int = 2
    structure_lookback: int = 8
    order_block_search: int = 6
    zone_max_age_bars: int = 60
    allowed_zone_kinds: Optional[Tuple[str, ...]] = None
    stop_buffer: float = 0.0005
    risk_reward: float = 2.0
    starting_equity: float = 10_000.0
    risk_per_trade: float = 0.01
    session_start: Optional[str] = None
    session_end: Optional[str] = None
    force_intraday_exit: bool = False
    square_off_time: Optional[str] = None


@dataclass
class DhanFetchConfig:
    client_id: str
    access_token: str
    security_id: str
    exchange_segment: str
    instrument_type: str
    interval: int = 15
    oi: bool = False
    batch_days: int = 90
    batch_pause_seconds: float = 0.8
    max_retries: int = 3
    retry_pause_seconds: float = 2.0


@dataclass
class BacktestResult:
    trades: List[Trade]
    metrics: Dict[str, float]
    diagnostics: Dict[str, float]


def build_best_nifty_strategy_config() -> BacktestConfig:
    return BacktestConfig(
        base_timeframe="15m",
        poi_timeframe="1h",
        confirmation_timeframes=("1h", "15m"),
        confirmation_mode="bos_or_choch",
        risk_reward=2.0,
        session_start="09:15",
        session_end="10:45",
    )


def build_best_nifty_combo_base_config() -> BacktestConfig:
    return BacktestConfig(
        base_timeframe="15m",
        bias_timeframes=("1w", "1d"),
        poi_timeframe="1h",
        confirmation_timeframes=("1h", "15m"),
        confirmation_mode="bos_or_choch",
        confirmation_lookback_bars=3,
        allowed_zone_kinds=("fvg",),
        risk_reward=2.0,
        session_start="09:15",
        session_end="10:45",
        force_intraday_exit=True,
        square_off_time="15:20",
    )


def build_best_nifty_combo_strict_config() -> BacktestConfig:
    return replace(build_best_nifty_combo_base_config(), confirmation_lookback_bars=2)


def build_nifty_intraday_production_config() -> BacktestConfig:
    return BacktestConfig(
        base_timeframe="15m",
        bias_timeframes=("1w", "1d"),
        poi_timeframe="1h",
        confirmation_timeframes=("1h", "15m"),
        confirmation_mode="bos_or_choch",
        confirmation_lookback_bars=2,
        allowed_zone_kinds=("fvg",),
        risk_reward=2.0,
        session_start="09:15",
        session_end="12:00",
        force_intraday_exit=True,
        square_off_time="15:20",
    )


def build_fast_nifty_5m_config() -> BacktestConfig:
    return BacktestConfig(
        base_timeframe="5m",
        bias_timeframes=("1d", "4h"),
        poi_timeframe="15m",
        confirmation_timeframes=("15m", "5m"),
        confirmation_mode="bos_or_choch",
        confirmation_lookback_bars=2,
        allowed_zone_kinds=("fvg",),
        risk_reward=2.0,
        session_start="09:15",
        session_end="10:45",
        force_intraday_exit=True,
        square_off_time="15:20",
    )


def build_fast_nifty_1m_config() -> BacktestConfig:
    return BacktestConfig(
        base_timeframe="1m",
        bias_timeframes=("1d", "4h"),
        poi_timeframe="15m",
        confirmation_timeframes=("15m", "5m"),
        confirmation_mode="bos_or_choch",
        confirmation_lookback_bars=2,
        allowed_zone_kinds=("fvg",),
        risk_reward=2.0,
        session_start="09:15",
        session_end="10:45",
        force_intraday_exit=True,
        square_off_time="15:20",
    )


def build_best_stock_strategy_config() -> BacktestConfig:
    return BacktestConfig(
        base_timeframe="15m",
        poi_timeframe="1h",
        confirmation_timeframes=("1h", "15m"),
        confirmation_mode="bos_or_choch",
        risk_reward=2.0,
        session_start="09:15",
        session_end="10:45",
    )


def parse_timestamp(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")


def load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_candles(csv_path: Path) -> List[Candle]:
    candles: List[Candle] = []
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"timestamp", "open", "high", "low", "close"}
        if not required.issubset({name.strip().lower() for name in reader.fieldnames or []}):
            raise ValueError("CSV must contain timestamp, open, high, low, close columns.")

        for row in reader:
            normalized = {k.strip().lower(): v for k, v in row.items()}
            candles.append(
                Candle(
                    timestamp=parse_timestamp(normalized["timestamp"]),
                    open=float(normalized["open"]),
                    high=float(normalized["high"]),
                    low=float(normalized["low"]),
                    close=float(normalized["close"]),
                    volume=float(normalized.get("volume", 0.0) or 0.0),
                )
            )
    candles.sort(key=lambda candle: candle.timestamp)
    return candles


def infer_candle_interval_minutes(candles: List[Candle]) -> Optional[int]:
    if len(candles) < 2:
        return None
    deltas: Dict[int, int] = defaultdict(int)
    previous = candles[0].timestamp
    for candle in candles[1:]:
        delta_minutes = int((candle.timestamp - previous).total_seconds() // 60)
        previous = candle.timestamp
        if delta_minutes > 0:
            deltas[delta_minutes] += 1
    if not deltas:
        return None
    return max(deltas.items(), key=lambda item: item[1])[0]


def split_date_range(start: datetime, end: datetime, batch_days: int) -> List[Tuple[datetime, datetime]]:
    ranges: List[Tuple[datetime, datetime]] = []
    current = start
    while current <= end:
        batch_end = min(end, current + timedelta(days=batch_days - 1))
        ranges.append((current, batch_end))
        current = batch_end + timedelta(days=1)
    return ranges


def build_dhan_client(config: DhanFetchConfig):
    if DhanContext is None or dhanhq is None:
        raise RuntimeError("dhanhq is not installed. Install it with `pip install dhanhq` to fetch broker data.")
    context = DhanContext(config.client_id, config.access_token)
    return dhanhq(context)


def _normalize_dhan_payload(payload: object) -> List[Candle]:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            candles: List[Candle] = []
            for row in data:
                if not isinstance(row, dict):
                    continue
                timestamp_value = row.get("timestamp") or row.get("start_Time") or row.get("time")
                if timestamp_value is None:
                    continue
                if isinstance(timestamp_value, (int, float)):
                    timestamp = datetime.fromtimestamp(timestamp_value)
                else:
                    timestamp = parse_timestamp(str(timestamp_value))
                candles.append(
                    Candle(
                        timestamp=timestamp,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("volume", 0.0) or 0.0),
                    )
                )
            return candles

        if isinstance(data, dict):
            return _normalize_dhan_payload(data)

        if all(key in payload for key in ("timestamp", "open", "high", "low", "close")):
            timestamps = payload["timestamp"]
            opens = payload["open"]
            highs = payload["high"]
            lows = payload["low"]
            closes = payload["close"]
            volumes = payload.get("volume") or [0.0] * len(timestamps)
            candles = []
            for timestamp_value, open_price, high_price, low_price, close_price, volume in zip(
                timestamps, opens, highs, lows, closes, volumes
            ):
                if isinstance(timestamp_value, (int, float)):
                    timestamp = datetime.fromtimestamp(timestamp_value)
                else:
                    timestamp = parse_timestamp(str(timestamp_value))
                candles.append(
                    Candle(
                        timestamp=timestamp,
                        open=float(open_price),
                        high=float(high_price),
                        low=float(low_price),
                        close=float(close_price),
                        volume=float(volume or 0.0),
                    )
                )
            return candles

        if payload.get("status") == "failure":
            raise ValueError(f"Dhan API error: {payload.get('remarks')}")

    raise ValueError("Unsupported Dhan candle response shape.")


def fetch_dhan_intraday_candles(
    config: DhanFetchConfig,
    start: datetime,
    end: datetime,
) -> List[Candle]:
    client = build_dhan_client(config)
    all_candles: List[Candle] = []

    batches = split_date_range(start, end, config.batch_days)
    for batch_number, (batch_start, batch_end) in enumerate(batches):
        response = None
        for attempt in range(1, config.max_retries + 1):
            response = client.intraday_minute_data(
                security_id=config.security_id,
                exchange_segment=config.exchange_segment,
                instrument_type=config.instrument_type,
                from_date=batch_start.strftime("%Y-%m-%d %H:%M:%S"),
                to_date=batch_end.strftime("%Y-%m-%d %H:%M:%S"),
                interval=config.interval,
                oi=config.oi,
            )
            try:
                all_candles.extend(_normalize_dhan_payload(response))
                break
            except ValueError as exc:
                if "DH-904" not in str(exc) or attempt == config.max_retries:
                    raise
                time.sleep(config.retry_pause_seconds * attempt)

        if batch_number < len(batches) - 1:
            time.sleep(config.batch_pause_seconds)

    deduped: Dict[datetime, Candle] = {}
    for candle in all_candles:
        deduped[candle.timestamp] = candle
    return [deduped[timestamp] for timestamp in sorted(deduped)]


def build_nifty_dhan_fetch_config(
    client_id: str,
    access_token: str,
    interval: int = 15,
    security_id: str = DEFAULT_NIFTY_SECURITY_ID,
    exchange_segment: str = DEFAULT_NIFTY_EXCHANGE_SEGMENT,
    instrument_type: str = DEFAULT_NIFTY_INSTRUMENT_TYPE,
) -> DhanFetchConfig:
    return DhanFetchConfig(
        client_id=client_id,
        access_token=access_token,
        security_id=security_id,
        exchange_segment=exchange_segment,
        instrument_type=instrument_type,
        interval=interval,
    )


def build_stock_dhan_fetch_config(
    client_id: str,
    access_token: str,
    interval: int = 15,
    security_id: str = "",
    exchange_segment: str = DEFAULT_STOCK_EXCHANGE_SEGMENT,
    instrument_type: str = DEFAULT_STOCK_INSTRUMENT_TYPE,
) -> DhanFetchConfig:
    return DhanFetchConfig(
        client_id=client_id,
        access_token=access_token,
        security_id=security_id,
        exchange_segment=exchange_segment,
        instrument_type=instrument_type,
        interval=interval,
    )


def load_dhan_scrip_master_rows(url: str = DHAN_SCRIP_MASTER_URL) -> List[Dict[str, str]]:
    with urllib.request.urlopen(url, timeout=30) as response:
        content = response.read().decode("utf-8")
    reader = csv.DictReader(content.splitlines())
    return [{(key or "").strip(): (value or "").strip() for key, value in row.items()} for row in reader]


def load_local_instrument_rows(path: Path) -> List[Dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return [{(key or "").strip(): (value or "").strip() for key, value in row.items()} for row in reader]


def discover_local_instrument_file() -> Optional[Path]:
    candidates = sorted(Path(".").glob("all_instrument*.csv"))
    return candidates[0] if candidates else None


def _row_matches_index_symbol(
    row: Dict[str, str],
    normalized_symbol: str,
    exchange_segment: str,
    instrument_type: str,
) -> bool:
    exchange_candidates = {
        row.get("SEM_EXM_EXCH_ID", "").strip().upper(),
        row.get("exchange_segment", "").strip().upper(),
    }
    instrument_candidates = {
        row.get("SEM_INSTRUMENT_NAME", "").strip().upper(),
        row.get("instrument_type", "").strip().upper(),
    }
    segment_candidates = {
        row.get("SEM_SEGMENT", "").strip().upper(),
    }
    trading_symbol = row.get("SEM_TRADING_SYMBOL", "").strip().upper() or row.get("trading_symbol", "").strip().upper()
    custom_symbol = row.get("SEM_CUSTOM_SYMBOL", "").strip().upper() or row.get("custom_symbol", "").strip().upper()
    company_name = row.get("SM_SYMBOL_NAME", "").strip().upper() or row.get("symbol_name", "").strip().upper()

    requested_exchange = exchange_segment.upper()
    requested_instrument = instrument_type.upper()
    instrument_ok = requested_instrument in instrument_candidates or (
        requested_instrument == "INDEX" and "INDEX" in instrument_candidates
    )
    exchange_ok = requested_exchange in exchange_candidates
    if requested_exchange == "IDX_I":
        exchange_ok = exchange_ok or ("NSE" in exchange_candidates and "I" in segment_candidates)
    if requested_exchange == "NSE_EQ":
        exchange_ok = exchange_ok or ("NSE" in exchange_candidates and "E" in segment_candidates)
    if requested_exchange == "BSE_EQ":
        exchange_ok = exchange_ok or ("BSE" in exchange_candidates and "E" in segment_candidates)
    return exchange_ok and instrument_ok and normalized_symbol in {trading_symbol, custom_symbol, company_name}


def _extract_security_id(row: Dict[str, str]) -> Optional[str]:
    security_id = row.get("SEM_SMST_SECURITY_ID", "").strip() or row.get("security_id", "").strip()
    return security_id or None


def resolve_index_security_id(
    symbol_name: str,
    exchange_segment: str = DEFAULT_NIFTY_EXCHANGE_SEGMENT,
    instrument_type: str = DEFAULT_NIFTY_INSTRUMENT_TYPE,
) -> Optional[str]:
    normalized_symbol = symbol_name.strip().upper()
    local_file = discover_local_instrument_file()
    if local_file:
        for row in load_local_instrument_rows(local_file):
            if _row_matches_index_symbol(row, normalized_symbol, exchange_segment, instrument_type):
                security_id = _extract_security_id(row)
                if security_id:
                    return security_id

    for row in load_dhan_scrip_master_rows():
        if _row_matches_index_symbol(row, normalized_symbol, exchange_segment, instrument_type):
            security_id = _extract_security_id(row)
            if security_id:
                return security_id
    return None


def resolve_symbol_security_id(
    symbol_name: str,
    exchange_segment: str,
    instrument_type: str,
) -> Optional[str]:
    normalized_symbol = symbol_name.strip().upper()
    local_file = discover_local_instrument_file()
    if local_file:
        for row in load_local_instrument_rows(local_file):
            if _row_matches_index_symbol(row, normalized_symbol, exchange_segment, instrument_type):
                security_id = _extract_security_id(row)
                if security_id:
                    return security_id

    for row in load_dhan_scrip_master_rows():
        if _row_matches_index_symbol(row, normalized_symbol, exchange_segment, instrument_type):
            security_id = _extract_security_id(row)
            if security_id:
                return security_id
    return None


def write_candles_csv(path: Path, candles: List[Candle]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for candle in candles:
            writer.writerow(
                [
                    candle.timestamp.isoformat(sep=" "),
                    f"{candle.open:.5f}",
                    f"{candle.high:.5f}",
                    f"{candle.low:.5f}",
                    f"{candle.close:.5f}",
                    f"{candle.volume:.2f}",
                ]
            )


def candle_timeframe_minutes(timeframe: str) -> int:
    if timeframe not in TIMEFRAME_MINUTES:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    return TIMEFRAME_MINUTES[timeframe]


def bucket_start(timestamp: datetime, timeframe: str) -> datetime:
    if timeframe == "1m":
        return timestamp.replace(second=0, microsecond=0)
    if timeframe == "5m":
        minute = (timestamp.minute // 5) * 5
        return timestamp.replace(minute=minute, second=0, microsecond=0)
    if timeframe == "15m":
        minute = (timestamp.minute // 15) * 15
        return timestamp.replace(minute=minute, second=0, microsecond=0)
    if timeframe == "1h":
        return timestamp.replace(minute=0, second=0, microsecond=0)
    if timeframe == "4h":
        hour = (timestamp.hour // 4) * 4
        return timestamp.replace(hour=hour, minute=0, second=0, microsecond=0)
    if timeframe == "1d":
        return timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
    if timeframe == "1w":
        start = timestamp - timedelta(days=timestamp.weekday())
        return start.replace(hour=0, minute=0, second=0, microsecond=0)
    if timeframe == "1M":
        return timestamp.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"Unsupported timeframe: {timeframe}")


def resample_candles(candles: Iterable[Candle], timeframe: str) -> List[Candle]:
    grouped: Dict[datetime, List[Candle]] = defaultdict(list)
    for candle in candles:
        grouped[bucket_start(candle.timestamp, timeframe)].append(candle)

    aggregated: List[Candle] = []
    for start in sorted(grouped):
        group = grouped[start]
        aggregated.append(
            Candle(
                timestamp=start,
                open=group[0].open,
                high=max(item.high for item in group),
                low=min(item.low for item in group),
                close=group[-1].close,
                volume=sum(item.volume for item in group),
            )
        )
    return aggregated


def detect_swings(candles: List[Candle], lookback: int) -> Tuple[List[SwingPoint], List[SwingPoint]]:
    highs: List[SwingPoint] = []
    lows: List[SwingPoint] = []

    for i in range(lookback, len(candles) - lookback):
        left = candles[i - lookback : i]
        right = candles[i + 1 : i + 1 + lookback]
        current = candles[i]
        if all(current.high > bar.high for bar in left + right):
            highs.append(SwingPoint(i, current.timestamp, current.high, "high"))
        if all(current.low < bar.low for bar in left + right):
            lows.append(SwingPoint(i, current.timestamp, current.low, "low"))

    return highs, lows


def detect_structure_breaks(candles: List[Candle], lookback: int) -> List[StructureEvent]:
    swing_highs, swing_lows = detect_swings(candles, lookback)
    events: List[StructureEvent] = []
    active_highs = [None] * len(candles)
    active_lows = [None] * len(candles)

    last_high = None
    high_iter = iter(swing_highs)
    next_high = next(high_iter, None)
    for index in range(len(candles)):
        while next_high and next_high.index <= index:
            last_high = next_high
            next_high = next(high_iter, None)
        active_highs[index] = last_high

    last_low = None
    low_iter = iter(swing_lows)
    next_low = next(low_iter, None)
    for index in range(len(candles)):
        while next_low and next_low.index <= index:
            last_low = next_low
            next_low = next(low_iter, None)
        active_lows[index] = last_low

    last_direction: Optional[str] = None
    for i, candle in enumerate(candles):
        previous_high = active_highs[i]
        previous_low = active_lows[i]

        if previous_high and candle.close > previous_high.price and i > previous_high.index:
            kind = "bos" if last_direction in (None, "bullish") else "choch"
            events.append(StructureEvent(i, candle.timestamp, "bullish", previous_high.price, previous_high.index, kind))
            last_direction = "bullish"
        elif previous_low and candle.close < previous_low.price and i > previous_low.index:
            kind = "bos" if last_direction in (None, "bearish") else "choch"
            events.append(StructureEvent(i, candle.timestamp, "bearish", previous_low.price, previous_low.index, kind))
            last_direction = "bearish"

    unique_events: List[StructureEvent] = []
    seen = set()
    for event in events:
        key = (event.index, event.direction, event.level)
        if key not in seen:
            seen.add(key)
            unique_events.append(event)
    return unique_events


def detect_fvg_zones(candles: List[Candle], timeframe: str) -> List[Zone]:
    zones: List[Zone] = []
    for i in range(2, len(candles)):
        left = candles[i - 2]
        current = candles[i]
        if left.high < current.low:
            zones.append(
                Zone(
                    kind="fvg",
                    direction="bullish",
                    timeframe=timeframe,
                    created_index=i,
                    created_at=current.timestamp,
                    low=left.high,
                    high=current.low,
                )
            )
        if left.low > current.high:
            zones.append(
                Zone(
                    kind="fvg",
                    direction="bearish",
                    timeframe=timeframe,
                    created_index=i,
                    created_at=current.timestamp,
                    low=current.high,
                    high=left.low,
                )
            )
    return zones


def detect_order_blocks(candles: List[Candle], structure_events: List[StructureEvent], timeframe: str, search_bars: int) -> List[Zone]:
    zones: List[Zone] = []
    for event in structure_events:
        start = max(0, event.index - search_bars)
        window = candles[start : event.index]
        if event.direction == "bullish":
            candidates = [bar for bar in reversed(window) if bar.close < bar.open]
        else:
            candidates = [bar for bar in reversed(window) if bar.close > bar.open]

        if not candidates:
            continue

        source = candidates[0]
        zones.append(
            Zone(
                kind="order_block",
                direction=event.direction,
                timeframe=timeframe,
                created_index=event.index,
                created_at=event.timestamp,
                low=source.low,
                high=source.high,
                meta={"structure_kind": event.kind},
            )
        )
    return zones


def calculate_bias(candles: List[Candle], lookback: int) -> List[str]:
    swing_highs, swing_lows = detect_swings(candles, lookback)
    high_map = {item.index: item for item in swing_highs}
    low_map = {item.index: item for item in swing_lows}
    bias = ["neutral"] * len(candles)

    recent_highs: List[SwingPoint] = []
    recent_lows: List[SwingPoint] = []
    current_bias = "neutral"

    for i in range(len(candles)):
        if i in high_map:
            recent_highs.append(high_map[i])
            recent_highs = recent_highs[-2:]
        if i in low_map:
            recent_lows.append(low_map[i])
            recent_lows = recent_lows[-2:]

        if len(recent_highs) == 2 and len(recent_lows) == 2:
            higher_highs = recent_highs[1].price > recent_highs[0].price
            higher_lows = recent_lows[1].price > recent_lows[0].price
            lower_highs = recent_highs[1].price < recent_highs[0].price
            lower_lows = recent_lows[1].price < recent_lows[0].price
            if higher_highs and higher_lows:
                current_bias = "bullish"
            elif lower_highs and lower_lows:
                current_bias = "bearish"
        elif i >= lookback * 3:
            reference_close = candles[i - (lookback * 3)].close
            if candles[i].close > reference_close:
                current_bias = "bullish"
            elif candles[i].close < reference_close:
                current_bias = "bearish"
        bias[i] = current_bias

    return bias


def latest_completed_index(candles: List[Candle], timestamp: datetime) -> Optional[int]:
    last_index = None
    for i, candle in enumerate(candles):
        if candle.timestamp <= timestamp:
            last_index = i
        else:
            break
    return last_index


def current_range(candles: List[Candle], index: int, lookback: int) -> Optional[Tuple[float, float]]:
    if index <= 0:
        return None
    start = max(0, index - lookback)
    window = candles[start : index + 1]
    if len(window) < 2:
        return None
    return min(bar.low for bar in window), max(bar.high for bar in window)


def in_discount_or_premium(direction: str, price: float, swing_range: Optional[Tuple[float, float]]) -> bool:
    if not swing_range:
        return False
    low, high = swing_range
    midpoint = low + (high - low) * 0.5
    if direction == "bullish":
        return price <= midpoint
    return price >= midpoint


def latest_zone_touch(
    zones: List[Zone],
    candle: Candle,
    direction: str,
    current_index: int,
    max_age_bars: int,
) -> Optional[Zone]:
    candidates = [
        zone
        for zone in zones
        if zone.direction == direction
        and zone.created_index < current_index
        and current_index - zone.created_index <= max_age_bars
        and zone.touched_by(candle)
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda zone: zone.created_index, reverse=True)[0]


def latest_zone_touch_fast(
    zones: List[Zone],
    candle: Candle,
    direction: str,
    current_index: int,
    max_age_bars: int,
) -> Optional[Zone]:
    for zone in reversed(zones):
        if zone.created_index >= current_index:
            continue
        if current_index - zone.created_index > max_age_bars:
            break
        if zone.direction == direction and zone.touched_by(candle):
            return zone
    return None


def build_confirmation_index(structure_events: List[StructureEvent]) -> Dict[str, set[int]]:
    index = {
        "bullish": set(),
        "bearish": set(),
        "bullish_bos": set(),
        "bearish_bos": set(),
        "bullish_choch": set(),
        "bearish_choch": set(),
    }
    for event in structure_events:
        index[event.direction].add(event.index)
        index[f"{event.direction}_{event.kind}"].add(event.index)
    return index


def has_recent_confirmation(
    confirmation_index: Dict[str, set[int]],
    index: int,
    direction: str,
    lookback_bars: int,
    mode: str,
) -> bool:
    start = max(0, index - lookback_bars)
    if mode == "bos_only":
        key = f"{direction}_bos"
    elif mode == "choch_only":
        key = f"{direction}_choch"
    else:
        key = direction
    for candidate in range(start, index + 1):
        if candidate in confirmation_index[key]:
            return True
    return False


def is_rejection_candle(candle: Candle, direction: str) -> bool:
    candle_range = candle.high - candle.low
    if candle_range <= 0:
        return False
    body = abs(candle.close - candle.open)
    body_ratio = body / candle_range
    upper_wick = candle.high - max(candle.open, candle.close)
    lower_wick = min(candle.open, candle.close) - candle.low
    if direction == "bullish":
        return candle.close > candle.open and lower_wick / candle_range >= 0.35 and body_ratio >= 0.2
    return candle.close < candle.open and upper_wick / candle_range >= 0.35 and body_ratio >= 0.2


def evaluate_trade_exit(trade: Trade, candle: Candle) -> Optional[Tuple[float, str]]:
    if trade.direction == "bullish":
        if candle.low <= trade.stop_price:
            return trade.stop_price, "stop"
        if candle.high >= trade.target_price:
            return trade.target_price, "target"
    else:
        if candle.high >= trade.stop_price:
            return trade.stop_price, "stop"
        if candle.low <= trade.target_price:
            return trade.target_price, "target"
    return None


def compute_metrics(trades: List[Trade], starting_equity: float, risk_per_trade: float) -> Dict[str, float]:
    closed = [trade for trade in trades if trade.exit_price is not None]
    wins = [trade for trade in closed if (trade.pnl_r or 0.0) > 0]
    total_r = sum(trade.pnl_r or 0.0 for trade in closed)
    equity = starting_equity

    for trade in closed:
        equity += equity * risk_per_trade * (trade.pnl_r or 0.0)

    return {
        "trades": float(len(closed)),
        "win_rate": (len(wins) / len(closed) * 100.0) if closed else 0.0,
        "avg_r": (total_r / len(closed)) if closed else 0.0,
        "total_r": total_r,
        "ending_equity": equity,
        "return_pct": ((equity / starting_equity) - 1.0) * 100.0,
    }


def format_metric_value(key: str, value: float) -> str:
    if key in {"trades"}:
        return str(int(value))
    if key.endswith("_pct") or key == "win_rate":
        return f"{value:.2f}%"
    return f"{value:.2f}"


def print_metrics(metrics: Dict[str, float]) -> None:
    for key, value in metrics.items():
        print(f"{key}: {format_metric_value(key, value)}")


def print_diagnostics(diagnostics: Dict[str, float]) -> None:
    print("Diagnostics")
    ordered_keys = [
        "base_bars",
        "bars_with_timeframe_context",
        "bias_aligned",
        "discount_or_premium_ok",
        "zone_touch_ok",
        "confirmation_ok",
        "rejection_ok",
        "session_ok",
        "intraday_squareoffs",
        "invalid_risk_rejected",
        "entries_opened",
        "closed_trades",
    ]
    for key in ordered_keys:
        if key in diagnostics:
            print(f"{key}: {int(diagnostics[key])}")


def parse_float_list(value: str) -> List[float]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    if not items:
        raise ValueError("Expected at least one numeric value.")
    return [float(item) for item in items]


def parse_string_list(value: str) -> List[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    if not items:
        raise ValueError("Expected at least one value.")
    return items


def parse_clock_time(value: str) -> Tuple[int, int]:
    hour_text, minute_text = value.split(":", 1)
    hour = int(hour_text)
    minute = int(minute_text)
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"Invalid time: {value}")
    return hour, minute


def candle_in_session(candle: Candle, session_start: Optional[str], session_end: Optional[str]) -> bool:
    if not session_start and not session_end:
        return True
    if not session_start or not session_end:
        raise ValueError("Both session_start and session_end must be set together.")
    start_hour, start_minute = parse_clock_time(session_start)
    end_hour, end_minute = parse_clock_time(session_end)
    candle_minutes = candle.timestamp.hour * 60 + candle.timestamp.minute
    start_minutes = start_hour * 60 + start_minute
    end_minutes = end_hour * 60 + end_minute
    return start_minutes <= candle_minutes <= end_minutes


def candle_at_or_after_time(candle: Candle, clock_time: str) -> bool:
    hour, minute = parse_clock_time(clock_time)
    candle_minutes = candle.timestamp.hour * 60 + candle.timestamp.minute
    target_minutes = hour * 60 + minute
    return candle_minutes >= target_minutes


def run_backtest(candles: List[Candle], config: BacktestConfig) -> BacktestResult:
    if config.base_timeframe not in TIMEFRAME_MINUTES:
        raise ValueError("Unsupported base timeframe.")
    if len(config.bias_timeframes) < 2:
        raise ValueError("bias_timeframes must contain at least 2 timeframes.")
    for timeframe in config.bias_timeframes:
        if timeframe not in TIMEFRAME_MINUTES:
            raise ValueError(f"Unsupported bias timeframe: {timeframe}")
    if config.allowed_zone_kinds is not None:
        invalid_zone_kinds = [kind for kind in config.allowed_zone_kinds if kind not in {"fvg", "order_block"}]
        if invalid_zone_kinds:
            raise ValueError(f"Unsupported zone kind(s): {', '.join(invalid_zone_kinds)}")

    timeframes = {
        "1m": candles if config.base_timeframe == "1m" else resample_candles(candles, "1m"),
        "5m": candles if config.base_timeframe == "5m" else resample_candles(candles, "5m"),
        "15m": candles if config.base_timeframe == "15m" else resample_candles(candles, "15m"),
        "1h": resample_candles(candles, "1h"),
        "4h": resample_candles(candles, "4h"),
        "1d": resample_candles(candles, "1d"),
        "1w": resample_candles(candles, "1w"),
        "1M": resample_candles(candles, "1M"),
    }

    biases = {timeframe: calculate_bias(timeframes[timeframe], config.swing_lookback) for timeframe in config.bias_timeframes}
    poi_candles = timeframes[config.poi_timeframe]
    poi_structure = detect_structure_breaks(poi_candles, config.swing_lookback)
    poi_zones = detect_fvg_zones(poi_candles, config.poi_timeframe) + detect_order_blocks(
        poi_candles, poi_structure, config.poi_timeframe, config.order_block_search
    )

    confirmation_events = {
        timeframe: build_confirmation_index(detect_structure_breaks(timeframes[timeframe], config.swing_lookback))
        for timeframe in config.confirmation_timeframes
    }
    if config.allowed_zone_kinds is not None:
        allowed = set(config.allowed_zone_kinds)
        poi_zones = [zone for zone in poi_zones if zone.kind in allowed]
    poi_zones.sort(key=lambda zone: zone.created_index)

    base_candles = timeframes[config.base_timeframe]
    trades: List[Trade] = []
    open_trade: Optional[Trade] = None
    diagnostics = {
        "base_bars": float(len(base_candles)),
        "bars_with_timeframe_context": 0.0,
        "bias_aligned": 0.0,
        "discount_or_premium_ok": 0.0,
        "zone_touch_ok": 0.0,
        "confirmation_ok": 0.0,
        "rejection_ok": 0.0,
        "session_ok": 0.0,
        "intraday_squareoffs": 0.0,
        "invalid_risk_rejected": 0.0,
        "entries_opened": 0.0,
        "closed_trades": 0.0,
    }
    pointers = {name: 0 for name in [*config.bias_timeframes, config.poi_timeframe, *config.confirmation_timeframes]}
    previous_candle: Optional[Candle] = None

    def advance_pointer(timeframe: str, timestamp: datetime) -> Optional[int]:
        series = timeframes[timeframe]
        if not series or series[0].timestamp > timestamp:
            return None
        pointer = pointers[timeframe]
        while pointer + 1 < len(series) and series[pointer + 1].timestamp <= timestamp:
            pointer += 1
        pointers[timeframe] = pointer
        return pointer

    for i, candle in enumerate(base_candles):
        if open_trade:
            exit_info = evaluate_trade_exit(open_trade, candle)
            if exit_info:
                open_trade.exit_price, open_trade.exit_reason = exit_info
                open_trade.exit_time = candle.timestamp
                trades.append(open_trade)
                open_trade = None
            else:
                if config.force_intraday_exit:
                    entry_date = open_trade.entry_time.date()
                    if config.square_off_time and candle.timestamp.date() == entry_date and candle_at_or_after_time(candle, config.square_off_time):
                        open_trade.exit_price = candle.close
                        open_trade.exit_reason = "intraday_squareoff"
                        open_trade.exit_time = candle.timestamp
                        trades.append(open_trade)
                        open_trade = None
                        diagnostics["intraday_squareoffs"] += 1
                    elif candle.timestamp.date() > entry_date and previous_candle and previous_candle.timestamp.date() == entry_date:
                        open_trade.exit_price = previous_candle.close
                        open_trade.exit_reason = "intraday_squareoff"
                        open_trade.exit_time = previous_candle.timestamp
                        trades.append(open_trade)
                        open_trade = None
                        diagnostics["intraday_squareoffs"] += 1
            if open_trade:
                previous_candle = candle
                continue

        bias_indices = [advance_pointer(timeframe, candle.timestamp) for timeframe in config.bias_timeframes]
        poi_index = advance_pointer(config.poi_timeframe, candle.timestamp)

        if any(index is None for index in bias_indices) or poi_index is None:
            continue
        diagnostics["bars_with_timeframe_context"] += 1

        active_biases = [biases[timeframe][index] for timeframe, index in zip(config.bias_timeframes, bias_indices) if index is not None]
        if "neutral" in set(active_biases):
            continue
        if len(set(active_biases)) != 1:
            continue
        diagnostics["bias_aligned"] += 1

        direction = active_biases[0]
        swing_range = current_range(poi_candles, poi_index, config.structure_lookback)
        if not in_discount_or_premium(direction, candle.close, swing_range):
            continue
        diagnostics["discount_or_premium_ok"] += 1

        zone = latest_zone_touch_fast(poi_zones, candle, direction, poi_index, config.zone_max_age_bars)
        if not zone:
            continue
        diagnostics["zone_touch_ok"] += 1

        confirmation_found = False
        for timeframe in config.confirmation_timeframes:
            confirm_index = advance_pointer(timeframe, candle.timestamp)
            if confirm_index is None:
                continue
            structure_mode = config.confirmation_mode
            if structure_mode in {"rejection_candle", "bos_and_rejection"}:
                structure_mode = "bos_or_choch"
            if has_recent_confirmation(
                confirmation_events[timeframe],
                confirm_index,
                direction,
                config.confirmation_lookback_bars,
                structure_mode,
            ):
                confirmation_found = True
                break

        rejection_ok = is_rejection_candle(candle, direction)
        if config.confirmation_mode == "rejection_candle":
            if not rejection_ok:
                continue
        elif config.confirmation_mode == "bos_and_rejection":
            if not confirmation_found or not rejection_ok:
                continue
        else:
            if not confirmation_found:
                continue
        diagnostics["confirmation_ok"] += 1
        if rejection_ok:
            diagnostics["rejection_ok"] += 1

        if not candle_in_session(candle, config.session_start, config.session_end):
            continue
        diagnostics["session_ok"] += 1

        entry = candle.close
        if direction == "bullish":
            stop = zone.low - config.stop_buffer
            target = entry + (entry - stop) * config.risk_reward
        else:
            stop = zone.high + config.stop_buffer
            target = entry - (stop - entry) * config.risk_reward

        if (direction == "bullish" and stop >= entry) or (direction == "bearish" and stop <= entry):
            diagnostics["invalid_risk_rejected"] += 1
            continue

        open_trade = Trade(
            direction=direction,
            entry_time=candle.timestamp,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            setup_zone=zone,
        )
        diagnostics["entries_opened"] += 1
        previous_candle = candle
        continue

    if open_trade:
        last_close = base_candles[-1].close
        open_trade.exit_time = base_candles[-1].timestamp
        open_trade.exit_price = last_close
        open_trade.exit_reason = "end_of_data"
        trades.append(open_trade)
    else:
        previous_candle = base_candles[-1] if base_candles else None

    metrics = compute_metrics(trades, config.starting_equity, config.risk_per_trade)
    diagnostics["closed_trades"] = metrics["trades"]
    return BacktestResult(trades=trades, metrics=metrics, diagnostics=diagnostics)


def run_parameter_sweep(
    candles: List[Candle],
    base_config: BacktestConfig,
    rr_values: List[float],
    poi_timeframes: List[str],
    session_windows: List[Tuple[Optional[str], Optional[str]]],
    confirmation_modes: List[str],
) -> List[Dict[str, float | str]]:
    results: List[Dict[str, float | str]] = []
    for poi_timeframe in poi_timeframes:
        for risk_reward in rr_values:
            for session_start, session_end in session_windows:
                for confirmation_mode in confirmation_modes:
                    session_label = "full_session" if not session_start or not session_end else f"{session_start}-{session_end}"
                    config = BacktestConfig(
                        base_timeframe=base_config.base_timeframe,
                        poi_timeframe=poi_timeframe,
                        confirmation_timeframes=base_config.confirmation_timeframes,
                        confirmation_mode=confirmation_mode,
                        swing_lookback=base_config.swing_lookback,
                        structure_lookback=base_config.structure_lookback,
                        order_block_search=base_config.order_block_search,
                        zone_max_age_bars=base_config.zone_max_age_bars,
                        stop_buffer=base_config.stop_buffer,
                        risk_reward=risk_reward,
                        starting_equity=base_config.starting_equity,
                        risk_per_trade=base_config.risk_per_trade,
                        session_start=session_start,
                        session_end=session_end,
                    )
                    result = run_backtest(candles, config)
                    results.append(
                        {
                            "poi_timeframe": poi_timeframe,
                            "rr": risk_reward,
                            "session": session_label,
                            "confirmation_mode": confirmation_mode,
                            **result.metrics,
                        }
                    )
    results.sort(key=lambda item: (float(item["total_r"]), float(item["win_rate"])), reverse=True)
    return results


def find_latest_entry_signal(candles: List[Candle], config: BacktestConfig) -> Optional[Trade]:
    if not candles:
        return None
    result = run_backtest(candles, config)
    latest_timestamp = candles[-1].timestamp
    latest_entries = [trade for trade in result.trades if trade.entry_time == latest_timestamp]
    if not latest_entries:
        return None
    return latest_entries[-1]


def write_sweep_csv(path: Path, rows: List[Dict[str, float | str]]) -> None:
    if not rows:
        return
    headers = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def print_sweep_results(rows: List[Dict[str, float | str]], limit: int = 10) -> None:
    print("Sweep Results")
    for row in rows[:limit]:
        print(
            " | ".join(
                [
                    f"poi_timeframe={row['poi_timeframe']}",
                    f"rr={float(row['rr']):.2f}",
                    f"session={row['session']}",
                    f"confirmation={row['confirmation_mode']}",
                    f"trades={int(float(row['trades']))}",
                    f"win_rate={float(row['win_rate']):.2f}%",
                    f"avg_r={float(row['avg_r']):.2f}",
                    f"total_r={float(row['total_r']):.2f}",
                    f"return_pct={float(row['return_pct']):.2f}%",
                ]
            )
        )


def parse_session_windows(value: str) -> List[Tuple[Optional[str], Optional[str]]]:
    windows: List[Tuple[Optional[str], Optional[str]]] = []
    for item in parse_string_list(value):
        if item.lower() == "full":
            windows.append((None, None))
            continue
        if "-" not in item:
            raise ValueError(f"Invalid session window: {item}")
        start, end = [part.strip() for part in item.split("-", 1)]
        parse_clock_time(start)
        parse_clock_time(end)
        windows.append((start, end))
    return windows


def write_trades_csv(path: Path, trades: List[Trade]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "direction",
                "entry_time",
                "entry_price",
                "stop_price",
                "target_price",
                "exit_time",
                "exit_price",
                "exit_reason",
                "pnl_r",
                "zone_kind",
                "zone_timeframe",
            ]
        )
        for trade in trades:
            writer.writerow(
                [
                    trade.direction,
                    trade.entry_time.isoformat(sep=" "),
                    f"{trade.entry_price:.5f}",
                    f"{trade.stop_price:.5f}",
                    f"{trade.target_price:.5f}",
                    trade.exit_time.isoformat(sep=" ") if trade.exit_time else "",
                    f"{trade.exit_price:.5f}" if trade.exit_price is not None else "",
                    trade.exit_reason or "",
                    f"{(trade.pnl_r or 0.0):.3f}",
                    trade.setup_zone.kind if trade.setup_zone else "",
                    trade.setup_zone.timeframe if trade.setup_zone else "",
                ]
            )


def generate_sample_data(path: Path, bars: int = 25_000) -> None:
    timestamp = datetime(2024, 1, 1, 0, 0, 0)
    price = 100.0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])

        for i in range(bars):
            drift = 0.018 if (i // 1500) % 2 == 0 else -0.012
            wave = math.sin(i / 25.0) * 0.22
            shock = random.uniform(-0.18, 0.18)
            open_price = price
            close_price = max(1.0, open_price + drift + wave + shock)
            high = max(open_price, close_price) + random.uniform(0.01, 0.25)
            low = min(open_price, close_price) - random.uniform(0.01, 0.25)
            volume = random.uniform(100, 1000)
            writer.writerow(
                [
                    timestamp.isoformat(sep=" "),
                    f"{open_price:.5f}",
                    f"{high:.5f}",
                    f"{low:.5f}",
                    f"{close_price:.5f}",
                    f"{volume:.2f}",
                ]
            )
            price = close_price
            timestamp += timedelta(minutes=15)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backtest the image-based trading plan on OHLCV CSV data.")
    parser.add_argument("--data", type=Path, help="Path to OHLCV CSV data.")
    parser.add_argument("--broker", choices=("dhan",), help="Fetch candles from a broker instead of reading CSV.")
    parser.add_argument("--from-date", help="Broker fetch start in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--to-date", help="Broker fetch end in YYYY-MM-DD or YYYY-MM-DD HH:MM:SS.")
    parser.add_argument("--client-id", help="Broker client id. For Dhan, can also come from DHAN_CLIENT_ID.")
    parser.add_argument("--access-token", help="Broker access token. For Dhan, can also come from DHAN_ACCESS_TOKEN.")
    parser.add_argument("--security-id", help="Broker security id. For Dhan, this is required when using --broker dhan.")
    parser.add_argument("--exchange-segment", default="IDX_I", help="Broker exchange segment, for example IDX_I or NSE_EQ.")
    parser.add_argument("--instrument-type", default="INDEX", help="Broker instrument type, for example INDEX or EQUITY.")
    parser.add_argument("--interval", type=int, default=15, help="Broker candle interval in minutes.")
    parser.add_argument("--save-fetched-data", type=Path, help="Optional path to save broker-fetched candles as CSV.")
    parser.add_argument("--base-timeframe", default="15m", choices=sorted(TIMEFRAME_MINUTES))
    parser.add_argument("--poi-timeframe", default="4h", choices=("1h", "4h", "1d"))
    parser.add_argument("--session-start", help="Entry window start in HH:MM, for example 09:15.")
    parser.add_argument("--session-end", help="Entry window end in HH:MM, for example 10:45.")
    parser.add_argument(
        "--confirmation-mode",
        default="bos_or_choch",
        choices=("bos_or_choch", "bos_only", "choch_only", "rejection_candle", "bos_and_rejection"),
        help="Entry confirmation mode.",
    )
    parser.add_argument("--rr", type=float, default=2.0, help="Risk/reward target multiple.")
    parser.add_argument("--risk-per-trade", type=float, default=0.01)
    parser.add_argument("--output", type=Path, default=Path("backtest_trades.csv"))
    parser.add_argument("--diagnostics", action="store_true", help="Print filter diagnostics for the current run.")
    parser.add_argument("--sweep-rr", help="Comma-separated RR values for a parameter sweep, for example 1.5,2.0,3.0.")
    parser.add_argument("--sweep-poi", help="Comma-separated POI timeframes for a parameter sweep, for example 1h,4h.")
    parser.add_argument("--sweep-session", help="Comma-separated session windows such as full,09:15-10:45,10:00-13:00.")
    parser.add_argument(
        "--sweep-confirmation",
        help="Comma-separated confirmation modes such as bos_or_choch,bos_only,rejection_candle.",
    )
    parser.add_argument("--sweep-output", type=Path, default=Path("backtest_sweep.csv"), help="CSV path for parameter sweep results.")
    parser.add_argument("--generate-sample", type=Path, help="Write synthetic sample data to the given CSV path.")
    parser.add_argument("--sample-bars", type=int, default=25_000, help="Number of synthetic 15m bars to generate.")
    return parser


def main() -> None:
    load_env_file()
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.generate_sample:
        generate_sample_data(args.generate_sample, bars=args.sample_bars)
        print(f"Sample data written to {args.generate_sample}")
        return

    if not args.data and not args.broker:
        parser.error("Provide --data, use --broker, or use --generate-sample.")

    config = BacktestConfig(
        base_timeframe=args.base_timeframe,
        poi_timeframe=args.poi_timeframe,
        confirmation_mode=args.confirmation_mode,
        risk_reward=args.rr,
        risk_per_trade=args.risk_per_trade,
        session_start=args.session_start,
        session_end=args.session_end,
    )
    if bool(args.session_start) != bool(args.session_end):
        parser.error("--session-start and --session-end must be provided together.")
    if args.session_start and args.session_end:
        try:
            parse_clock_time(args.session_start)
            parse_clock_time(args.session_end)
        except ValueError as exc:
            parser.error(str(exc))
    if args.broker == "dhan":
        if not args.from_date or not args.to_date or not args.security_id:
            parser.error("--broker dhan requires --from-date, --to-date, and --security-id.")
        client_id = args.client_id or os.getenv("DHAN_CLIENT_ID")
        access_token = args.access_token or os.getenv("DHAN_ACCESS_TOKEN")
        if not client_id or not access_token:
            parser.error("Dhan credentials are required via --client-id/--access-token or DHAN_CLIENT_ID/DHAN_ACCESS_TOKEN.")
        fetch_config = DhanFetchConfig(
            client_id=client_id,
            access_token=access_token,
            security_id=args.security_id,
            exchange_segment=args.exchange_segment,
            instrument_type=args.instrument_type,
            interval=args.interval,
        )
        candles = fetch_dhan_intraday_candles(
            fetch_config,
            parse_timestamp(args.from_date),
            parse_timestamp(args.to_date),
        )
        if args.save_fetched_data:
            write_candles_csv(args.save_fetched_data, candles)
            print(f"Fetched candles written to {args.save_fetched_data}")
    else:
        candles = load_candles(args.data)

    if args.sweep_rr or args.sweep_poi:
        rr_values = parse_float_list(args.sweep_rr) if args.sweep_rr else [config.risk_reward]
        poi_timeframes = parse_string_list(args.sweep_poi) if args.sweep_poi else [config.poi_timeframe]
        session_windows = parse_session_windows(args.sweep_session) if args.sweep_session else [
            (config.session_start, config.session_end)
        ]
        confirmation_modes = parse_string_list(args.sweep_confirmation) if args.sweep_confirmation else [
            config.confirmation_mode
        ]
        invalid = [item for item in poi_timeframes if item not in {"1h", "4h", "1d"}]
        if invalid:
            parser.error(f"Unsupported POI timeframe(s) in sweep: {', '.join(invalid)}")
        allowed_confirmation = {"bos_or_choch", "bos_only", "choch_only", "rejection_candle", "bos_and_rejection"}
        invalid_confirmation = [item for item in confirmation_modes if item not in allowed_confirmation]
        if invalid_confirmation:
            parser.error(f"Unsupported confirmation mode(s): {', '.join(invalid_confirmation)}")
        sweep_rows = run_parameter_sweep(candles, config, rr_values, poi_timeframes, session_windows, confirmation_modes)
        write_sweep_csv(args.sweep_output, sweep_rows)
        print_sweep_results(sweep_rows)
        print(f"Sweep results written to {args.sweep_output}")
        return

    result = run_backtest(candles, config)
    write_trades_csv(args.output, result.trades)

    print("Backtest complete")
    print_metrics(result.metrics)
    if args.diagnostics:
        print_diagnostics(result.diagnostics)
    print(f"Trades written to {args.output}")


if __name__ == "__main__":
    main()
