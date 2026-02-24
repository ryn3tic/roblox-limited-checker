import os
import asyncio
import random
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

import aiohttp
import discord
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
GUILD_ID = 1475935690465480979

AUTO_MAX_PRICE = 200
AUTO_TOP_N = 10
AUTO_MIN_RAP = 0
AUTO_MIN_GAP = -999
AUTO_MODE = "gap"
SCAN_INTERVAL = 3600

MAX_CONCURRENT = 8
ROLIMONS_SAMPLE_SIZE = 250

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ================== DATA ==================

async def fetch_rolimons_limiteds(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    url = "https://www.rolimons.com/itemapi/itemdetails"
    async with session.get(url, timeout=30) as r:
        data = await r.json()

    items = []

    for asset_id, info in data.get("items", {}).items():
        try:
            aid = int(asset_id)
            name = info[0]
            limited_flag = info[5]
            if limited_flag in (1, 2):
                items.append({"id": aid, "name": name})
        except:
            continue

    return items


async def fetch_resale(session: aiohttp.ClientSession, asset_id: int) -> Optional[Dict[str, Any]]:
    url = f"https://economy.roblox.com/v1/assets/{asset_id}/resale-data"
    async with session.get(url, timeout=15) as r:
        if r.status != 200:
            return None
        data = await r.json()

    lowest = data.get("lowestResalePrice")
    rap = data.get("recentAveragePrice")

    if not isinstance(lowest, (int, float)):
        return None

    return {
        "lowest": float(lowest),
        "rap": float(rap) if isinstance(rap, (int, float)) else 0,
    }


def compute_gap(rap, lowest):
    if rap <= 0:
        return 0
    return (rap - lowest) / rap * 100


async def run_scan(max_price, top_n, min_rap, min_gap, mode):
    async with aiohttp.ClientSession() as session:

        rolimons_items = await fetch_rolimons_limiteds(session)

        # SAMPLE instead of scanning all
        if len(rolimons_items) > ROLIMONS_SAMPLE_SIZE:
            rolimons_items = random.sample(rolimons_items, ROLIMONS_SAMPLE_SIZE)

        sem = asyncio.Semaphore(MAX_CONCURRENT)
        results = []

        async def worker(item):
            async with sem:
                resale = await fetch_resale(session, item["id"])
                if not resale:
                    return

                lowest = resale["lowest"]
                rap = resale["rap"]

                if lowest > max_price:
                    return
                if rap < min_rap:
                    return

                gap = compute_gap(rap, lowest)
                if gap < min_gap:
                    return

                results.append({
                    "id": item["id"],
                    "name": item["name"],
                    "lowest": lowest,
                    "rap": rap,
                    "gap": gap
                })

        await asyncio.gather(*(worker(i) for i in rolimons_items))

        results.sort(key=lambda x: x["gap"], reverse=True)

        return results[:min(top_n, 25)], len(rolimons_items), len(results)


# ================== EMBED ==================

def build_embed(items, scanned, qualified, params, trigger):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    embed = discord.Embed(
        title=f"📈 Limited Scan ≤ {params['max_price']}R$",
        description=f"{trigger}\nChecked: {now}",
        color=discord.Color.green(),
    )

    embed.add_field(
        name="Summary",
        value=f"Scanned: {scanned}\nQualified: {qualified}",
        inline=False
    )

    if not items:
        embed.add_field(name="No Results", value="Nothing matched filters.", inline=False)
        return embed

    for i, item in enumerate(items, 1):
        embed.add_field(
            name=f"{i}. {item['name']}",
            value=f"Lowest: {int(item['lowest'])} | RAP: {int(item['rap'])}\nGap: {item['gap']:.1f}%",
            inline=False
        )

    return embed


async def post_scan(trigger, max_price, top_n, min_rap, min_gap, mode):
    if CHANNEL_ID == 0:
        return

    channel = client.get_channel(CHANNEL_ID)
    if not channel:
        return

    items, scanned, qualified = await run_scan(max_price, top_n, min_rap, min_gap, mode)

    embed = build_embed(
        items,
        scanned,
        qualified,
        {
            "max_price": max_price,
        },
        trigger
    )

    await channel.send(embed=embed)


async def hourly_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        await post_scan("Auto Hourly Scan", AUTO_MAX_PRICE, AUTO_TOP_N, AUTO_MIN_RAP, AUTO_MIN_GAP, AUTO_MODE)
        await asyncio.sleep(SCAN_INTERVAL)


@tree.command(name="scan", description="Run scan now", guild=discord.Object(id=GUILD_ID))
async def scan(interaction: discord.Interaction, max_price: int = AUTO_MAX_PRICE):
    await interaction.response.send_message("🔎 Running scan...", ephemeral=True)
    await post_scan(f"Manual scan by {interaction.user}", max_price, AUTO_TOP_N, AUTO_MIN_RAP, AUTO_MIN_GAP, AUTO_MODE)


@client.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    await tree.sync(guild=guild)
    print("Bot Ready")
    client.loop.create_task(hourly_loop())


client.run(TOKEN)
