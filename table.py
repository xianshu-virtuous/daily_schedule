"""daily_schedule 的日志表格渲染。

日志是多行文本，控制台和文件都能原样显示，所以这里用框线字符画一张表：
比十几行缩进更像「一张表」，又不用去改框架的日志实现。

两个必须自己处理的点：

1. **宽度**：中英混排时不能按字符数算，中文占两列，否则框线会歪；
   这里按显示宽度（East Asian Width）计算。
2. **Rich 标记**：框架会先按 Rich markup 解析日志文本，所以单元格里的
   ``[`` 要转义成 ``\\[``，而粗体之类的样式标记会被框架自动从文件日志里剥掉。
"""

from __future__ import annotations

import unicodedata

__all__ = ["char_width", "display_width", "pad", "render_box", "truncate"]

#: 省略号，超出列宽时用。
ELLIPSIS = "…"


def char_width(char: str) -> int:
    """单个字符占的终端列数。

    Args:
        char: 单个字符。

    Returns:
        中文/全角字符为 2，其余为 1。
    """
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1


def display_width(text: str) -> int:
    """字符串的终端显示宽度。

    Args:
        text: 待测量的文本。

    Returns:
        显示宽度（列数）。
    """
    return sum(char_width(char) for char in text)


def truncate(text: str, width: int) -> str:
    """按显示宽度截断，超长时以省略号收尾。

    Args:
        text: 待截断文本。
        width: 允许的最大显示宽度。

    Returns:
        截断后的文本；未超长则原样返回。
    """
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text

    budget = width - display_width(ELLIPSIS)
    kept: list[str] = []
    used = 0
    for char in text:
        step = char_width(char)
        if used + step > budget:
            break
        kept.append(char)
        used += step
    return "".join(kept) + ELLIPSIS


def _escape(text: str) -> str:
    """转义 Rich markup 的左方括号，避免正文被当成样式标签。"""
    return text.replace("[", r"\[")


def pad(text: str, width: int) -> str:
    """按显示宽度右侧补空格。

    Args:
        text: 单元格文本。
        width: 目标显示宽度。

    Returns:
        补齐后的文本（已转义 Rich 标记）。
    """
    return _escape(text) + " " * max(width - display_width(text), 0)


def render_box(
    headers: list[str],
    rows: list[list[str]],
    *,
    caps: list[int] | None = None,
    row_styles: list[str | None] | None = None,
) -> str:
    """把二维数据渲染成框线表格。

    Args:
        headers: 表头。
        rows: 数据行，每行长度需与表头一致。
        caps: 每列的最大显示宽度。
        row_styles: 每行的 Rich 样式（如 ``"bold"``），``None`` 表示不套样式。

    Returns:
        多行表格文本。
    """
    columns = len(headers)
    if caps is None:
        caps = [80] * columns

    widths: list[int] = []
    for index in range(columns):
        cells = [headers[index]] + [
            row[index] for row in rows if len(row) > index
        ]
        widest = max((display_width(cell) for cell in cells), default=0)
        widths.append(min(widest, caps[index]))

    def rule(left: str, middle: str, right: str) -> str:
        return left + middle.join("─" * (width + 2) for width in widths) + right

    def render_row(cells: list[str], style: str | None = None) -> str:
        padded = [pad(cells[i] if i < len(cells) else "", widths[i]) for i in range(columns)]
        line = "│" + "│".join(f" {cell} " for cell in padded) + "│"
        return f"[{style}]{line}[/{style}]" if style else line

    lines = [rule("┌", "┬", "┐"), render_row(headers), rule("├", "┼", "┤")]
    for index, row in enumerate(rows):
        style = row_styles[index] if row_styles and index < len(row_styles) else None
        lines.append(render_row([truncate(cell, widths[i]) for i, cell in enumerate(row)], style))
    lines.append(rule("└", "┴", "┘"))
    return "\n".join(lines)
