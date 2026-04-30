"""
Schwab API client — fetches open positions from the user's brokerage account.

One-time auth setup (run on a Mac with a browser, NOT the headless Pi):
  python3 -c "
  import schwab
  schwab.auth.client_from_manual_flow(
      'YOUR_APP_KEY', 'YOUR_APP_SECRET',
      'https://flightsstockticker.duckdns.org/',
      '/tmp/schwab_token.json'
  )
  "
  Then: scp /tmp/schwab_token.json royman@Flights.local:/home/royman/schwab_token.json

At runtime on the Pi, get_client() loads the saved token directly — no browser needed.
"""

import logging

log = logging.getLogger(__name__)

DEFAULT_CALLBACK_URL = "https://flightsstockticker.duckdns.org/"


def get_client(app_key: str, app_secret: str, token_path: str, callback_url: str = DEFAULT_CALLBACK_URL):
    """
    Load an authenticated Schwab client from a saved token file.
    The token file must already exist (created via client_from_manual_flow on a Mac).
    Automatically refreshes expired access tokens using the saved refresh token.
    """
    try:
        import schwab
        return schwab.auth.client_from_token_file(token_path, app_key, app_secret)
    except FileNotFoundError:
        log.error(
            f"Schwab token file not found at {token_path}. "
            "Run the one-time auth flow on a Mac first (see module docstring)."
        )
        return None
    except Exception as e:
        log.error(f"Schwab client error: {e}")
        return None


def get_positions(
    app_key: str,
    app_secret: str,
    token_path: str,
    account_hashes: list[str],
    callback_url: str = DEFAULT_CALLBACK_URL,
) -> list[dict]:
    """
    Fetch all open equity/ETF positions across all provided accounts.
    Symbols held in multiple accounts are merged: market_value and day_change
    are summed; price and day_change_pct reflect the largest holding.

    Returns list of dicts:
    {
        symbol: str,
        asset_type: "ETF" | "EQUITY",
        price: float,
        day_change: float,       # combined $ change across all accounts today
        day_change_pct: float,   # % change (from largest-value account)
        market_value: float,     # combined market value across all accounts
    }
    """
    client = get_client(app_key, app_secret, token_path, callback_url)
    if client is None:
        return []

    try:
        import schwab
        # Aggregate by symbol across all accounts
        merged: dict[str, dict] = {}

        for account_hash in account_hashes:
            resp = client.get_account(
                account_hash,
                fields=[schwab.client.Client.Account.Fields.POSITIONS],
            )
            if resp.status_code != 200:
                log.warning(f"Schwab API error for account {account_hash[:8]}…: {resp.status_code}")
                continue

            positions = (
                resp.json()
                    .get("securitiesAccount", {})
                    .get("positions", [])
            )

            for pos in positions:
                instrument = pos.get("instrument", {})
                asset_type_raw = instrument.get("assetType", "")

                if asset_type_raw not in ("EQUITY", "COLLECTIVE_INVESTMENT"):
                    continue

                symbol = instrument.get("symbol", "")
                if not symbol:
                    continue

                asset_type = "ETF" if asset_type_raw == "COLLECTIVE_INVESTMENT" else "EQUITY"
                qty = pos.get("longQuantity", 1) or 1
                market_value = pos.get("marketValue", 0.0)
                price = market_value / qty
                day_change = pos.get("currentDayProfitLoss", 0.0)
                day_change_pct = pos.get("currentDayProfitLossPercentage", 0.0)

                if symbol in merged:
                    existing = merged[symbol]
                    # Keep price/pct from whichever holding has higher market value
                    if market_value > existing["market_value"]:
                        existing["price"] = price
                        existing["day_change_pct"] = day_change_pct
                    existing["market_value"] += market_value
                    existing["day_change"] += day_change
                else:
                    merged[symbol] = {
                        "symbol": symbol,
                        "asset_type": asset_type,
                        "price": price,
                        "day_change": day_change,
                        "day_change_pct": day_change_pct,
                        "market_value": market_value,
                    }

        # Sort by total market value descending
        return sorted(merged.values(), key=lambda x: x["market_value"], reverse=True)

    except Exception as e:
        log.error(f"Failed to fetch Schwab positions: {e}")
        return []
