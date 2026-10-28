"""Check only the eight explicitly supplied demo logins; never enumerate users."""
import asyncio
from collections import Counter
import json
from pathlib import Path
from .client import EvenBetClient,Settings

NAMES=("richard","kate","john","jeremy","mason","monica","emma","andrew")

async def main():
    results=[]
    lobby=[]
    for name in NAMES:
        try:
            async with EvenBetClient(Settings(username=name)) as client:
                row={"username":name,"login":True,"player_id":client.player_id}
                if not lobby:
                    lobby=await client.list_tables()
                results.append(row)
        except Exception as e:
            results.append({"username":name,"login":False,"error_type":type(e).__name__})
    output={"accounts":results,"table_count":len(lobby),"sample":lobby[:3]}
    path=Path("artifacts/demo-account-check.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"accounts":results,"tables":lobby},indent=2),encoding="utf-8")
    print(json.dumps(output,indent=2))

if __name__=="__main__":
    asyncio.run(main())
