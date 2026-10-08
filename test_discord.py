import os
import asyncio
import aiohttp
import json

async def main():
    async with aiohttp.ClientSession() as s:
        schema = {
            "name": "tron",
            "description": "Control TronClass rollcall automation.",
            "type": 1,
            "options": [
                {
                    "name": "status",
                    "description": "Show the bound profile status.",
                    "type": 1
                }
            ]
        }
        r = await s.post(
            'https://discord.com/api/v10/applications/1513817558879572018/guilds/1439168245130334288/commands',
            headers={
                'Authorization': "Bot " + os.environ["DISCORD_BOT_TOKEN"],
                'Content-Type': 'application/json'
            },
            json=schema
        )
        print("Status:", r.status)
        print("Body:", await r.text())

if __name__ == "__main__":
    asyncio.run(main())
