import os
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp
import discord
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))  # set this in Railway Variables

MAX_PRICE = 100
ANALYZE_LIMIT = 50
POST_TOP = 10
SCAN_EVERY_SECONDS = 3600  # 1 hour


intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ---------- Data Fetching ----------

async def fetch_catalog_candidates(session: aiohttp.ClientSession, limit: int) -> List[int]:
    """
    Pull a list of candidate collectible items from Roblox catalog search.
    NOTE: Roblox parameters sometimes change; if you get empty results, we’ll tweak sortType/filters.
    """
    # Collectibles = SalesTypeFilter 2 (commonly used in Roblox catalog URLs / docs)
    # SortType: we try Bestselling-ish. If this yields weird results, we’ll adjust.
    url = (
        "https://catalog.roblox.com/v2/search/items/details"
        f"?salesTypeFilter=2"
        f"&limit={limit}"
        f"&minPrice=0"
        f"&maxPrice={MAX_PRICE}"
        f"&sortType=3"
    )

    headers = {
        "User-Agent": "LimitedTrendBot/1.0 (discord bot)",
        "Accept": "application/json",
    }

    async with session.get(url, headers=headers, timeout=20) as r:
        data = await r.json()

    ids: List[int] = []
    for item in data.get("data", []):
        # v2 search can return Bundles + Assets; we only want Assets
        if item.get("itemType") == "Asset" and isinstance(item.get("id"), int):
            ids.append(item["id"])

    return ids


async def fetch_resale_data(session: aiohttp.ClientSession, asset_id: int) -> Optional[Dict[str, Any]]:
    """
    Uses Roblox economy resale-data for RAP + lowest resale (structured endpoint).
    """
    url = f"https://economy.roblox.com/v1/assets/{asset_id}/resale-data"
    headers = {
        "User-Agent": "LimitedTrendBot/1.0 (discord bot)",
        "Accept": "application/json",
    }

    async with session.get(url, headers=headers, timeout=20) as r:
        if r.status != 200:
            return None
        data = await r.json()

    # Typical fields include: recentAveragePrice, lowestResalePrice, volumeData, etc.
    rap = data.get("recentAveragePrice")
    lowest = data.get("lowestResalePrice")

    if not isinstance(rap, (int, float)) or not isinstance(lowest, (int, float)):
        return None

    return {
        "asset_id": asset_id,
        "rap": float(rap),
        "lowest": float(lowest),
    }


async def fetch_asset_names(session: aiohttp.ClientSession, asset_ids: List[int]) -> Dict[int, str]:
    """
    Resolve item names in batch via items API. If this fails, we’ll fall back to showing IDs.
    """
    if not asset_ids:
        return {}

    url = "https://catalog.roblox.com/v1/catalog/items/details"
    headers = {
        "User-Agent": "LimitedTrendBot/1.0 (discord bot)",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    payload = {"items": [{"itemType": "Asset", "id": aid} for aid in asset_ids]}

    async with session.post(url, json=payload, headers=headers, timeout=25) as r:
        if r.status != 200:
            return {}
        data = await r.json()

    out: Dict[int, str] = {}
    for item in data.get("data", []):
        try:
            _id = int(item.get("id"))
            name = str(item.get("name", f"Asset {_id}"))
            out[_id] = name
        except Exception:
            continue
    return out


def opportunity_score(rap: float, lowest: float) -> float:
    """
    Include everything (your request), but still rank sensibly.
    Higher score = better "value gap".
    """
    if rap <= 0:
        return -9999.0
    gap = (rap - lowest) / rap  # positive if lowest < rap
    return gap


async def run_scan() -> Dict[str, Any]:
    """
    Returns scan results ready to post.
    """
    async with aiohttp.ClientSession() as session:
        candidates = await fetch_catalog_candidates(session, ANALYZE_LIMIT)

        # Pull resale data (RAP/lowest) for each candidate
        resale_results: List[Dict[str, Any]] = []
        # light concurrency
        sem = asyncio.Semaphore(10)

        async def worker(aid: int):
            async with sem:
                d = await fetch_resale_data(session, aid)
                if d:
                    resale_results.append(d)

        await asyncio.gather(*(worker(aid) for aid in candidates))

        # Filter max price
        filtered = [x for x in resale_results if x["lowest"] <= MAX_PRICE]

        # Score + sort
        for x in filtered:
            x["score"] = opportunity_score(x["rap"], x["lowest"])

        filtered.sort(key=lambda x: x["score"], reverse=True)

        top = filtered[:POST_TOP]

        # Try to resolve names for top results
        names = await fetch_asset_names(session, [x["asset_id"] for x in top])

        return {
            "count_candidates": len(candidates),
            "count_scored": len(filtered),
            "top": [
                {
                    "asset_id": x["asset_id"],
                    "name": names.get(x["asset_id"], f"Asset {x['asset_id']}"),
                    "lowest": x["lowest"],
                    "rap": x["rap"],
                    "gap_pct": ((x["rap"] - x["lowest"]) / x["rap"] * 100) if x["rap"] > 0 else 0.0,
                    "score": x["score"],
                }
                for x in top
            ],
        }


# ---------- Posting ----------

def make_embed(result: Dict[str, Any], triggered_by: str) -> discord.Embed:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    embed = discord.Embed(
        title=f"📈 Trending Collectibles ≤ {MAX_PRICE}R$",
        description=f"Source: Roblox catalog + resale data | Trigger: {triggered_by}\nChecked: {now}",
        color=discord.Color.green(),
    )

    embed.add_field(
        name="Scan Summary",
        value=f"Candidates: {result['count_candidates']}\nScored (≤{MAX_PRICE}): {result['count_scored']}\nTop shown: {len(result['top'])}",
        inline=False,
    )

    if not result["top"]:
        embed.add_field(
            name="No items found",
            value="Nothing matched the filters this scan. (Still posting because you asked always post.)",
            inline=False,
        )
        return embed

    lines = []
    for i, item in enumerate(result["top"], start=1):
        lines.append(
            f"**{i}. {item['name']}** (`{item['asset_id']}`)\n"
            f"Lowest: **{int(item['lowest'])}** | RAP: **{int(item['rap'])}** | Gap: **{item['gap_pct']:.1f}%**"
        )

    embed.add_field(name="Top Picks", value="\n\n".join(lines), inline=False)
    return embed


async def post_to_channel(triggered_by: str):
    if CHANNEL_ID == 0:
        print("CHANNEL_ID is not set. Add it in Railway Variables.")
        return

    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        print("Could not find channel. Check CHANNEL_ID and bot server access.")
        return

    try:
        result = await run_scan()
        embed = make_embed(result, triggered_by)
        await channel.send(embed=embed)
    except Exception as e:
        await channel.send(f"⚠️ Scan failed: `{type(e).__name__}: {e}`")


async def hourly_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        await post_to_channel("auto-hourly")
        await asyncio.sleep(SCAN_EVERY_SECONDS)


# ---------- Slash Commands ----------

@tree.command(name="profit", description="Calculate profit after Roblox 30% fee (you receive 70%).")
@app_commands.describe(buy_price="Price you bought the item for", sell_price="Current sell price")
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


@tree.command(name="scan", description="Run a scan now and post results publicly.")
async def scan(interaction: discord.Interaction):
    await interaction.response.send_message("🔎 Scanning now… (posting results in the channel)", ephemeral=True)
    await post_to_channel(f"manual /scan by {interaction.user}")


@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user}")
    client.loop.create_task(hourly_loop())


client.run(TOKEN)
