import os
import asyncio
import aiohttp
import json

async def main():
    async with aiohttp.ClientSession() as s:
        r = await s.get(
            'https://discord.com/api/v10/guilds/1439168245130334288/members?limit=100',
            headers={
                'Authorization': "Bot " + os.environ["DISCORD_BOT_TOKEN"],
            }
        )
        print("Status:", r.status)
        members = await r.json()
        if isinstance(members, list):
            for m in members:
                user = m.get("user", {})
                print(f"User: {user.get('username')}, ID: {user.get('id')}")
        else:
            print("Error:", members)

if __name__ == "__main__":
    asyncio.run(main())
