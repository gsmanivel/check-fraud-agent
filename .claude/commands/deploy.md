Deploy the check-fraud-agent to Azure Functions.

This command zips the project (excluding .funcignore entries) and deploys to the Azure Function App.

Steps performed:
1. Create deploy.zip excluding files in .funcignore
2. Deploy to Azure using az functionapp deployment source config-zip
3. Show deployment status

Function App: checkfraudagent
Resource Group: manman-rg

Execute the following commands in sequence:
```
zip -r deploy.zip . -x@.funcignore
az functionapp deployment source config-zip --resource-group manman-rg --name checkfraudagent --src deploy.zip
rm deploy.zip
```
