import pandas as pd
from dhanhq import dhanhq, DhanContext
from datetime import datetime, timedelta
import time

class FVGBacktester:

    def __init__(self, security_id, hardtoken, client_id="1104879898"):

        self.security_id = security_id
        self.context = DhanContext(client_id, hardtoken)
        self.dhan = dhanhq(self.context)

    # ----------------------------------------
    # Fetch historical candles
    # ----------------------------------------

    def fetch_day_data(self, date):

        response = self.dhan.intraday_minute_data(
            security_id=self.security_id,
            exchange_segment=self.dhan.INDEX,
            instrument_type="INDEX",
            from_date=date,
            to_date=date,
            interval=5,
            oi=False
        )

        data = response.get("data", [])
        print(f"Fetched {len(data)} candles for {date}")
        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data)

        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")

        df = df.sort_values("timestamp").reset_index(drop=True)

        return df

    # ----------------------------------------
    # Detect FVG + OB
    # ----------------------------------------

    def detect_fvg(self, df):

        setups = []

        for i in range(2, len(df)):

            c1 = df.iloc[i - 2]
            c2 = df.iloc[i - 1]
            c3 = df.iloc[i]

            # -------------------------
            # C1 metrics
            # -------------------------

            c1_range = c1.high - c1.low
            c1_body = abs(c1.close - c1.open)

            c1_body_ratio = c1_body / c1_range if c1_range > 0 else 0

            c1_upper_wick = c1.high - max(c1.open, c1.close)
            c1_lower_wick = min(c1.open, c1.close) - c1.low

            # -------------------------
            # C3 metrics
            # -------------------------

            c3_range = c3.high - c3.low
            c3_body = abs(c3.close - c3.open)

            c3_body_ratio = c3_body / c3_range if c3_range > 0 else 0

            # skip exhaustion candles
            if c3_body_ratio > 0.7:
                continue

            c3_upper_wick = c3.high - max(c3.open, c3.close)
            c3_lower_wick = min(c3.open, c3.close) - c3.low

            # continuation metric
            c3_vs_c2_close = c3.close - c2.close

            # compression vs displacement
            c2_range = c2.high - c2.low
            c1_compression = c1_range / c2_range if c2_range > 0 else 0

            # skip weak displacement
            if c1_compression > 0.8:
                continue

            body = abs(c2.close - c2.open)
            rng = c2.high - c2.low

            hour = c3.timestamp.hour

            if hour < 9 or hour > 14:
                continue



            if rng == 0:
                continue

            displacement = body / rng

            if displacement < 0.75:
                continue

            if c3_body_ratio < 0.15:
                continue


            # BULLISH
            if c3.low > c1.high:

                gap = c3.low - c1.high

                if gap < 5 or gap > 25:
                    continue

                ratio = gap / (c2.high - c2.low)

                if ratio < 0.2 or ratio > 0.6:
                    continue

                setups.append({
                    "index": i,
                    "time": c3.timestamp,
                    "type": "BULLISH",
                    "fvg_top": c3.low,
                    "fvg_bottom": c1.high,
                    "ob_high": c2.high,
                    "ob_low": c2.low,

                    # C1 metrics
                    "c1_range": c1_range,
                    "c1_body": c1_body,
                    "c1_body_ratio": c1_body_ratio,
                    "c1_upper_wick": c1_upper_wick,
                    "c1_lower_wick": c1_lower_wick,

                    # C3 metrics
                    "c3_range": c3_range,
                    "c3_body": c3_body,
                    "c3_body_ratio": c3_body_ratio,
                    "c3_upper_wick": c3_upper_wick,
                    "c3_lower_wick": c3_lower_wick,

                    "c1_compression": c1_compression,

                    "c3_vs_c2_close": c3_vs_c2_close
                })

            # BEARISH
            if c3.high < c1.low:

                gap = c1.low - c3.high

                if gap < 5 or gap > 25:
                    continue

                setups.append({
                    "index": i,
                    "time": c3.timestamp,
                    "type": "BEARISH",
                    "fvg_top": c1.low,
                    "fvg_bottom": c3.high,
                    "ob_high": c2.high,
                    "ob_low": c2.low,

                    # C1 metrics
                    "c1_range": c1_range,
                    "c1_body": c1_body,
                    "c1_body_ratio": c1_body_ratio,
                    "c1_upper_wick": c1_upper_wick,
                    "c1_lower_wick": c1_lower_wick,

                    # C3 metrics
                    "c3_range": c3_range,
                    "c3_body": c3_body,
                    "c3_body_ratio": c3_body_ratio,
                    "c3_upper_wick": c3_upper_wick,
                    "c3_lower_wick": c3_lower_wick,

                    "c1_compression": c1_compression,

                    "c3_vs_c2_close": c3_vs_c2_close
                })

        return setups

    # ----------------------------------------
    # Simulate trade
    # ----------------------------------------

    def simulate_trade(self, df, setup):

        fvg_top = setup["fvg_top"]
        fvg_bottom = setup["fvg_bottom"]

        entry = (fvg_top + fvg_bottom) / 2

        if setup["type"] == "BULLISH":
            stop = setup["ob_low"]
            target = entry + (entry - stop) * 1.4
        else:
            stop = setup["ob_high"]
            target = entry - (stop - entry) * 1.4

        retraced = False
        hit_target = False
        hit_stop = False

        max_favorable = 0
        max_adverse = 0
        last_price = None

        max_retrace_candles = 10

        for i in range(setup["index"] + 1,
                    min(len(df), setup["index"] + 1 + max_retrace_candles)):

            candle = df.iloc[i]
            last_price = candle.close

            # -----------------
            # WAIT FOR ENTRY
            # -----------------

            if not retraced:

                if setup["type"] == "BULLISH":

                    if candle.low <= entry:
                        retraced = True

                else:

                    if candle.high >= entry:
                        retraced = True

            # -----------------
            # AFTER ENTRY
            # -----------------

            if retraced:

                if setup["type"] == "BULLISH":

                    move_up = candle.high - entry
                    move_down = entry - candle.low

                    max_favorable = max(max_favorable, move_up)
                    max_adverse = max(max_adverse, move_down)

                    if candle.low <= stop:
                        hit_stop = True
                        break

                    if candle.high >= target:
                        hit_target = True
                        break

                else:

                    move_down = entry - candle.low
                    move_up = candle.high - entry

                    max_favorable = max(max_favorable, move_down)
                    max_adverse = max(max_adverse, move_up)

                    if candle.high >= stop:
                        hit_stop = True
                        break

                    if candle.low <= target:
                        hit_target = True
                        break

        return retraced, hit_target, hit_stop, entry, stop, target, max_favorable, max_adverse, last_price
    # ----------------------------------------
    # Run backtest
    # ----------------------------------------

    def run(self, date):

        df = self.fetch_day_data(date)

        if df.empty:
            print("No data")
            return

        setups = self.detect_fvg(df)

        results = []

        for s in setups:

            # retraced, hit_target, hit_stop, entry, stop, target = self.simulate_trade(df, s)
            retraced, hit_target, hit_stop, entry, stop, target, mfe, mae, last_price = self.simulate_trade(df, s)

            gap = abs(s["fvg_top"] - s["fvg_bottom"])
            ob = abs(s["ob_high"] - s["ob_low"])

            results.append({
                "time": s["time"],
                "type": s["type"],

                "gap_size": gap,
                "ob_size": ob,

                "retraced": retraced,
                "target_hit": hit_target,
                "stop_hit": hit_stop,

                # C1 metrics
                "c1_range": s["c1_range"],
                "c1_body": s["c1_body"],
                "c1_body_ratio": s["c1_body_ratio"],
                "c1_upper_wick": s["c1_upper_wick"],
                "c1_lower_wick": s["c1_lower_wick"],

                # C3 metrics
                "c3_range": s["c3_range"],
                "c3_body": s["c3_body"],
                "c3_body_ratio": s["c3_body_ratio"],
                "c3_upper_wick": s["c3_upper_wick"],
                "c3_lower_wick": s["c3_lower_wick"],

                "c3_vs_c2_close": s["c3_vs_c2_close"],
                "c1_compression": s["c1_compression"],

                "entry": entry,
                "stop": stop,
                "target": target,
                "mfe": mfe,
                "mae": mae,
                "last_price": last_price,
            })

        result_df = pd.DataFrame(results)

        if result_df.empty:
            print("No FVG detected")
            return

        result_df["hour"] = result_df["time"].dt.hour

        result_df["gap_ob_ratio"] = result_df["gap_size"] / result_df["ob_size"]

        result_df["win"] = result_df["target_hit"].astype(int)
        result_df["loss"] = result_df["stop_hit"].astype(int)

        print("\n===== BACKTEST RESULT =====")

        total = len(result_df)

        retrace_pct = result_df["retraced"].mean() * 100
        target_pct = result_df["target_hit"].mean() * 100
        stop_pct = result_df["stop_hit"].mean() * 100

        print("Total FVG:", total)
        print("Retraced %:", round(retrace_pct, 2))
        print("Target %:", round(target_pct, 2))
        print("Stop %:", round(stop_pct, 2))

        print("\n===== GAP SIZE PERFORMANCE =====")

        result_df["gap_bucket"] = pd.cut(result_df["gap_size"], 5)

        gap_stats = result_df.groupby("gap_bucket").agg(
            setups=("gap_size", "count"),
            win_rate=("win", "mean"),
            loss_rate=("loss", "mean"),
            avg_gap=("gap_size", "mean"),
            avg_ob=("ob_size", "mean")
        )

        gap_stats["win_rate"] *= 100
        gap_stats["loss_rate"] *= 100

        print(gap_stats)

        print("\n===== OB SIZE PERFORMANCE =====")

        result_df["ob_bucket"] = pd.cut(result_df["ob_size"], 5)

        ob_stats = result_df.groupby("ob_bucket").agg(
            setups=("ob_size", "count"),
            win_rate=("win", "mean"),
            loss_rate=("loss", "mean")
        )

        ob_stats["win_rate"] *= 100
        ob_stats["loss_rate"] *= 100

        print(ob_stats)

        print("\n===== GAP / OB RATIO =====")

        result_df["ratio_bucket"] = pd.cut(result_df["gap_ob_ratio"], 5)

        ratio_stats = result_df.groupby("ratio_bucket").agg(
            setups=("gap_ob_ratio", "count"),
            win_rate=("win", "mean"),
            loss_rate=("loss", "mean")
        )

        ratio_stats["win_rate"] *= 100
        ratio_stats["loss_rate"] *= 100

        print(ratio_stats)

        print("\n===== WIN RATE BY HOUR =====")

        hour_stats = result_df.groupby("hour").agg(
            trades=("win", "count"),
            win_rate=("win", "mean"),
            loss_rate=("loss", "mean")
        )

        hour_stats["win_rate"] *= 100
        hour_stats["loss_rate"] *= 100

        print(hour_stats)

        # result_df.to_csv(f"fvg_backtest_{date}.csv", index=False)
        # print("\nResults saved to CSV")

        return result_df


