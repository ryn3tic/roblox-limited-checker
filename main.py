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
AUTO_MIN_GAP = 0          # % gap between RAP and Rolimons value
AUTO_MODE = "gap"
SCAN_INTERVAL = 3600

ROLIMONS_SAMPLE_SIZE = 300

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ================== DATA ==================

# Rolimons item array indices:
# [0]  name
# [1]  acronym
# [2]  rap          (Recent Average Price — Roblox official)
# [3]  value        (Rolimons community value estimate)
# [4]  default_value
# [5]  demand       (0=unassigned, 1=terrible, 2=low, 3=normal, 4=high, 5=amazing)
# [6]  trend        (0=unassigned, 1=lowering, 2=stable, 3=raising, 4=fluctuating, 5=projected)
# [7]  projected    (1=projected, -1=not)
# [8]  hyped        (1=hyped, -1=not)
# [9]  rare         (1=rare, -1=not)

DEMAND_LABELS = {
    0: "Unassigned",
    1: "Terrible",
    2: "Low",
    3: "Normal",
    4: "High",
    5: "Amazing",
}

TREND_LABELS = {
    0: "Unassigned",
    1: "Lowering ↓",
    2: "Stable →",
    3: "Raising ↑",
    4: "Fluctuating ~",
    5: "Projected",
}


async def fetch_rolimons_limiteds(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    url = "https://www.rolimons.com/itemapi/itemdetails"
    headers = {"User-Agent": "Mozilla/5.0"}  # Rolimons prefers a real UA
    async with session.get(url, headers=headers, timeout=30) as r:
        data = await r.json(content_type=None)

    items = []
    for asset_id, info in data.get("items", {}).items():
        try:
            aid = int(asset_id)
            name  = info[0]
            rap   = info[2] if isinstance(info[2], (int, float)) else 0
            value = info[3] if isinstance(info[3], (int, float)) else 0

            # Rolimons marks limited/collectible with flag at index 5 being 1 or 2
            # We skip items with no meaningful data (rap or value <= 0)
            if rap <= 0 or value <= 0:
                continue

            demand   = int(info[5]) if isinstance(info[5], int) else 0
            trend    = int(info[6]) if isinstance(info[6], int) else 0
            projected = info[7] == 1
            hyped     = info[8] == 1
            rare      = info[9] == 1

            items.append({
                "id": aid,
                "name": name,
                "rap": float(rap),
                "value": float(value),
                "demand": demand,
                "trend": trend,
                "projected": projected,
                "hyped": hyped,
                "rare": rare,
            })
        except Exception:
            continue

    return items


def compute_gap(rap: float, value: float) -> float:
    """
    Gap = how much the community value EXCEEDS the RAP.
    Positive = item is 'underpriced' relative to community estimate.
    Negative = item is trading above its estimated value (risky).
    """
    if value <= 0:
        return 0.0
    return (value - rap) / value * 100


def score_item(item: Dict[str, Any]) -> float:
    """
    Composite score blending gap, demand, and trend.
    Higher = better opportunity.
    """
    gap    = item["gap"]
    demand = item["demand"]   # 0-5
    trend  = item["trend"]    # 0-5

    # Normalise demand and trend to a 0-1 scale (ignoring 0=unassigned)
    d_score = (demand / 5) * 20 if demand > 0 else 0
    t_score = (trend  / 5) * 10 if trend  > 0 else 0

    bonus = 0
    if item["hyped"]:     bonus += 5
    if item["rare"]:      bonus += 5
    if item["projected"]: bonus -= 5   # projected items can be risky

    return gap + d_score + t_score + bonus


async def run_scan(max_price, top_n, min_rap, min_gap, mode):
    async with aiohttp.ClientSession() as session:
        all_items = await fetch_rolimons_limiteds(session)

    # Filter by price and RAP first
    candidates = [
        i for i in all_items
        if i["rap"] <= max_price and i["rap"] >= min_rap
    ]

    # Optionally sample to keep things snappy
    if len(candidates) > ROLIMONS_SAMPLE_SIZE:
        candidates = random.sample(candidates, ROLIMONS_SAMPLE_SIZE)

    results = []
    for item in candidates:
        gap = compute_gap(item["rap"], item["value"])
        if gap < min_gap:
            continue

        item["gap"]   = gap
        item["score"] = score_item(item)
        results.append(item)

    # Sort
    if mode == "score":
        results.sort(key=lambda x: x["score"], reverse=True)
    else:
        results.sort(key=lambda x: x["gap"], reverse=True)

    return results[:min(top_n, 25)], len(candidates), len(results)


# ================== EMBED ==================

def build_embed(items, scanned, qualified, params, trigger):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    embed = discord.Embed(
        title=f"📈 Limited Scan — RAP ≤ {params['max_price']} R$",
        description=f"{trigger}\nChecked: {now}\n*Powered by Rolimons data — no Roblox API needed*",
        color=discord.Color.green(),
    )

    embed.add_field(
        name="Summary",
        value=f"Candidates: {scanned} | Qualified: {qualified}",
        inline=False,
    )

    if not items:
        embed.add_field(
            name="No Results",
            value="Nothing matched your filters. Try raising `max_price` or lowering `min_gap`.",
            inline=False,
        )
        return embed

    for i, item in enumerate(items, 1):
        demand_label = DEMAND_LABELS.get(item["demand"], "?")
        trend_label  = TREND_LABELS.get(item["trend"], "?")

        tags = []
        if item["hyped"]:     tags.append("🔥 Hyped")
        if item["rare"]:      tags.append("💎 Rare")
        if item["projected"]: tags.append("📊 Projected")
        tag_str = "  ".join(tags) if tags else ""

        embed.add_field(
            name=f"{i}. {item['name']}",
            value=(
                f"RAP: **{int(item['rap'])}** | Value: **{int(item['value'])}** | Gap: **{item['gap']:.1f}%**\n"
                f"Demand: {demand_label} | Trend: {trend_label}\n"
                + (f"{tag_str}\n" if tag_str else "")
                + f"🔗 [Rolimons](https://www.rolimons.com/item/{item['id']})"
            ),
            inline=False,
        )

    embed.set_footer(text="Gap = (Value − RAP) / Value × 100.  Positive gap = potential upside.")
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
        {"max_price": max_price},
        trigger,
    )

    await channel.send(embed=embed)


