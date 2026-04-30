"""
Schwab API client — fetches open positions from the user's brokerage account.

Auth setup (one-time, run on a machine with a browser):
  python3 -c "from plugins.stock_ticker.schwab_client import setup_auth; setup_auth()"

Uses the schwab-py library (pip install schwab-py).
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)


def setup_auth(app_key: str, app_secret: str, token_path: str):
    """
    One-time OAuth2 browser flow. Saves token to token_path.
    Run this once on a machine with a browser; the Pi reads the saved token.
    """
    try:
        import schwab
        client = schwab.auth.easy_client(app_key, app_secret, token_path)
        log.info(f"Auth complete. Token saved to {token_path}")
        return client
    except Exception as e:
        log.error(f"Schwab auth failed: {e}")
        return None


def get_client(app_key: str, app_secret: str, token_path: str):
    """Return an authenticated Schwab client using saved token."""
    try:
        import schwab
        return schwab.auth.easy_client(app_key, app_secret, token_path)
    except Exception as e:
        log.error(f"Schwab client error: {e}")
        return None


def get_positions(app_key: str, app_secret: str, token_path: str, account_hash: str) -> list[dict]:
    """
    Fetch all open equity/ETF positions from the account.

    Returns list of dicts:
    {
        symbol: str,
        asset_type: "ETF" | "EQUITY" | "OTHER",
        price: float,
        day_change: float,       # $ change today
        day_change_pct: float,   # % change today (e.g. 2.34 = +2.34%)
        market_value: float,
    }
    """
    client = get_client(app_key, app_secret, token_path)
    if client is None:
        return []

    try:
        import schwab
        resp = client.get_account(
            account_hash,
            fields=[schwab.client.Client.Account.Fields.POSITIONS],
        )
        if resp.status_code != 200:
            log.error(f"Schwab API error: {resp.status_code}")
            return []

        data = resp.json()
        positions = (
            data.get("securitiesAccount", {})
                .get("positions", [])
        )

        results = []
        for pos in positions:
            instrument = pos.get("instrument", {})
            asset_type_raw = instrument.get("assetType", "")

            # Only show equities and ETFs (skip options, fixed income, etc.)
            if asset_type_raw not in ("EQUITY", "COLLECTIVE_INVESTMENT"):
                continue

            symbol = instrument.get("symbol", "")
            if not symbol:
                continue

            asset_type = "ETF" if asset_type_raw == "COLLECTIVE_INVESTMENT" else "EQUITY"

            price = pos.get("marketValue", 0) / max(pos.get("longQuantity", 1) or 1, 1)

            results.append({
                "symbol": symbol,
                "asset_type": asset_type,
                "price": price,
                "day_change": pos.get("currentDayProfitLoss", 0.0),
                "day_change_pct": pos.get("currentDayProfitLossPercentage", 0.0),
                "market_value": pos.get("marketValue", 0.0),
            })

        return results

    except Exception as e:
        log.error(f"Failed to fetch Schwab positions: {e}")
        return []
