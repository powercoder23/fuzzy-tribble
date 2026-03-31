import os
import sqlite3
import logging

class InstrumentMapper:
    instrument_tokens = {}
    def __init__(self, db_path=None):
        """
        Initialize the mapper with path to SQLite DB.
        If no path is given, assumes `instruments_simple.db` in same folder.
        """
        if db_path:
            self.db_path = db_path
        else:
            base = os.path.dirname(__file__)
            self.db_path = os.path.join(base, '..', "complete.db")
        self.instrument_tokens = {}

    def get_trading_symbol_from_key(self, instrument_key: str) -> str:
        """
        Fetch the trading symbol from the DB using instrument_key.

        Args:
            instrument_key (str): like 'NSE_EQ|INE742F01042'

        Returns:
            trading_symbol (str) or None if not found
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            query = "SELECT trading_symbol FROM instruments WHERE instrument_key = ?"
            cursor.execute(query, (instrument_key,))
            result = cursor.fetchone()
            conn.close()

            return result[0] if result else None
        except Exception as e:
            logging.warning(f"Failed to fetch trading symbol for {instrument_key}: {e}")
            return None



    def get_instrument_keys_from_symbols(self, fundamentally_good_stocks):
        """
        Map symbols to instrument keys using SQLite.
        Returns a dict identical in shape to the JSON-based method.
        Only includes records where exchange = 'NSE'.
        """
        # Clear any existing tokens
        self.instrument_tokens.clear()

        # Connect to SQLite
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        if not fundamentally_good_stocks:
            # No symbols provided: return empty dict
            print("✅ Mapped 0 instrument tokens for fundamentally good stocks (NSE only).")
            logging.info("Mapped {} instrument tokens for fundamentally good stocks (NSE only).")
            conn.close()
            return {}

        # Build SQL placeholders
        placeholders = ",".join("?" for _ in fundamentally_good_stocks)
        sql = f"""
            SELECT trading_symbol, instrument_key
              FROM instruments
             WHERE exchange = 'NSE'
               AND trading_symbol IN ({placeholders})
        """

        # Execute query
        cursor.execute(sql, fundamentally_good_stocks)
        rows = cursor.fetchall()
        conn.close()

        # Build the mapping
        self.instrument_tokens = {symbol: key for symbol, key in rows}

        # Log and print as before
        count = len(self.instrument_tokens)
        print(f"✅ Mapped {count} instrument tokens for fundamentally good stocks (NSE only).")
        logging.info(f"Mapped {self.instrument_tokens} instrument tokens for fundamentally good stocks (NSE only).")

        return self.instrument_tokens


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Example usage
    mapper = InstrumentMapper()
    good_stocks = [
        "JPYINR 61 CE 27 MAR 26",
        "RAJKSYN",
        "NONEXISTENT"
    ]
    tokens = mapper.get_instrument_keys_from_symbols(good_stocks)
    print("Result:", tokens)
