"""Seed / clean 3 synthetic locked EOB snapshots on farm 'southridge' to exercise
the Best/Worst batch callout + coloured trend dots. Scores by construction: A=100, B=90, C=80.
Usage: python seed_accuracy_batches.py seed | clean
"""
import sys
from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
db = MongoClient(env["MONGO_URL"])[env["DB_NAME"]]
FARM = "southridge"
IDS = ["TEST_ACC_A", "TEST_ACC_B", "TEST_ACC_C"]
PRED = {"aveWeightKg": 2.5, "fcr": 1.5, "cfcr": 1.6, "cage": 1.0, "finishAge": 40, "grade": "A"}


def actual(mult):
    return {
        "aveWeight": round(2.5 * mult, 4), "fcr": round(1.5 * mult, 4),
        "cfcr": round(1.6 * mult, 4), "cageRating": round(1.0 * mult, 4),
        "correctedAge": 40, "actualAge": 40, "totalCaught": 40000, "totalLiveWeightKg": 100000,
    }


if sys.argv[1] == "seed":
    rows = [("TEST_ACC_A", "2026-06-01T00:00:00+00:00", 1.0),
            ("TEST_ACC_B", "2026-07-01T00:00:00+00:00", 1.1),
            ("TEST_ACC_C", "2026-08-01T00:00:00+00:00", 0.8)]
    for bid, locked, mult in rows:
        db["eob_snapshots"].update_one({"farmId": FARM, "batchIdentifier": bid}, {"$set": {
            "farmId": FARM, "batchIdentifier": bid, "lockedAt": locked,
            "fingerprint": {"predictedEob": PRED}, "report": actual(mult),
        }}, upsert=True)
    print("seeded", IDS)
else:
    print(db["eob_snapshots"].delete_many({"farmId": FARM, "batchIdentifier": {"$in": IDS}}).deleted_count, "removed")
