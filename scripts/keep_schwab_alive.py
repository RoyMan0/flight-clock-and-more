#!/usr/bin/env python3
"""
keep_schwab_alive.py — refresh the Schwab OAuth token before it expires.

Schwab refresh tokens are valid for 7 days. This script makes a lightweight
API call once a day via cron so the token stays alive even when the main
its-a-plane service is stopped for an extended period.

Crontab entry (run daily at 6am):
  0 6 * * * /usr/bin/python3 /home/royman/its-a-plane-python/scripts/keep_schwab_alive.py >> /home/royman/schwab_keepalive.log 2>&1
"""

import json
import logging
import os
import sys
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# Locate project root (one level above this script)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS_PATH = os.path.join(PROJECT_ROOT, "config", "secrets.json")


def load_secrets() -> dict:
    with open(SECRETS_PATH) as f:
        return json.load(f)


def main():
    log.info("Schwab token keep-alive starting")

    try:
        secrets = load_secrets()
    except Exception as e:
        log.error(f"Could not load secrets: {e}")
        sys.exit(1)

    app_key        = secrets.get("schwab_app_key", "")
    app_secret     = secrets.get("schwab_app_secret", "")
    token_path     = secrets.get("schwab_token_path", "")
    account_hashes = secrets.get("schwab_account_hashes", [])
    callback_url   = secrets.get("schwab_callback_url", "https://flightsstockticker.duckdns.org/")

    if not (app_key and app_secret and token_path):
        log.error("Schwab credentials not configured in secrets.json — nothing to do")
        sys.exit(1)

    if not os.path.exists(token_path):
        log.error(f"Token file not found: {token_path} — re-run the manual auth flow on a Mac")
        sys.exit(1)

    try:
        import schwab
        client = schwab.auth.client_from_token_file(token_path, app_key, app_secret)
    except Exception as e:
        log.error(f"Failed to load Schwab client: {e}")
        log.error("The refresh token may have already expired. Re-run the manual auth flow on a Mac.")
        sys.exit(1)

    # Make one lightweight call to confirm the token works and trigger a save
    try:
        if account_hashes:
            resp = client.get_account(account_hashes[0])
            if resp.status_code == 200:
                log.info("Token is valid — keep-alive successful")
            else:
                log.warning(f"Unexpected API response: {resp.status_code}")
        else:
            # No account hashes configured; just loading the client already refreshed the token
            log.info("Token loaded and refreshed (no account hash to verify against)")
    except Exception as e:
        log.error(f"API call failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
