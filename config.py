import os
from dotenv import load_dotenv
from pathlib import Path

# Load environment variables
load_dotenv()

class Config:
    # Dhan Credentials
    DHAN_CLIENT_ID = os.getenv('DHAN_CLIENT_ID')
    DHAN_PIN = os.getenv('DHAN_PIN')
    DHAN_TOTP_SECRET = os.getenv('DHAN_TOTP_SECRET')
    DHAN_MOBILE = os.getenv('DHAN_MOBILE')
    
    # Telegram
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
    
    # Scanner Config
    INSTRUMENT_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
    HTF_INTERVAL = int(os.getenv('HTF_INTERVAL', '60'))
    LTF_INTERVAL = int(os.getenv('LTF_INTERVAL', '15'))
    LOOKBACK_DAYS = int(os.getenv('LOOKBACK_DAYS', '10'))
    MAX_SYMBOLS_PER_SCAN = int(os.getenv('MAX_SYMBOLS_PER_SCAN', '100'))

    SCREENER_URL = "https://www.screener.in/screens/3528566/fvg-screener/"
    
    # Paths
    BASE_DIR = Path('/app')
    TOKEN_DIR = BASE_DIR / 'data' / 'tokens'
    SIGNALS_DIR = BASE_DIR / 'data' / 'signals'
    LOGS_DIR = BASE_DIR / 'logs'
    
    # Token file
    TOKEN_FILE = TOKEN_DIR / 'access_token.json'
    
    @classmethod
    def ensure_dirs(cls):
        """Ensure all required directories exist"""
        cls.TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        cls.SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
        cls.LOGS_DIR.mkdir(parents=True, exist_ok=True)