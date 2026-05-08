"""
Yahoo Finance quote fetcher — free, no API key required.
Uses the public v8 chart endpoint (one request per symbol).
"""

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; flight-clock/1.0)",
    "Accept": "application/json",
}


def _fetch_one(symbol: str) -> dict | None:
    url = _CHART_URL.format(symbol=symbol)
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        log.warning(f"[stock/yahoo] {symbol}: network error: {e}")
        return None
    except Exception as e:
        log.warning(f"[stock/yahoo] {symbol}: {e}")
        return None

    try:
        result = data["chart"]["result"][0]
        meta = result["meta"]
    except (KeyError, IndexError, TypeError):
        log.warning(f"[stock/yahoo] {symbol}: unexpected response shape")
        return None

    price = meta.get("regularMarketPrice", 0.0)
    prev_close = meta.get("chartPreviousClose") or meta.get("previousClose", price)
    day_change = price - prev_close
    day_change_pct = (day_change / prev_close * 100.0) if prev_close else 0.0

    instrument_type = meta.get("instrumentType", "EQUITY").upper()
    asset_type = "ETF" if instrument_type == "ETF" else "EQUITY"

    return {
        "symbol": symbol.upper(),
        "asset_type": asset_type,
        "price": price,
        "day_change": day_change,
        "day_change_pct": day_change_pct,
        "market_value": price,
    }


def get_quotes(symbols: list[str]) -> list[dict]:
    """
    Fetch current quotes for the given symbols from Yahoo Finance.
    Returns list of dicts in the same format as schwab_client.get_positions().
    Symbols that fail silently fall back to a zero-price stub.
    """
    results = []
    for sym in symbols:
        entry = _fetch_one(sym)
        if entry is None:
            entry = {
                "symbol": sym.upper(),
                "asset_type": "EQUITY",
                "price": 0.0,
                "day_change": 0.0,
                "day_change_pct": 0.0,
                "market_value": 0.0,
            }
        results.append(entry)
    return results
