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
        
    def get_valid_token(self, force_refresh=False):
        """Get valid token - either from file or generate new one"""
        return self.refresh_if_needed(force_refresh=force_refresh)
    
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
    
    def _is_token_valid(self, token_data, min_remaining_seconds=300):
        """Check if token is still valid"""
        if not token_data or 'expires_at' not in token_data:
            return False
        if 'accessToken' not in token_data and 'access_token' not in token_data:
            logger.info("Saved token data missing access token")
            return False

        expires_at = datetime.fromtimestamp(token_data['expires_at'])
        now = datetime.now()
        remaining = (expires_at - now).total_seconds()

        if now < expires_at and remaining > min_remaining_seconds:
            return True

        logger.info("Token expired or expiring soon (%s seconds remaining)", int(remaining))
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
    
    def refresh_if_needed(self, force_refresh=False):
        """Force refresh if token is near expiry or refresh is explicitly requested"""
        token_data = self._load_token()

        if token_data and self._is_token_valid(token_data):
            logger.info("Reusing saved token for scheduled run")
            return token_data.get('accessToken') or token_data.get('access_token')

        if force_refresh:
            logger.info("Forced token refresh requested")
            try:
                return self._generate_new_token()
            except Exception as exc:
                logger.warning(
                    "Forced token refresh failed; falling back to existing token if still valid: %s",
                    exc,
                )
                if token_data and self._is_token_valid(token_data, min_remaining_seconds=10):
                    return token_data.get('accessToken') or token_data.get('access_token')
                raise

        if token_data and self._is_token_valid(token_data, min_remaining_seconds=10):
            logger.info("Using saved token even though it is expiring soon")
            return token_data.get('accessToken') or token_data.get('access_token')

        logger.info("No reusable token found for scheduled run; generating a new token")
        try:
            return self._generate_new_token()
        except Exception as exc:
            logger.warning(
                "Token generation failed; using existing saved token if still valid: %s",
                exc,
            )
            if token_data and self._is_token_valid(token_data, min_remaining_seconds=10):
                return token_data.get('accessToken') or token_data.get('access_token')
            raise
