import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from azure.cosmos import CosmosClient

# Read settings directly from local.settings.json
settings = json.load(open("local.settings.json"))
values = settings["Values"]

endpoint = values["COSMOS_ENDPOINT"]
key = values["COSMOS_KEY"]
database = values["COSMOS_DATABASE"]

# Connect to Cosmos DB
client = CosmosClient(url=endpoint, credential=key)
db = client.get_database_client(database)

# Load synthetic data
data = json.load(open("synthetic_data_full.json"))

# Seed customers
print("Seeding customers...")
container = db.get_container_client("customers")
for item in data["customers"]:
    container.upsert_item(item)
print(f"✅ Seeded {len(data['customers'])} customers")

# Seed checks
print("Seeding checks...")
container = db.get_container_client("checks")
for item in data["checks"]:
    container.upsert_item(item)
print(f"✅ Seeded {len(data['checks'])} checks")

# Seed fraud cases
print("Seeding fraud cases...")
container = db.get_container_client("fraud_cases")
for item in data["fraud_cases"]:
    container.upsert_item(item)
print(f"✅ Seeded {len(data['fraud_cases'])} fraud cases")

print("\n✅ Cosmos DB seeding complete!")
print(f"   Customers : {len(data['customers'])}")
print(f"   Checks    : {len(data['checks'])}")
print(f"   Fraud cases: {len(data['fraud_cases'])}")
