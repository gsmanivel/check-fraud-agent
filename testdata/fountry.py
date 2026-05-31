import os, json
from pathlib import Path
from azure.ai.agents import AgentsClient
from azure.identity import DefaultAzureCredential

settings = json.loads(Path('local.settings.json').read_text())['Values']
endpoint = settings['AZURE_AI_AGENTS_ENDPOINT']

client = AgentsClient(endpoint=endpoint, credential=DefaultAzureCredential())

deleted = False
def main():
  for agent in client.list_agents():
    print(agent.name)
    # if agent.name == 'FraudInvestigationAgent':
    #   client.delete_agent(agent.id)
    #   print(f'Deleted: {agent.id}')
    #   deleted = True
    #   break
  
if __name__ == "__main__":
    main()