#!/usr/bin/env python3

import pymongo
import demo_settings

if __name__ == "__main__":
    try:
        conn = pymongo.MongoClient(demo_settings.URI_STRING)
        print("Connected to MongoDB")

        db = conn[demo_settings.DB_NAME]
        # username = db.command("connectionStatus")['authInfo']['authenticatedUsers'][0]['user']
        # collection = db[username]
        collection = db[demo_settings.COLLECTION_NAME]

        print("Dropping Collection: " + demo_settings.DB_NAME + "." + demo_settings.COLLECTION_NAME)
        collection.drop()

        print("Operation completed successfully!!!")

    except pymongo.errors.ConnectionFailure as e:
        print("Could not connect to MongoDB: %s" % e)
    conn.close()
