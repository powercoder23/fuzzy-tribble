import os
import time
import logging
import requests
import pyotp
from urllib import parse as urlparse
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

try:
    from .config import Config
except ImportError:
    from config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

os.environ["SELENIUM_MANAGER_SKIP_DOWNLOAD"] = "1"


def _generate_totp(secret: str) -> str:
    return pyotp.TOTP(secret).now()


def get_upstox_access_token() -> str:
    """Automate Upstox OAuth2 login via Selenium + TOTP and return the access token."""
    api_key = Config.UPSTOX_API_KEY
    api_secret = Config.UPSTOX_API_SECRET
    redirect_uri = Config.UPSTOX_REDIRECT_URL
    mobile_no = Config.UPSTOX_MOBILE_NO
    totp_secret = Config.UPSTOX_TOTP_SECRET
    pin = Config.UPSTOX_PIN

    url = (
        f"https://api.upstox.com/v2/login/authorization/dialog"
        f"?client_id={api_key}&redirect_uri={redirect_uri}&response_type=code"
    )

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    # Debian package 'chromium' installs the browser at /usr/bin/chromium
    options.binary_location = os.getenv("CHROME_BIN", "/usr/bin/chromium")

    chromedriver_path = os.getenv("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")
    service = Service(chromedriver_path)
    driver = webdriver.Chrome(service=service, options=options)
    wait = WebDriverWait(driver, 10)

    try:
        logger.info("Navigating to Upstox login page...")
        driver.get(url)

        wait.until(EC.presence_of_element_located((By.XPATH, '//input[@type="text"]'))).send_keys(mobile_no)
        wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="getOtp"]'))).click()

        time.sleep(2)
        totp = _generate_totp(totp_secret)
        wait.until(EC.presence_of_element_located((By.XPATH, '//*[@id="otpNum"]'))).send_keys(totp)
        wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="continueBtn"]'))).click()

        time.sleep(2)
        wait.until(EC.presence_of_element_located((By.XPATH, '//*[@id="pinCode"]'))).send_keys(pin)
        wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="pinContinueBtn"]'))).click()

        time.sleep(3)
        token_url = driver.current_url
        driver.quit()

        parsed = urlparse.urlparse(token_url)
        code = urlparse.parse_qs(parsed.query).get("code", [None])[0]

        if not code:
            raise Exception("Failed to retrieve Upstox authorization code.")

        logger.info("Authorization code retrieved.")

        response = requests.post(
            "https://api.upstox.com/v2/login/authorization/token",
            headers={"accept": "application/json"},
            data={
                "code": code,
                "client_id": api_key,
                "client_secret": api_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )

        if response.status_code == 200:
            access_token = response.json().get("access_token")
            logger.info("Upstox access token retrieved successfully.")
            return access_token
        else:
            raise Exception(f"Upstox token exchange failed: {response.text}")

    except Exception as e:
        logger.error("Upstox login failed: %s", str(e))
        try:
            driver.save_screenshot("upstox_login_error.png")
            driver.quit()
        except Exception:
            pass
        raise
