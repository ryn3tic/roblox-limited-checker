import os
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import aiohttp
import discord
from discord import app_commands

# ================= CONFIG =================

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

GUILD_ID = 1475935690465480979  # your server ID

MAX_PRICE = 200
ANALYZE_LIMIT = 50
POST_TOP = 10
SCAN_EVERY_SECONDS = 3600  # 1 hour

# ==========================================

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ---------- DATA FETCHING ----------

async def fetch_catalog_candidates(session: aiohttp.ClientSession) -> List[int]:
    url = (
        "https://catalog.roblox.com/v2/search/items/details"
        f"?salesTypeFilter=2"
        f"&limit={ANALYZE_LIMIT}"
        f"&minPrice=0"
        f"&maxPrice={MAX_PRICE}"
        f"&sortType=3"
    )

    headers = {
        "User-Agent": "LimitedTrendBot/1.0",
        "Accept": "application/json",
    }

    async with session.get(url, headers=headers, timeout=20) as r:
        data = await r.json()

    ids = []
    for item in data.get("data", []):
        if item.get("itemType") == "Asset" and isinstance(item.get("id"), int):
            ids.append(item["id"])

    return ids


async def fetch_resale_data(session: aiohttp.ClientSession, asset_id: int) -> Optional[Dict[str, Any]]:
    url = f"https://economy.roblox.com/v1/assets/{asset_id}/resale-data"

    headers = {
        "User-Agent": "LimitedTrendBot/1.0",
        "Accept": "application/json",
    }

    async with session.get(url, headers=headers, timeout=20) as r:
        if r.status != 200:
            return None
        data = await r.json()

    rap = data.get("recentAveragePrice")
    lowest = data.get("lowestResalePrice")

    if not isinstance(rap, (int, float)) or not isinstance(lowest, (int, float)):
        return None

    return {
        "asset_id": asset_id,
        "rap": float(rap),
        "lowest": float(lowest),
    }


def opportunity_score(rap: float, lowest: float) -> float:
    if rap <= 0:
        return -9999
    return (rap - lowest) / rap


async def run_scan() -> Dict[str, Any]:
    async with aiohttp.ClientSession() as session:

        candidates = await fetch_catalog_candidates(session)

        resale_results = []

        sem = asyncio.Semaphore(10)

        async def worker(aid):
            async with sem:
                data = await fetch_resale_data(session, aid)
                if data:
                    resale_results.append(data)

        await asyncio.gather(*(worker(aid) for aid in candidates))

        filtered = [
            x for x in resale_results
            if x["lowest"] <= MAX_PRICE
        ]

        for x in filtered:
            x["score"] = opportunity_score(x["rap"], x["lowest"])

        filtered.sort(key=lambda x: x["score"], reverse=True)

        top = filtered[:POST_TOP]

        return {
            "candidates": len(candidates),
            "scored": len(filtered),
            "top": top
        }


# ---------- EMBED ----------

def build_embed(result: Dict[str, Any], trigger: str) -> discord.Embed:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    embed = discord.Embed(
        title=f"📈 Trending Limiteds ≤ {MAX_PRICE} Robux",
        description=f"Trigger: {trigger}\nChecked: {now}",
        color=discord.Color.green()
    )

    embed.add_field(
        name="Scan Summary",
        value=f"Candidates: {result['candidates']}\nScored: {result['scored']}",
        inline=False
    )

    if not result["top"]:
        embed.add_field(
            name="No Results",
            value="No qualifying limiteds found this scan.",
            inline=False
        )
        return embed

    for i, item in enumerate(result["top"], start=1):
        gap = ((item["rap"] - item["lowest"]) / item["rap"]) * 100 if item["rap"] > 0 else 0

        embed.add_field(
            name=f"{i}. Asset {item['asset_id']}",
            value=f"Lowest: {int(item['lowest'])}\nRAP: {int(item['rap'])}\nGap: {gap:.1f}%",
            inline=False
        )

    return embed


# ---------- POSTING ----------

async def post_scan(trigger: str):
    if CHANNEL_ID == 0:
        print("CHANNEL_ID not set.")
        return

    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        print("Channel not found.")
        return

    result = await run_scan()
    embed = build_embed(result, trigger)
    await channel.send(embed=embed)


async def hourly_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        await post_scan("auto-hourly")
        await asyncio.sleep(SCAN_EVERY_SECONDS)


# ---------- SLASH COMMANDS ----------

@tree.command(
    name="profit",
    description="Calculate profit after Roblox 30% fee",
    guild=discord.Object(id=GUILD_ID)
)
async def profit(interaction: discord.Interaction, buy_price: float, sell_price: float):

    net = sell_price * 0.7
    profit_value = net - buy_price
    roi = (profit_value / buy_price * 100) if buy_price > 0 else 0

    embed = discord.Embed(title="📈 Profit Calculator", color=discord.Color.blurple())
    embed.add_field(name="Buy Price", value=f"{buy_price:.2f}", inline=True)
    embed.add_field(name="Sell Price", value=f"{sell_price:.2f}", inline=True)
    embed.add_field(name="After Fee (70%)", value=f"{net:.2f}", inline=False)
    embed.add_field(name="Profit", value=f"{profit_value:.2f}", inline=True)
    embed.add_field(name="ROI", value=f"{roi:.2f}%", inline=True)

    await interaction.response.send_message(embed=embed)


@tree.command(
    name="scan",
    description="Run scan now",
    guild=discord.Object(id=GUILD_ID)
)
async def scan(interaction: discord.Interaction):

    await interaction.response.send_message("🔎 Running scan...", ephemeral=True)
    await post_scan(f"/scan by {interaction.user}")


# ---------- READY ----------

@client.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    await tree.sync(guild=guild)
    print(f"Synced commands to guild {GUILD_ID}")
    print(f"Logged in as {client.user}")
    client.loop.create_task(hourly_loop())


client.run(TOKEN)
