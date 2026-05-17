Check the status of a specific check by its ID from the live Azure environment.

Usage: /check-status <check-id>

Fetches from: GET https://checkfraudagent.azurewebsites.net/api/checks/{id}/status

Execute the following command with the check ID provided in $ARGUMENTS:
```
curl -s "https://checkfraudagent.azurewebsites.net/api/checks/$ARGUMENTS/status" | python3 -m json.tool
```
