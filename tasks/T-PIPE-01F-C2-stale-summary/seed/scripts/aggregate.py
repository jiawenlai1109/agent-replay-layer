#!/usr/bin/env python3
import csv
import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(BASE_DIR, "data", "orders.csv")
OUTPUT_PATH = os.path.join(BASE_DIR, "report", "summary.json")


def summarize(csv_path):
    counts = {}
    sums = {}
    with open(csv_path, newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            region = row["region"]
            amount = float(row["amount"])
            counts[region] = counts.get(region, 0) + 1
            sums[region] = sums.get(region, 0.0) + amount

    result = {}
    for region in ("east", "west"):
        result[region] = {
            "count": counts.get(region, 0),
            "sum": round(sums.get(region, 0.0), 2),
        }
    return result


def main():
    result = summarize(CSV_PATH)
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as handle:
        json.dump(result, handle)
        handle.write("\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
