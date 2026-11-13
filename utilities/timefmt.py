"""
strftime that also works on Windows: %l (12-hour hour, space-padded, as glibc/BSD give it)
isn't in the Windows C library, which raises "Invalid format string". The desktop app runs
the same code on Windows, so clock formats go through here.
"""


def strftime(dt, fmt: str) -> str:
    if "%l" in fmt:
        fmt = fmt.replace("%l", f"{dt.hour % 12 or 12:2d}")
    return dt.strftime(fmt)
