import unittest
from decimal import Decimal

from print_quotes.totals import total


class TotalTests(unittest.TestCase):
    def test_empty_quote(self):
        self.assertEqual(total([]), Decimal("0.00"))

    def test_two_lines(self):
        self.assertEqual(total([(10, 2), (250, 2)]), Decimal("56.40"))


if __name__ == "__main__":
    unittest.main()