# ----------------------------------------
# Usage
# ----------------------------------------

bt = FVGBacktester(
    security_id=13,
    hardtoken="eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJkaGFuIiwicGFydG5lcklkIjoiIiwiZXhwIjoxNzczNTY4NjU4LCJpYXQiOjE3NzM0ODIyNTgsInRva2VuQ29uc3VtZXJUeXBlIjoiU0VMRiIsIndlYmhvb2tVcmwiOiIiLCJkaGFuQ2xpZW50SWQiOiIxMTA0ODc4OTg5In0.Xi66NUYMY8BlyJoqlltKedNZGA5Ox3HYy8fANow2XP0FqlMnWnGx3725OsCgQFH7mTUZXHyOANGRLOTDYWidYA"
)

# df = bt.run("2025-03-12")

start_date = datetime(2026, 1, 1)
end_date = datetime(2026, 3, 14)

all_results = []

current = start_date

while current <= end_date:

    # skip weekends
    if current.weekday() >= 5:
        current += timedelta(days=1)
        continue

    date_str = current.strftime("%Y-%m-%d")

    print(f"\nRunning backtest for {date_str}")

    try:
        df = bt.run(date_str)

        # skip empty days
        if df is not None and not df.empty:
            all_results.append(df)

    except Exception as e:
        print("Error:", e)

    # throttle API to avoid DH-904
    time.sleep(0.8)

    current += timedelta(days=1)


# combine all results
if all_results:

    final_df = pd.concat(all_results, ignore_index=True)

    final_df.to_csv("fvg_backtest_quaterly.csv", index=False)

    print("\n===================================")
    print("MONTHLY BACKTEST COMPLETE")
    print("Total trades:", len(final_df))
    print("===================================")

else:
    print("No trades found")