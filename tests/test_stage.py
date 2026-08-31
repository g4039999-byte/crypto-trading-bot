import unittest

from src.stage import classify_stage


class TestClassifyStage(unittest.TestCase):
    def test_unknown_when_age_is_none(self):
        self.assertEqual(classify_stage(None), "UNKNOWN")

    def test_early(self):
        self.assertEqual(classify_stage(0), "EARLY")
        self.assertEqual(classify_stage(15), "EARLY")

    def test_rising(self):
        self.assertEqual(classify_stage(15.01), "RISING")
        self.assertEqual(classify_stage(60), "RISING")

    def test_mature(self):
        self.assertEqual(classify_stage(60.01), "MATURE")
        self.assertEqual(classify_stage(240), "MATURE")

    def test_late(self):
        self.assertEqual(classify_stage(240.01), "LATE")
        self.assertEqual(classify_stage(10000), "LATE")


if __name__ == "__main__":
    unittest.main()
