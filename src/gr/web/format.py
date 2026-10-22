"""Numbers on the screens: the only place values are rounded (R-13), Russian notation."""

from decimal import ROUND_HALF_EVEN, Decimal

NBSP = " "


def _grouped(value: Decimal, digits: int) -> str:
    rounded = value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_EVEN)
    if rounded == 0:
        rounded = abs(rounded)  # no «−0»
    return f"{rounded:,.{digits}f}".replace(",", NBSP).replace(".", ",").replace("-", "−")


def money(value: Decimal | None) -> str:
    """Whole roubles: the model is not that precise."""
    return "—" if value is None else f"{_grouped(value, 0)}{NBSP}₽"


def number(value: Decimal | None, digits: int = 1) -> str:
    return "—" if value is None else _grouped(value, digits)


def percent(value: Decimal | None) -> str:
    """A ratio as a percentage; None is a ratio that is not defined."""
    return "не определён" if value is None else f"{_grouped(value * 100, 0)}{NBSP}%"


def plain(value: Decimal) -> str:
    """For an input field: exact, no grouping and no trailing zeros, decimal comma."""
    return format(value.normalize(), "f").replace(".", ",")
