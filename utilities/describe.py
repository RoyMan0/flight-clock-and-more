"""
Helpers for BasePlugin.describe(): turn panel colours into JSON-safe [r, g, b].
"""


def rgb(colour):
    """graphics.Color, (r, g, b) or None → [r, g, b] (or None)."""
    if colour is None:
        return None
    if hasattr(colour, "red"):
        return [int(colour.red), int(colour.green), int(colour.blue)]
    return [int(c) for c in tuple(colour)[:3]]
