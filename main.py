import os
import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

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
SCAN_INTERVAL = 3600

MAX_CONCURRENT = 15

# ==========================================

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ================= DATA ===================

async def fetch_rolimons_limiteds(session: aiohttp.ClientSession) -> Dict[int, Dict[str, Any]]:
    """
    Get all limited items from Rolimon's API.
    """
    url = "https://www.rolimons.com/itemapi/itemdetails"

    async with session.get(url, timeout=30) as r:
        data = await r.json()

    items = {}

    for asset_id, info in data.get("items", {}).items():
        try:
            aid = int(asset_id)
            name = info[0]
            rap = info[2]
            value = info[3]
            limited_flag = info[5]

            # limited_flag: 1 = limited, 2 = limitedU
            if limited_flag in (1, 2):
                items[aid] = {
                    "name": name,
                    "rap": rap,
                    "value": value,
                }
        except Exception:
            continue

    return items


async def fetch_resale(session: aiohttp.ClientSession, asset_id: int) -> Optional[Dict[str, Any]]:
    url = f"https://economy.roblox.com/v1/assets/{asset_id}/resale-data"

    async with session.get(url, timeout=20) as r:
        if r.status != 200:
            return None
        data = await r.json()

    lowest = data.get("lowestResalePrice")
    rap = data.get("recentAveragePrice")
    num_sellers = data.get("numSellers")

    if not isinstance(lowest, (int, float)):
        return None

    if lowest <= 0:
        return None

    return {
        "lowest": float(lowest),
        "rap": float(rap) if isinstance(rap, (int, float)) else 0,
        "num_sellers": num_sellers,
    }


def compute_gap(rap: float, lowest: float) -> float:
    if rap <= 0:
        return 0
    return (rap - lowest) / rap * 100


def risk_rating(gap: float) -> str:
    if gap < -20:
        return "High"
    if -20 <= gap < 0:
        return "Medium"
    if 0 <= gap < 25:
        return "Low"
    return "Medium"


def score_item(gap: float, mode: str) -> float:
    if mode == "gap":
        return gap
    if mode == "momentum":
        return -abs(gap + 5)
    return gap - abs(gap) * 0.2


async def run_scan(max_price, top_n, min_rap, min_gap, mode):
    async with aiohttp.ClientSession() as session:

        rolimons_items = await fetch_rolimons_limiteds(session)

        sem = asyncio.Semaphore(MAX_CONCURRENT)
        results = []

        async def worker(asset_id, item_info):
            async with sem:
                resale = await fetch_resale(session, asset_id)
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
                    "id": asset_id,
                    "name": item_info["name"],
                    "lowest": lowest,
                    "rap": rap,
                    "gap": gap,
                    "risk": risk_rating(gap),
                    "score": score_item(gap, mode),
                })

        tasks = [
            worker(aid, info)
            for aid, info in rolimons_items.items()
        ]

        await asyncio.gather(*tasks)

        results.sort(key=lambda x: x["score"], reverse=True)

        return results[:min(top_n, 25)], len(rolimons_items), len(results)


# ================= EMBED ==================

def build_embed(items, total, qualified, params, trigger):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    embed = discord.Embed(
        title=f"📈 Limited Scan ≤ {params['max_price']}R$",
        description=f"{trigger}\nMode: {params['mode']} | min_rap: {params['min_rap']} | min_gap: {params['min_gap']}%\nChecked: {now}",
        color=discord.Color.green(),
    )

    embed.add_field(
        name="Summary",
        value=f"Total Limiteds: {total}\nQualified: {qualified}",
        inline=False
    )

    if not items:
        embed.add_field(name="No Results", value="Nothing matched filters.", inline=False)
        return embed

    for i, item in enumerate(items, 1):
        embed.add_field(
            name=f"{i}. {item['name']}",
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

    items, total, qualified = await run_scan(max_price, top_n, min_rap, min_gap, mode)

    embed = build_embed(
        items,
        total,
        qualified,
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
