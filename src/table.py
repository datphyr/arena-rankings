"""Shared table formatting utilities for CLI and Discord bot.

Provides Col + print_table for direct printing, and table_lines for
getting lines as a list (for embedding in code blocks).
"""

from datetime import datetime


class Col:
    """Column definition for print_table / table_lines."""
    __slots__ = ("header", "align", "key", "fmt")

    def __init__(self, header, align="<", key=None, fmt=None):
        self.header = header
        self.align = align
        self.key = key
        self.fmt = fmt


# ─── Formatting helpers (shared) ───────────────────────────────────────────────

def fmt_date(dt) -> str:
    if not dt or dt == datetime(1970, 1, 1):
        return "—"
    return dt.strftime("%Y-%m-%d, %H:%M")


def fmt_rating(r: float) -> str:
    return f"{r:.0f}" if r is not None else "—"


def fmt_wr(wins: int, matches: int) -> str:
    if not matches:
        return "—"
    return f"{wins / matches * 100:.1f}%"


def fmt_rd(v) -> str:
    return f"{v:.0f}" if v else "—"


def fmt_vol(v) -> str:
    return f"{v:.4f}" if v else "—"


def fmt_tier(t: str) -> str:
    """Capitalize tier for display: 'minor' -> 'Minor', '' -> '—'."""
    if not t:
        return "—"
    return t.capitalize()


# ─── Core table engine ─────────────────────────────────────────────────────────

def _col_value(col, row):
    if col.key:
        val = col.key(row)
    else:
        val = row
    if col.fmt:
        val = col.fmt(val)
    return str(val)


def _compute_widths(cols, rows):
    widths = []
    for col in cols:
        w = len(col.header)
        for row in rows:
            val = _col_value(col, row)
            w = max(w, len(val))
        widths.append(w)
    return widths


def table_lines(cols, rows, indent="  "):
    """Return table as list of lines (no trailing newline)."""
    widths = _compute_widths(cols, rows)
    sep = "  "

    parts = []
    for i, (col, w) in enumerate(zip(cols, widths)):
        parts.append(f"{{{i}:{col.align}{w}}}")
    fmt_str = sep.join(parts)

    lines = []
    lines.append(indent + fmt_str.format(*[c.header for c in cols]))
    lines.append(indent + sep.join("—" * w for w in widths))
    for row in rows:
        vals = [_col_value(col, row) for col in cols]
        lines.append(indent + fmt_str.format(*vals))
    return lines


def print_table(cols, rows, indent="  "):
    """Print table directly to stdout."""
    for line in table_lines(cols, rows, indent):
        print(line)