# ================== LOOP ==================

async def hourly_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        await post_scan(
            "⏰ Auto Hourly Scan",
            AUTO_MAX_PRICE, AUTO_TOP_N,
            AUTO_MIN_RAP, AUTO_MIN_GAP, AUTO_MODE,
        )
        await asyncio.sleep(SCAN_INTERVAL)


# ================== COMMANDS ==================

@tree.command(
    name="scan",
    description="Scan for undervalued Roblox limiteds",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(
    max_price="Max RAP in Robux (default 200)",
    min_gap="Min gap % between RAP and Rolimons value (default 0)",
    mode="Sort mode: gap or score (default gap)",
)
async def scan_cmd(
    interaction: discord.Interaction,
    max_price: int = AUTO_MAX_PRICE,
    min_gap: int = AUTO_MIN_GAP,
    mode: str = AUTO_MODE,
):
    await interaction.response.send_message("🔎 Scanning — this takes a few seconds...", ephemeral=True)
    await post_scan(
        f"Manual scan by {interaction.user}",
        max_price, AUTO_TOP_N,
        AUTO_MIN_RAP, min_gap, mode,
    )


@tree.command(
    name="item",
    description="Look up a specific Roblox limited by name",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(name="Part of the item name to search for")
async def item_cmd(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        all_items = await fetch_rolimons_limiteds(session)

    matches = [i for i in all_items if name.lower() in i["name"].lower()]
    if not matches:
        await interaction.followup.send(f"No item found matching `{name}`.", ephemeral=True)
        return

    item = matches[0]
    gap = compute_gap(item["rap"], item["value"])
    item["gap"] = gap
    item["score"] = score_item(item)

    embed = discord.Embed(
        title=f"🔍 {item['name']}",
        url=f"https://www.rolimons.com/item/{item['id']}",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="RAP",    value=f"{int(item['rap'])} R$",   inline=True)
    embed.add_field(name="Value",  value=f"{int(item['value'])} R$", inline=True)
    embed.add_field(name="Gap",    value=f"{gap:.1f}%",              inline=True)
    embed.add_field(name="Demand", value=DEMAND_LABELS.get(item["demand"], "?"), inline=True)
    embed.add_field(name="Trend",  value=TREND_LABELS.get(item["trend"], "?"),  inline=True)
    embed.add_field(name="Score",  value=f"{item['score']:.1f}",     inline=True)

    tags = []
    if item["hyped"]:     tags.append("🔥 Hyped")
    if item["rare"]:      tags.append("💎 Rare")
    if item["projected"]: tags.append("📊 Projected")
    if tags:
        embed.add_field(name="Tags", value="  ".join(tags), inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


# ================== STARTUP ==================

@client.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    await tree.sync(guild=guild)
    print(f"✅ Logged in as {client.user} — commands synced to guild {GUILD_ID}")
    client.loop.create_task(hourly_loop())


client.run(TOKEN)
