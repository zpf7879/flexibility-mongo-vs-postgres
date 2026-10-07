#!/usr/bin/env python3

import pymongo
import datetime
import random
import demo_settings
from pymongo import UpdateOne

def alter_model(collection):
    pipeline = [{'$sample': {'size': demo_settings.NUM_SAMPLING}}]
    docs = collection.aggregate(pipeline)

    updates = []
    for doc in docs:
        new_doc = add_newfields(doc["emp_no"])
        update = UpdateOne({"emp_no": doc["emp_no"]}, {"$set": new_doc})
        updates.append(update)
    
    collection.bulk_write(updates)

def add_newfields(emp_no):
    # add new fields to employee document
    departments = ["Marketing", "Sales","Engineering", "Human Resources", "Finance", "Services", "Other"]
    titles = ["Director", "Manager","Senior Staff", "Staff", "Other"]
    hobbies = ["movies", "cycling", "singing", "running", "hiking", "photography", "reading", "sleeping"]
    base_birth_year = 1975
    employee={
            "emp_no": emp_no,
            "birth_date": datetime.datetime(int(base_birth_year + random.choice(range(15))),int(1 + random.choice(range(11))), int(1 + random.choice(range(28)))),
            "department" : random.choice(departments),
            "title" : random.choice(titles),
            "hobbies": []
            }

    for i in range(1 + int(1000 * random.random()) % 3) :
        employee["hobbies"].append(random.choice(hobbies))

    return employee

if __name__ == "__main__":
    try:
        conn = pymongo.MongoClient(demo_settings.URI_STRING)
        print("Connected to MongoDB")

        db = conn[demo_settings.DB_NAME]
        # username = db.command("connectionStatus")['authInfo']['authenticatedUsers'][0]['user']
        # collection = db[username]
        collection = db[demo_settings.COLLECTION_NAME]
        

        print("Adding fields to ", demo_settings.NUM_SAMPLING, " documents ...")
        alter_model(collection)

        # microservice_two.py filters on department, so index it -- the same
        # index the PostgreSQL version creates. No index is needed for hobbies:
        # they are embedded in each employee document, not in a separate table.
        print("Creating index on department ...")
        collection.create_index([("department", pymongo.ASCENDING)])

        print("Operation completed successfully!!!")

    except pymongo.errors.ConnectionFailure as e:
        print("Could not connect to MongoDB: %s" % e)
    conn.close()
