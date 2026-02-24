import discord
import requests
import asyncio
import os
from datetime import datetime

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

CHANNEL_ID = 1475935692537204860 # replace with your channel ID


async def fetch_trending():
    # Placeholder – we’ll improve this later
    return [
        {"name": "Example Item", "rap": 600, "lowest": 465, "change": "+12%"},
        {"name": "Example Item 2", "rap": 320, "lowest": 305, "change": "+3%"},
    ]


async def post_trending():
    await client.wait_until_ready()
    channel = client.get_channel(CHANNEL_ID)

    while not client.is_closed():
        items = await fetch_trending()

        message = f"📈 Trending Update ({datetime.now().strftime('%H:%M')})\n\n"

        for item in items:
            message += (
                f"**{item['name']}**\n"
                f"RAP: {item['rap']}\n"
                f"Lowest: {item['lowest']}\n"
                f"24h Change: {item['change']}\n\n"
            )

        await channel.send(message)

        await asyncio.sleep(3600)  # 1 hour


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    client.loop.create_task(post_trending())


client.run(TOKEN)
