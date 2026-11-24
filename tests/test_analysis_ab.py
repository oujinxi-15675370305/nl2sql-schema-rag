"""analysis_ab 覆盖率辅助函数的测试：重点钉住 Unicode 负号。"""
from __future__ import annotations

import unittest

from scripts.analysis_ab import answer_numbers


class AnswerNumbersTest(unittest.TestCase):
    def test_unicode_minus_is_parsed_as_negative(self) -> None:
        # C8 的环比原文：−0.1627 / −0.1292 / −0.3679（U+2212）
        values = answer_numbers("2026-08 −0.1627；2026-09 −0.1292；2026-11 −0.3679")
        self.assertIn(-0.1627, values)
        self.assertIn(-0.1292, values)
        self.assertIn(-0.3679, values)
        self.assertNotIn(0.1627, values)

    def test_unicode_minus_percent_and_fullwidth(self) -> None:
        values = answer_numbers("环比 −12.5%，另一个 －3.25，ASCII -0.5")
        self.assertIn(-0.125, values)
        self.assertIn(-3.25, values)
        self.assertIn(-0.5, values)

    def test_ascii_negative_still_works(self) -> None:
        self.assertIn(-1.5, answer_numbers("差额 -1.5"))


if __name__ == "__main__":
    unittest.main()
