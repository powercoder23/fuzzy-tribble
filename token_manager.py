import json
import pyotp
from datetime import datetime, timedelta
from dhanhq import DhanLogin
import logging
from pathlib import Path
try:
    from .config import Config
except ImportError:
    from config import Config

logger = logging.getLogger(__name__)

class TokenManager:
    def __init__(self):
        self.token_file = Config.TOKEN_FILE
        self.client_id = Config.DHAN_CLIENT_ID
        self.pin = Config.DHAN_PIN
        self.totp_secret = Config.DHAN_TOTP_SECRET
        
    def get_valid_token(self):
        """Get valid token - either from file or generate new one"""
        
        # Try to load existing token
        token_data = self._load_token()
        
        if token_data and self._is_token_valid(token_data):
            logger.info("Using existing valid token")
            # Check different possible key names
            if 'accessToken' in token_data:
                return token_data['accessToken']
            elif 'access_token' in token_data:
                return token_data['access_token']
            else:
                logger.error("No access token key found in saved data")
                return self._generate_new_token()
        
        # Generate new token
        logger.info("Generating new access token...")
        return self._generate_new_token()
    
    def _load_token(self):
        """Load token from file if exists"""
        try:
            if Path(self.token_file).exists():
                with open(self.token_file, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Error loading token: {e}")
        return None
    
    def _save_token(self, token_data):
        """Save token to file"""
        try:
            # Calculate expiry (midnight)
            now = datetime.now()
            midnight = datetime(now.year, now.month, now.day, 23, 59, 59)
            expires_at = midnight.timestamp()
            
            token_data['expires_at'] = expires_at
            token_data['created_at'] = now.timestamp()
            
            with open(self.token_file, 'w') as f:
                json.dump(token_data, f, indent=2)
            
            logger.info("Token saved successfully")
        except Exception as e:
            logger.error(f"Error saving token: {e}")
    
    def _is_token_valid(self, token_data):
        """Check if token is still valid"""
        if not token_data or 'expires_at' not in token_data:
            return False
        
        # Check if token expires at midnight
        expires_at = datetime.fromtimestamp(token_data['expires_at'])
        now = datetime.now()
        
        # Token is valid if not expired and not within 5 minutes of expiry
        if now < expires_at and (expires_at - now).seconds > 300:
            return True
        
        logger.info("Token expired or expiring soon")
        return False
    
    def _generate_new_token(self):
        """Generate new access token using TOTP"""
        try:
            # Initialize DhanLogin
            dhan_login = DhanLogin(self.client_id)
            
            # Generate TOTP
            totp = pyotp.TOTP(self.totp_secret).now()
            logger.info(f"Generated TOTP: {totp}")
            
            # Get access token
            response = dhan_login.generate_token(self.pin, totp)
            
            if not response:
                raise Exception("Empty response from token generation")
            
            logger.info(f"Token response keys: {response.keys()}")
            
            # Check for token in response (different possible keys)
            access_token = None
            if 'accessToken' in response:
                access_token = response['accessToken']
            elif 'access_token' in response:
                access_token = response['access_token']
            elif 'data' in response and isinstance(response['data'], dict):
                if 'accessToken' in response['data']:
                    access_token = response['data']['accessToken']
            
            if not access_token:
                raise Exception(f"No access token found in response: {response}")
            
            # Save token data
            self._save_token(response)
            
            logger.info("New access token generated successfully")
            return access_token
            
        except Exception as e:
            logger.error(f"Failed to generate token: {e}")
            raise
    
    def refresh_if_needed(self):
        """Force refresh if token is near expiry"""
        token_data = self._load_token()
        if not token_data or not self._is_token_valid(token_data):
            logger.info("No reusable token found for scheduled run; generating a new token")
            return self._generate_new_token()
        
        if 'accessToken' in token_data:
            logger.info("Reusing saved token for scheduled run")
            return token_data['accessToken']
        elif 'access_token' in token_data:
            logger.info("Reusing saved token for scheduled run")
            return token_data['access_token']
        else:
            logger.info("Saved token file is missing token value; generating a new token")
            return self._generate_new_token()
