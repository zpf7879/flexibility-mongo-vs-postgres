#!/usr/bin/env python3

"""
Change v2 on MongoDB: hobbies need structure.

After alter_model.py (v1), hobbies are an array of strings, e.g.
    "hobbies": ["cycling", "reading"]
Now each hobby needs a level and a start year:
    "hobbies": [{"name": "cycling", "level": "advanced", "since": 2019}, ...]

There is no schema change to run. This script converts about half of the
employees that have hobbies to the new shape and leaves the rest as plain
strings, so the collection holds BOTH shapes at once -- exactly the state
a gradual, in-place migration passes through. microservice_two.py handles
both shapes, so it keeps working throughout without a restart.

This is not free: any code that reads hobbies must accept both shapes until
every document is converted. But there's no DDL, no new table, no copy that
must finish before reads can switch, and no lock on the collection.

Usage:
  ./alter_model_v2.py                # convert about half of the documents
  ./alter_model_v2.py --convert-all  # convert every remaining string hobby

Compare ../postgres/alter_model_v2.py, the PostgreSQL version.
"""

import random
import sys

import pymongo
from pymongo import UpdateOne

import demo_settings

LEVELS = ["casual", "intermediate", "advanced"]


def convert_sample(collection):
    # Employees whose hobbies are still plain strings
    with_strings = list(collection.find({"hobbies": {"$type": "string"}}, {"emp_no": 1, "hobbies": 1}))
    sample = random.sample(with_strings, len(with_strings) // 2)

    updates = []
    for doc in sample:
        hobbies = [
            {"name": h, "level": random.choice(LEVELS), "since": 2000 + random.randrange(26)}
            if isinstance(h, str) else h
            for h in doc["hobbies"]
        ]
        # One atomic $set per document: the whole list changes shape at once
        updates.append(UpdateOne({"_id": doc["_id"]}, {"$set": {"hobbies": hobbies}}))

    if updates:
        collection.bulk_write(updates)
    return len(updates)


def convert_all(collection):
    # Convert every remaining string hobby in one server-side update.
    # Strings become {name} objects with no details yet; objects are kept.
    result = collection.update_many(
        {"hobbies": {"$type": "string"}},
        [{"$set": {"hobbies": {"$map": {
            "input": "$hobbies",
            "as": "h",
            "in": {"$cond": [
                {"$eq": [{"$type": "$$h"}, "string"]},
                {"name": "$$h"},
                "$$h",
            ]},
        }}}}],
    )
    return result.modified_count


def report_shapes(collection):
    strings = collection.count_documents({"hobbies": {"$type": "string"}})
    objects = collection.count_documents({"hobbies": {"$elemMatch": {"name": {"$exists": True}}}})
    print(f"  Employees with hobbies as plain strings: {strings}")
    print(f"  Employees with hobbies as objects:       {objects}")


if __name__ == "__main__":
    try:
        conn = pymongo.MongoClient(demo_settings.URI_STRING)
        print("Connected to MongoDB")

        db = conn[demo_settings.DB_NAME]
        collection = db[demo_settings.COLLECTION_NAME]

        if collection.count_documents({"hobbies": {"$exists": True}}) == 0:
            print("ERROR: no employee has hobbies yet. Run alter_model.py (v1) first.")
            raise SystemExit(1)

        if "--convert-all" in sys.argv:
            print("\nConverting every remaining string hobby to an object ...")
            converted = convert_all(collection)
            print(f"  Converted {converted} documents in one update_many (no lock on the collection).")
        else:
            print("\nChange v2: hobbies need structure ({name, level, since}).")
            print("No schema change needed -- converting about half of the employees in place ...")
            converted = convert_sample(collection)
            print(f"  Converted {converted} documents, one atomic $set each.")

        print("\nShapes now in the collection:")
        report_shapes(collection)
        print("\nmicroservice_two.py reads both shapes, so it keeps running without a restart.")
        print("Operation completed successfully!!!")

    except pymongo.errors.ConnectionFailure as e:
        print("Could not connect to MongoDB: %s" % e)
    conn.close()
