# MongoDB connection settings for the comparison demo.
# The connection string is read from the MONGODB_URI environment variable so
# that no credentials ever need to be written into this file:
#   export MONGODB_URI="mongodb+srv://main_user:<password>@<cluster>/"
import os

URI_STRING = os.environ.get("MONGODB_URI", "YOUR_ATLAS_URI_STRING")

# Keep these in sync with ../postgres/demo_settings.py
NUM_ITEMS = 10000
NUM_SAMPLING = 1000
DB_NAME = "FLEXIBLE"
COLLECTION_NAME = "employees"
