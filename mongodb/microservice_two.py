#!/usr/bin/env python3

import pymongo
import time
import random
import demo_settings


def describe(hobby):
    # Hobbies come in two shapes while documents are being converted:
    #   v1: "cycling"
    #   v2: {"name": "cycling", "level": "advanced", "since": 2019}
    # Reading both lets documents be converted gradually, with no downtime.
    if isinstance(hobby, str):
        return hobby
    if hobby.get("level") is None:
        return hobby["name"]
    return f"{hobby['name']} ({hobby['level']}, since {hobby['since']})"


if __name__ == "__main__":
    try:
        conn=pymongo.MongoClient(demo_settings.URI_STRING)

        print("Microservice Two - connected to MongoDB\n")

        db = conn[demo_settings.DB_NAME]
        # username = db.command("connectionStatus")['authInfo']['authenticatedUsers'][0]['user']
        # collection = db[username]
        collection = db[demo_settings.COLLECTION_NAME]

        # If this service is deployed before alter_model.py has run, no document
        # has a department yet: distinct() just returns [], so wait and retry
        # rather than fail. The service starts reporting once the data arrives.
        #
        # The filter skips employees without a department. Once the department
        # index exists, distinct() reads it, and the index stores null for every
        # document that lacks the field -- the same reason the PostgreSQL version
        # needs WHERE department IS NOT NULL.
        has_department = {"department": {"$type": "string"}}
        departments = collection.distinct("department", has_department)
        while not departments:
            print("..departments.. [] -- no employee has a department yet; "
                  "waiting for alter_model.py (retrying in 5s)")
            time.sleep(5)
            departments = collection.distinct("department", has_department)
        print("..departments..", departments)
        print()

        while True:
            print("Running employees report (microservice two)")
            dept = random.choice(departments)
            print("--ENQUIRY FOR DEPARTMENT: " + dept)
            items = collection.find({"department" : dept}, {"_id": 0, "gender": 0, "annual_salary":0, "hire_date": 0}).limit(5)
            for item in items:
                hobbies = [describe(h) for h in item.get("hobbies", [])]
                print(f"{item['emp_no']} {item['first_name']} {item['last_name']} | "
                      f"{item['department']} | {item['title']} | hobbies: {hobbies}")

            print("...\n")
            time.sleep(5)

        print("Operation completed successfully!!!")

    except pymongo.errors.ConnectionFailure as e:
        print("Could not connect to MongoDB: %s" % e)
    conn.close()
