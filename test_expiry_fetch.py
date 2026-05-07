#!/usr/bin/env python3
"""Diagnostic script to test Dhan expiry list API using TokenManager."""

import argparse
import json
from pathlib import Path

import requests

from config import Config
from token_manager import TokenManager
from dhanhq import DhanContext, dhanhq


def generate_token(force_refresh=False):
    manager = TokenManager()
    try:
        token = manager.refresh_if_needed(force_refresh=force_refresh)
        if force_refresh:
            print("[INFO] Forced fresh token generation")
    except Exception as exc:
        print("[WARN] Forced token refresh failed:", exc)
        if force_refresh:
            print("[INFO] Falling back to saved token if available")
            token = manager.refresh_if_needed(force_refresh=False)
        else:
            raise
    print("[INFO] Token generated/validated successfully")
    print("[INFO] Client ID:", Config.DHAN_CLIENT_ID)
    print("[INFO] Token sample:", token[:12] + "...")
    return token


def print_saved_token_info():
    token_file = Path(Config.TOKEN_FILE)
    print("[INFO] Token file:", token_file)
    if not token_file.exists():
        print("[WARN] Token file does not exist")
        return

    token_data = json.loads(token_file.read_text())
    print("[INFO] Token file keys:", list(token_data.keys()))
    if "accessToken" in token_data:
        print("[INFO] accessToken present")
    if "access_token" in token_data:
        print("[INFO] access_token present")
    if "dhanClientId" in token_data:
        print("[INFO] saved dhanClientId", token_data.get("dhanClientId"))


def fetch_expiry_raw(security_id, segment, token):
    url = "https://api.dhan.co/v2/optionchain/expirylist"
    headers = {
        "Content-Type": "application/json",
        "access-token": token,
        "client-id": Config.DHAN_CLIENT_ID,
    }
    payload = {
        "UnderlyingScrip": int(security_id),
        "UnderlyingSeg": segment,
    }
    print("[INFO] Raw request payload:", payload)
    response = requests.post(url, json=payload, headers=headers, timeout=30)
    print("[INFO] HTTP status:", response.status_code)
    print("[INFO] Response text:", response.text)
    return response


def fetch_expiry_sdk(security_id, segment, token):
    print("[INFO] Calling Dhan SDK expiry_list()")
    ctx = DhanContext(Config.DHAN_CLIENT_ID, token)
    client = dhanhq(ctx)
    result = client.expiry_list(
        under_security_id=int(security_id),
        under_exchange_segment=segment,
    )
    print("[INFO] SDK result:", result)
    return result


def main():
    parser = argparse.ArgumentParser(description="Test Dhan expiry list API with TokenManager")
    parser.add_argument("--security-id", default=15332, type=int, help="Underlying security ID")
    parser.add_argument("--segment", default="NSE_FNO", help="Underlying segment (NSE_FNO, NSE_EQ, IDX_I)")
    parser.add_argument("--force-refresh", action="store_true", help="Force generation of a new access token")
    parser.add_argument("--raw-only", action="store_true", help="Only test the raw HTTP expiry list request")
    parser.add_argument("--sdk-only", action="store_true", help="Only test the SDK expiry list request")
    args = parser.parse_args()

    print_saved_token_info()
    token = generate_token(force_refresh=args.force_refresh)

    # if not args.sdk_only:
    #     print("\n=== RAW HTTP EXPIRY LIST TEST ===")
    expiry_result = fetch_expiry_raw(args.security_id, args.segment, token)

    
    if expiry_result.status_code == 200:
        data = expiry_result.json()

        expiries = data.get("data", [])

        if not expiries:
            print("[WARN] No expiries found")
            return

        first_expiry = expiries[0]

        print("[INFO] Using expiry:", first_expiry)

        print("\n=== OPTION CHAIN TEST ===")

        fetch_option_chain_raw(
            args.security_id,
            first_expiry,
            token,
        )

def fetch_option_chain_raw(security_id, expiry, token):
    url = "https://api.dhan.co/v2/optionchain"

    headers = {
        "Content-Type": "application/json",
        "access-token": token,
        "client-id": Config.DHAN_CLIENT_ID,
    }

    payload = {
        "UnderlyingScrip": int(security_id),
        "UnderlyingSeg": "NSE_FNO",
        "Expiry": expiry,
    }

    print("[INFO] Option chain payload:", payload)

    response = requests.post(
        url,
        json=payload,
        headers=headers,
        timeout=30,
    )

    print("[INFO] HTTP status:", response.status_code)
    print("[INFO] Response text:", response.text)

    return response

def fetch_option_chain_sdk(security_id, expiry, token):
    print("[INFO] Calling Dhan SDK option_chain()")

    ctx = DhanContext(Config.DHAN_CLIENT_ID, token)
    client = dhanhq(ctx)

    result = client.option_chain(
        under_security_id=int(security_id),
        under_exchange_segment="NSE_FNO",
        expiry=expiry,
    )

    print("[INFO] SDK result:", result)

    return result

if __name__ == "__main__":
    main()
