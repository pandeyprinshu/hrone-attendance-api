import os
from pathlib import Path
from bson import json_util
from pymongo import MongoClient
from dotenv import load_dotenv

# Load .env if present
load_dotenv()

# Read environment variables with consistent fallbacks
MONGO_URI = os.getenv("MONGO_URI", os.getenv("MONGODB_URL", "mongodb://localhost:27017"))
MONGO_DB = os.getenv("MONGO_DB", os.getenv("DATABASE_NAME", "hrone_attendance"))

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

# Locate sample_data folder relative to this script's path
BASE_DIR = Path(__file__).resolve().parent
SAMPLE_DATA_DIR = BASE_DIR / "sample_data"


def seed_file(filename: str, collection_name: str):
    file_path = SAMPLE_DATA_DIR / filename
    if not file_path.exists():
        print(f"File not found: {file_path}")
        return

    with open(file_path, "r", encoding="utf-8") as f:
        documents = json_util.loads(f.read())

    coll = db[collection_name]
    coll.delete_many({})  # Clear previous dirty records
    if documents:
        coll.insert_many(documents)
    print(f"Seeded {len(documents)} documents into '{collection_name}' in database '{MONGO_DB}'.")


if __name__ == "__main__":
    # Seed employees
    seed_file("employees.json", "employees")

    # Seed both collection names to prevent 404s regardless of how main.py references it
    seed_file("attendance_logs.json", "attendance")
    seed_file("attendance_logs.json", "attendance_logs")

    print("Database seeding completed.")