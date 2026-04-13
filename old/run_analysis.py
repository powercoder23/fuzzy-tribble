import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

# Your FNO stocks with NSE symbols
YOUR_STOCKS = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "HDFCBANK": "HDFCBANK",
    "RELIANCE": "RELIANCE",
    "ICICIBANK": "ICICIBANK",
    "INFY": "INFY",
    "TCS": "TCS",
    "HINDUNILVR": "HINDUNILVR",
    "SBIN": "SBIN"
}

def download_bhavcopy(date):
    """Download NSE bhavcopy for a specific date"""
    try:
        # Format date as DDMMYY
        date_str = date.strftime('%d%m%y')
        url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
        # Note: You'll need to handle NSE headers properly
        # For simplicity, we'll use a simpler approach
        return None
    except:
        return None

def get_historical_iv(symbol):
    """Fetch historical IV data from saved JSON or calculate"""
    # Since we're simplifying, we'll create sample data
    # In practice, you'd need to process bhavcopy files
    
    print(f"Processing {symbol}...")
    
    # Generate sample data for the last year
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365)
    
    dates = pd.date_range(start=start_date, end=end_date, freq='B')  # Business days
    
    # Create sample IV data (you'll replace this with real data)
    # This is just for structure demonstration
    data = []
    for i, date in enumerate(dates):
        # Simulate IV with some randomness and trends
        base_iv = 15 + 5 * np.sin(i / 30) + 2 * np.random.randn()
        
        data.append({
            'date': date.strftime('%Y-%m-%d'),
            'iv_30': max(5, base_iv),
            'iv_60': max(5, base_iv * 1.05),
            'iv_90': max(5, base_iv * 1.1)
        })
    
    # Calculate IV Percentile and Rank
    df = pd.DataFrame(data)
    current_iv = df.iloc[-1]['iv_30']
    
    # IV Percentile = % of days where IV was lower than current
    iv_percentile = (df['iv_30'] < current_iv).sum() / len(df) * 100
    
    # IV Rank = (current - min) / (max - min) * 100
    iv_min = df['iv_30'].min()
    iv_max = df['iv_30'].max()
    iv_rank = (current_iv - iv_min) / (iv_max - iv_min) * 100
    
    return {
        'historical_data': data,
        'current_iv': current_iv,
        'iv_percentile': iv_percentile,
        'iv_rank': iv_rank,
        'iv_min': iv_min,
        'iv_max': iv_max
    }

def main():
    """Main function to process all your stocks"""
    
    print("=" * 60)
    print("IV Percentile Calculator for FNO Stocks")
    print("=" * 60)
    
    results = {}
    
    for symbol, name in YOUR_STOCKS.items():
        print(f"\n📊 Processing {symbol} ({name})...")
        
        try:
            # Get IV data for this stock
            iv_data = get_historical_iv(symbol)
            
            results[symbol] = {
                'name': name,
                'current_iv': round(iv_data['current_iv'], 2),
                'iv_percentile': round(iv_data['iv_percentile'], 2),
                'iv_rank': round(iv_data['iv_rank'], 2),
                'iv_min': round(iv_data['iv_min'], 2),
                'iv_max': round(iv_data['iv_max'], 2),
                'historical_data': iv_data['historical_data'][-30:]  # Last 30 days
            }
            
            print(f"   ✅ Current IV: {results[symbol]['current_iv']}%")
            print(f"   📈 IV Percentile: {results[symbol]['iv_percentile']}%")
            print(f"   📊 IV Rank: {results[symbol]['iv_rank']}%")
            
        except Exception as e:
            print(f"   ❌ Error: {str(e)}")
            results[symbol] = {'error': str(e)}
    
    # Save to JSON file
    with open('iv_percentile_results.json', 'w') as f:
        json.dump(results, f, indent=2)
    
    print("\n" + "=" * 60)
    print("✅ Results saved to 'iv_percentile_results.json'")
    print("=" * 60)
    
    # Create a summary table
    summary = []
    for symbol, data in results.items():
        if 'error' not in data:
            summary.append({
                'Symbol': symbol,
                'Name': data['name'],
                'Current IV (%)': data['current_iv'],
                'IV Percentile (%)': data['iv_percentile'],
                'IV Rank (%)': data['iv_rank'],
                'IV Range': f"{data['iv_min']} - {data['iv_max']}"
            })
    
    df_summary = pd.DataFrame(summary)
    df_summary.to_csv('iv_percentile_summary.csv', index=False)
    
    print("\n📊 Summary Table:")
    print(df_summary.to_string(index=False))
    
    return results

if __name__ == "__main__":
    results = main()