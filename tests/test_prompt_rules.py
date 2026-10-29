import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import parity  # noqa: E402


class PromptRuleTests(unittest.TestCase):
    def test_rules_15_and_16_are_present(self):
        self.assertIn("15. INSPECT ... TALLYING", parity.SYSTEM)
        self.assertIn("16. The V in a PICTURE is an implied decimal point", parity.SYSTEM)
        self.assertIn("1200000.00", parity.SYSTEM)

    def test_rules_are_numbered_without_gaps(self):
        import re
        nums = [int(n) for n in re.findall(r"(?m)^(\d+)\. ", parity.SYSTEM)]
        self.assertEqual(nums, sorted(nums))
        self.assertEqual(nums[-1], 16)


if __name__ == "__main__":
    unittest.main()
