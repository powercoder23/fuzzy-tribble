"""
iv_percentile_runner.py - Runner for IV Percentile Strategy
"""

import os
import sys
import time
import logging
from datetime import datetime, timedelta

import pytz

from iv_percentile_strategy import IVPercentileStrategy
from discount import DiscountedPremiumScanner
import notifications

IST = pytz.timezone("Asia/Kolkata")
logger = logging.getLogger(__name__)

def main():
    """Main runner for IV Percentile strategy."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler("logs/iv_percentile.log"),
            logging.StreamHandler()
        ]
    )
    
    logger.info("Starting IV Percentile Strategy")
    
    try:
        strategy = IVPercentileStrategy()
        scanner = DiscountedPremiumScanner()
        securities = scanner.fno_stocks
        
        logger.info(f"Loaded {len(securities)} securities")
        
        # Get current time
        now = datetime.now(IST)
        
        # Only run during market hours
        if now.time() < datetime.strptime("09:15", "%H:%M").time():
            logger.info("Market not open yet. Waiting...")
            time.sleep(60)
            return
        
        if now.time() > datetime.strptime("15:15", "%H:%M").time():
            logger.info("Market closed. Run EOD summary only.")
            send_eod_summary(strategy)
            return
        
        # Run scan
        logger.info("Scanning for IV percentile signals...")
        signals = strategy.scan_all(securities)
        
        if not signals:
            logger.info("No signals found today")
            notifications.notify("📊 IV Percentile: No signals found today")
            return
        
        logger.info(f"Found {len(signals)} signals")
        
        # Book trades
        for signal in signals:
            logger.info(f"Booking: {signal['symbol']} | {signal['strategy']} | Confidence {signal['confidence']:.0f}%")
            result = strategy.run_paper_trade(signal)
            logger.info(f"Result: {result}")
            time.sleep(1)
        
    except Exception as e:
        logger.exception(f"Runner failed: {e}")

def send_eod_summary(strategy):
    """Send end-of-day summary."""
    # Query today's trades from paper_trades.db
    import sqlite3
    from pathlib import Path
    
    db_path = Path("data/paper_trades.db")
    if not db_path.exists():
        return
    
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    today = datetime.now(IST).date().isoformat()
    
    cur.execute("""
        SELECT strategy, COUNT(*), SUM(realized_rupees), 
               SUM(CASE WHEN realized_rupees > 0 THEN 1 ELSE 0 END)
        FROM paper_trades 
        WHERE date = ? AND strategy LIKE 'IV_%'
        GROUP BY strategy
    """, (today,))
    
    rows = cur.fetchall()
    conn.close()
    
    if not rows:
        return
    
    msg = f"📊 IV PERCENTILE EOD SUMMARY\n{today}\n"
    msg += "─" * 30 + "\n"
    
    total_trades = 0
    total_pnl = 0
    total_wins = 0
    
    for strategy, count, pnl, wins in rows:
        msg += f"{strategy}: {count} trades | P&L ₹{pnl:+,.0f} | {wins} wins\n"
        total_trades += count
        total_pnl += pnl or 0
        total_wins += wins or 0
    
    if total_trades > 0:
        win_rate = total_wins / total_trades * 100
        msg += f"\nTotal: {total_trades} trades | P&L ₹{total_pnl:+,.0f} | Win rate {win_rate:.1f}%"
    
    notifications.notify(msg)

if __name__ == "__main__":
    main()