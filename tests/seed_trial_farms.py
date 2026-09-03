"""Seed / remove temporary trialing farms for the Trial Nudge Banner UI test.
Usage: python seed_trial_farms.py seed | clean
"""
import sys
from datetime import datetime, timedelta, timezone
from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
db = MongoClient(env["MONGO_URL"])[env["DB_NAME"]]
SLUGS = ["test-trial-soft", "test-trial-expired"]

if sys.argv[1] == "seed":
    now = datetime.now(timezone.utc)
    for slug, days in [("test-trial-soft", 26), ("test-trial-expired", 31)]:
        db["farms"].update_one({"slug": slug}, {"$set": {
            "slug": slug, "name": f"TEST_{slug}", "ownerEmail": "appcovi2026@gmail.com",
            "subscriptionStatus": "trialing",
            "trialStartedAt": now - timedelta(days=days),
            "trialExpiresAt": now - timedelta(days=days) + timedelta(days=30),
            "createdAt": now,
        }}, upsert=True)
    print("seeded", SLUGS)
else:
    print(db["farms"].delete_many({"slug": {"$in": SLUGS}}).deleted_count, "removed")
