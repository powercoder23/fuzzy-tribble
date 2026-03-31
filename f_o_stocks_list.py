from datetime import datetime, timedelta
import pandas as pd
import requests
import gzip
from io import BytesIO

day = 1
def get_stock_futures(day=1) -> list:
    """
    Downloads and extracts only stock futures ('FUTSTK') from NSE F&O contract file.

    Args:
        csv_url (str): Direct link to NSE_FO_contract_*.csv.gz

    Returns:
        pd.DataFrame: Filtered DataFrame with columns for symbol, contract ID, expiry, stock name, and lot size.
    """
    # get today's date
    today = datetime.today() - timedelta(days=day)


    # if weekend, roll back to Friday
    if today.weekday() == 5:       # Saturday
        today -= timedelta(days=1)
    elif today.weekday() == 6:     # Sunday
        today -= timedelta(days=2)

    # format as DDMMYYYY
    date_str = today.strftime("%d%m%Y")

    # build full URL
    csv_url = f"https://archives.nseindia.com/content/fo/NSE_FO_contract_{date_str}.csv.gz"
    print(f"Downloading stock futures from: {csv_url}")
    try:
        resp = requests.get(csv_url, headers={"User-Agent": "Mozilla/5.0"})
        with gzip.open(BytesIO(resp.content), 'rt') as f:
            df = pd.read_csv(f)
        # Clean and filter for 'FUTSTK' in 'FinInstrmNm' column
        df['FinInstrmNm_clean'] = df['FinInstrmNm'].astype(str).str.strip().str.upper()
        df_stock_futures = df[df['FinInstrmNm_clean'] == 'FUTSTK']
        # Select relevant columns
        final_df = df_stock_futures[['TckrSymb', 'FinInstrmId', 'XpryDt', 'StockNm', 'MinLot']]
        unique_symbols = sorted(final_df['TckrSymb'].unique())
        return unique_symbols
    except Exception as e:
        print(f"❌ Unable to load: {e}")
        return get_stock_futures(day=day + 1)  # try previous day recursively




# Example usage:
# if __name__ == "__main__":
#     csv_url = "https://archives.nseindia.com/content/fo/NSE_FO_contract_28072025.csv.gz"
#     stock_futures_df = get_stock_futures(csv_url)
#     print(stock_futures_df)
#     # If you want the list of symbols only:
#     symbols_list = sorted(stock_futures_df['TckrSymb'].unique())
#     print("Stock Futures (Symbols):", symbols_list)
