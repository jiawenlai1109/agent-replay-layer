import csv
import json
import os
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(BASE_DIR, "data", "orders.csv")
SUMMARY_PATH = os.path.join(BASE_DIR, "report", "summary.json")


class TestAggregate(unittest.TestCase):
    def setUp(self):
        with open(SUMMARY_PATH, newline="") as handle:
            self.summary = json.load(handle)
        self.rows = []
        with open(CSV_PATH, newline="") as handle:
            reader = csv.DictReader(handle)
            self.rows = list(reader)

    def test_region_counts_sum_to_12(self):
        total_count = self.summary["east"]["count"] + self.summary["west"]["count"]
        self.assertEqual(total_count, 12)

    def test_region_sums_match_csv_total(self):
        csv_total = 0.0
        for row in self.rows:
            csv_total += float(row["amount"])
        summary_total = self.summary["east"]["sum"] + self.summary["west"]["sum"]
        self.assertLessEqual(abs(summary_total - csv_total), 1e-6)


if __name__ == "__main__":
    unittest.main()
