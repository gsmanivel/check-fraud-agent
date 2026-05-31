import json
import sys
import os
from pathlib import Path

repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root))
from azure.cosmos import CosmosClient

# Read settings directly from local.settings.json
settings = json.loads((repo_root / "local.settings.json").read_text())
values = settings["Values"]

endpoint = values["COSMOS_ENDPOINT"]
key = values["COSMOS_KEY"]
database = values["COSMOS_DATABASE"]

# Connect to Cosmos DB
client = CosmosClient(url=endpoint, credential=key)
db = client.get_database_client(database)


def reset_container(name: str, partition_key_path: str):
    """Drop and recreate a container so every seed run starts clean."""
    try:
        db.delete_container(name)
        print(f"   Cleared:   {name}")
    except Exception:
        pass
    db.create_container(
        id=name,
        partition_key={"paths": [partition_key_path], "kind": "Hash"},
    )
    print(f"   Recreated: {name}")
    return db.get_container_client(name)


# Load synthetic data
data = json.loads((repo_root / "testdata" / "scripts" / "synthetic_data_full.json").read_text())

print("Resetting containers...")
customers_c   = reset_container("customers",   "/id")
checks_c      = reset_container("checks",      "/id")
fraud_cases_c = reset_container("fraud_cases", "/id")

# Seed customers
print("\nSeeding customers...")
for item in data["customers"]:
    customers_c.upsert_item(item)
print(f"✅ Seeded {len(data['customers'])} customers")

# Seed checks
print("Seeding checks...")
for item in data["checks"]:
    checks_c.upsert_item(item)
print(f"✅ Seeded {len(data['checks'])} checks")

# Seed fraud cases
print("Seeding fraud cases...")
for item in data["fraud_cases"]:
    fraud_cases_c.upsert_item(item)
print(f"✅ Seeded {len(data['fraud_cases'])} fraud cases")

print("\n✅ Cosmos DB seeding complete!")
print(f"   Customers  : {len(data['customers'])}")
print(f"   Checks     : {len(data['checks'])}")
print(f"   Fraud cases: {len(data['fraud_cases'])}")
