Fetch all checks currently awaiting analyst review from the live Azure environment.

Shows checks in the Tier-3 analyst queue with their status, fraud decision, and assigned time.

Execute the following command:
```
curl -s "https://checkfraudagent.azurewebsites.net/api/checks/queue" | python3 -m json.tool
```
