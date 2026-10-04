"""Decimal helpers. All money is handled as Decimal, never float."""

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0")
HUNDRED = Decimal("100")


def q2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def q2_vat(value: Decimal, favour_lower: bool) -> Decimal:
    """Round VAT to the nearest cent; an exact half cent goes the taxpayer's way (VAT Act s71).

    `favour_lower` is True for tax the taxpayer pays (output tax) and False for
    tax they claim (input tax).
    """
    lo, hi = value.quantize(CENT, ROUND_FLOOR), value.quantize(CENT, ROUND_CEILING)
    if value - lo != hi - value:
        return lo if value - lo < hi - value else hi
    return lo if favour_lower else hi


def split_gross(gross: Decimal, rate: Decimal, favour_lower: bool = True) -> tuple[Decimal, Decimal]:
    """Split a VAT-inclusive amount into (net, vat) with the tax fraction rate / (100 + rate) (s9(2))."""
    vat = q2_vat(gross * rate / (HUNDRED + rate), favour_lower)
    return gross - vat, vat


def vat_on_net(net: Decimal, rate: Decimal, favour_lower: bool = True) -> tuple[Decimal, Decimal]:
    """VAT on a VAT-exclusive amount. Returns (net, vat)."""
    return net, q2_vat(net * rate / HUNDRED, favour_lower)


def fmt(value, places: int = 2) -> str:
    if value is None:
        return ""
    value = Decimal(value)
    if value < 0:
        return f"({abs(value):,.{places}f})"
    return f"{value:,.{places}f}"
