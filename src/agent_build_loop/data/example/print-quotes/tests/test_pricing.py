import unittest
from decimal import Decimal

from print_quotes.pricing import line_price


class LinePriceTests(unittest.TestCase):
    def test_small_job(self):
        self.assertEqual(line_price(10, 2), Decimal("2.40"))

    def test_bulk_discount(self):
        self.assertEqual(line_price(250, 2), Decimal("54.00"))

    def test_rejects_zero(self):
        with self.assertRaises(ValueError):
            line_price(0, 1)


if __name__ == "__main__":
    unittest.main()
