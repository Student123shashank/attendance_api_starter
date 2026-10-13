"""Load the provided Extended JSON sample files into MongoDB."""
import os
from pathlib import Path
from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
base = Path(__file__).resolve().parent / "sample_data"
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]
for collection in ("employees", "attendance_logs"):
    docs = json_util.loads((base / f"{collection}.json").read_text(encoding="utf-8"))
    db[collection].delete_many({})
    if docs:
        db[collection].insert_many(docs)
    print(f"loaded {len(docs):>2} documents into '{db.name}.{collection}'")
