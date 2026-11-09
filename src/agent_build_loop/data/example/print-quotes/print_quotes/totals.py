"""Total of a quote."""
from decimal import Decimal

from print_quotes.pricing import line_price


def total(lines) -> Decimal:
    """Sum the price of `(sheets, copies)` lines."""
    result = Decimal("0.00")
    for sheets, copies in lines:
        result += line_price(sheets, copies)
    return result
