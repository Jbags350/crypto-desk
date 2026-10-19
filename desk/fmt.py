"""Small helpers for printing numbers in the console tools."""

from __future__ import annotations


def fmt_price(p: float | None) -> str:
    if p is None:
        return "-"
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:,.4f}"
    return f"{p:.6f}"


def fmt_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def fmt_num(v: float | None, spec: str = ".1f") -> str:
    return "-" if v is None else format(v, spec)


def shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"
