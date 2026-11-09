"""Price of one quote line."""
from decimal import ROUND_HALF_UP, Decimal

PRICE_PER_SHEET = Decimal("0.12")
BULK_THRESHOLD = 500
BULK_DISCOUNT = Decimal("0.10")
CENT = Decimal("0.01")


def line_price(sheets: int, copies: int) -> Decimal:
    """Price for `copies` copies of a `sheets`-sheet job; 10% off from 500 sheets."""
    if sheets < 1 or copies < 1:
        raise ValueError("sheets and copies must be at least 1")
    count = sheets * copies
    price = PRICE_PER_SHEET * count
    if count >= BULK_THRESHOLD:
        price = price * (1 - BULK_DISCOUNT)
    return price.quantize(CENT, rounding=ROUND_HALF_UP)
