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
GUILD_ID = 1475935690465480979

AUTO_MAX_PRICE = 200
AUTO_TOP_N = 10
AUTO_MIN_RAP = 0
AUTO_MIN_GAP = -999
AUTO_MODE = "gap"
ANALYZE_LIMIT = 50
SCAN_INTERVAL = 3600

MAX_CONCURRENT = 10

# ==========================================

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ================= DATA ===================

async def fetch_collectible_assets(session: aiohttp.ClientSession, max_price: int) -> List[int]:
    """
    Fetch resale-enabled collectibles directly.
    """
    url = (
        "https://catalog.roblox.com/v1/search/items"
        f"?category=Collectibles"
        f"&salesTypeFilter=2"
        f"&limit={ANALYZE_LIMIT}"
        f"&minPrice=0"
        f"&maxPrice={max_price}"
        f"&sortType=3"
    )

    headers = {"User-Agent": "LimitedScannerBot/1.0"}

    async with session.get(url, headers=headers, timeout=20) as r:
        if r.status != 200:
            return []
        data = await r.json()

    ids = []
    for item in data.get("data", []):
        if item.get("itemType") == "Asset":
            ids.append(item["id"])

    return ids


async def fetch_resale(session: aiohttp.ClientSession, asset_id: int) -> Optional[Dict[str, Any]]:
    url = f"https://economy.roblox.com/v1/assets/{asset_id}/resale-data"
    headers = {"User-Agent": "LimitedScannerBot/1.0"}

    async with session.get(url, headers=headers, timeout=20) as r:
        if r.status != 200:
            return None
        data = await r.json()

    rap = data.get("recentAveragePrice")
    lowest = data.get("lowestResalePrice")

    if not isinstance(rap, (int, float)) or not isinstance(lowest, (int, float)):
        return None

    return {
        "id": asset_id,
        "rap": float(rap),
        "lowest": float(lowest),
    }


def compute_gap(rap: float, lowest: float) -> float:
    if rap <= 0:
        return 0
    return (rap - lowest) / rap * 100


def risk_rating(rap: float, lowest: float) -> str:
    gap = compute_gap(rap, lowest)
    if gap < -20:
        return "High"
    if -20 <= gap < 0:
        return "Medium"
    if 0 <= gap < 25:
        return "Low"
    return "Medium"


def score_item(rap: float, lowest: float, mode: str) -> float:
    gap = compute_gap(rap, lowest)
    if mode == "gap":
        return gap
    if mode == "momentum":
        return -abs(gap + 5)
    return gap - abs(gap) * 0.2


async def run_scan(max_price: int, top_n: int, min_rap: int, min_gap: float, mode: str):
    async with aiohttp.ClientSession() as session:
        candidates = await fetch_collectible_assets(session, max_price)

        sem = asyncio.Semaphore(MAX_CONCURRENT)
        results = []

        async def worker(aid):
            async with sem:
                data = await fetch_resale(session, aid)
                if data:
                    results.append(data)

        await asyncio.gather(*(worker(a) for a in candidates))

        filtered = []
        for item in results:
            if item["lowest"] > max_price:
                continue
            if item["rap"] < min_rap:
                continue

            gap = compute_gap(item["rap"], item["lowest"])
            if gap < min_gap:
                continue

            item["gap"] = gap
            item["risk"] = risk_rating(item["rap"], item["lowest"])
            item["score"] = score_item(item["rap"], item["lowest"], mode)
            filtered.append(item)

        filtered.sort(key=lambda x: x["score"], reverse=True)

        return filtered[:min(top_n, 25)], len(candidates), len(filtered)


# ================= EMBED ==================

def build_embed(items, candidates, scored, params, trigger):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    embed = discord.Embed(
        title=f"📈 Limited Scan ≤ {params['max_price']}R$",
        description=f"{trigger}\nMode: {params['mode']} | min_rap: {params['min_rap']} | min_gap: {params['min_gap']}%\nChecked: {now}",
        color=discord.Color.green(),
    )

    embed.add_field(
        name="Summary",
        value=f"Candidates: {candidates}\nQualified: {scored}",
        inline=False
    )

    if not items:
        embed.add_field(name="No Results", value="Nothing matched filters.", inline=False)
        return embed

    for i, item in enumerate(items, 1):
        embed.add_field(
            name=f"{i}. Asset {item['id']}",
            value=f"Lowest: {int(item['lowest'])} | RAP: {int(item['rap'])}\nGap: {item['gap']:.1f}% | Risk: {item['risk']}",
            inline=False
        )

    return embed


# ================= POSTING =================

async def post_scan(trigger, max_price, top_n, min_rap, min_gap, mode):
    if CHANNEL_ID == 0:
        return

    channel = client.get_channel(CHANNEL_ID)
    if not channel:
        return

    items, candidates, scored = await run_scan(max_price, top_n, min_rap, min_gap, mode)

    embed = build_embed(
        items,
        candidates,
        scored,
        {
            "max_price": max_price,
            "min_rap": min_rap,
            "min_gap": min_gap,
            "mode": mode,
        },
        trigger
    )

    await channel.send(embed=embed)


async def hourly_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        await post_scan(
            "Auto Hourly Scan",
            AUTO_MAX_PRICE,
            AUTO_TOP_N,
            AUTO_MIN_RAP,
            AUTO_MIN_GAP,
            AUTO_MODE
        )
        await asyncio.sleep(SCAN_INTERVAL)


# ================= SLASH ==================

@tree.command(name="profit", description="Calculate profit after 30% Roblox fee", guild=discord.Object(id=GUILD_ID))
async def profit(interaction: discord.Interaction, buy_price: float, sell_price: float):
    net = sell_price * 0.7
    profit_val = net - buy_price
    roi = (profit_val / buy_price * 100) if buy_price > 0 else 0

    embed = discord.Embed(title="📈 Profit Calculator", color=discord.Color.blurple())
    embed.add_field(name="After Fee", value=f"{net:.2f}", inline=False)
    embed.add_field(name="Profit", value=f"{profit_val:.2f}", inline=True)
    embed.add_field(name="ROI", value=f"{roi:.2f}%", inline=True)

    await interaction.response.send_message(embed=embed)


@tree.command(name="scan", description="Run scan now", guild=discord.Object(id=GUILD_ID))
async def scan(
    interaction: discord.Interaction,
    max_price: int = AUTO_MAX_PRICE,
    top_n: int = AUTO_TOP_N,
    min_rap: int = AUTO_MIN_RAP,
    min_gap: float = AUTO_MIN_GAP,
    mode: str = AUTO_MODE
):
    await interaction.response.send_message("🔎 Running scan...", ephemeral=True)

    await post_scan(
        f"Manual scan by {interaction.user}",
        max_price,
        top_n,
        min_rap,
        min_gap,
        mode
    )


@client.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    await tree.sync(guild=guild)
    print("Bot Ready")
    client.loop.create_task(hourly_loop())


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN not set")

client.run(TOKEN)
