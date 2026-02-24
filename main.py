import os
import asyncio
import random
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands

# ================== CONFIG ==================

TOKEN      = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
GUILD_ID   = 1475935690465480979

AUTO_MAX_PRICE     = 10_000   # R$ ceiling for undervalue scan
AUTO_TOP_N         = 10
AUTO_MIN_RAP       = 0
AUTO_MIN_GAP       = 0        # % gap between RAP and Rolimons value
AUTO_MODE          = "score"
SCAN_INTERVAL      = 3600     # seconds between auto scans

NEW_ITEM_COUNT     = 20       # how many "newest" items to surface
FORSALE_LIMIT      = 120      # max catalog items to fetch per page

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# ================== BOT SETUP ==================

intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)

# ================== LOOKUP TABLES ==================

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
    1: "Lowering",
    2: "Stable",
    3: "Raising",
    4: "Fluctuating",
    5: "Projected",
}

TREND_ICONS = {0: "", 1: "📉", 2: "➡️", 3: "📈", 4: "〰️", 5: "📊"}
DEMAND_ICONS = {0: "", 1: "❌", 2: "🔻", 3: "🟡", 4: "🟢", 5: "🚀"}

# ================== ROLIMONS CACHE ==================
# Rolimons item array indices:
# [0] name  [1] acronym  [2] rap  [3] value  [4] default_value
# [5] demand (0-5)  [6] trend (0-5)  [7] projected(1/-1)  [8] hyped(1/-1)  [9] rare(1/-1)

_rolimons_cache: Optional[Tuple[float, Dict[int, Dict]]] = None
ROLIMONS_CACHE_TTL = 300  # 5 minute cache


async def fetch_rolimons_raw(session: aiohttp.ClientSession) -> Dict[int, Dict]:
    global _rolimons_cache
    now = datetime.now(timezone.utc).timestamp()
    if _rolimons_cache and (now - _rolimons_cache[0]) < ROLIMONS_CACHE_TTL:
        return _rolimons_cache[1]

    url = "https://www.rolimons.com/itemapi/itemdetails"
    async with session.get(url, headers=HEADERS, timeout=30) as r:
        data = await r.json(content_type=None)

    lookup: Dict[int, Dict] = {}
    for asset_id, info in data.get("items", {}).items():
        try:
            aid   = int(asset_id)
            rap   = float(info[2]) if isinstance(info[2], (int, float)) and info[2] > 0 else 0.0
            value = float(info[3]) if isinstance(info[3], (int, float)) and info[3] > 0 else 0.0
            lookup[aid] = {
                "id":        aid,
                "name":      info[0],
                "rap":       rap,
                "value":     value,
                "demand":    int(info[5]) if isinstance(info[5], int) else 0,
                "trend":     int(info[6]) if isinstance(info[6], int) else 0,
                "projected": info[7] == 1,
                "hyped":     info[8] == 1,
                "rare":      info[9] == 1,
            }
        except Exception:
            continue

    _rolimons_cache = (now, lookup)
    return lookup


async def fetch_rolimons_list(session: aiohttp.ClientSession) -> List[Dict]:
    return list((await fetch_rolimons_raw(session)).values())


# ================== SCORING ==================

def compute_gap(rap: float, value: float) -> float:
    """
    Gap % = how far community value sits ABOVE RAP.
    Positive  → underpriced vs estimate (good to buy).
    Negative  → trading above estimate (risky).
    """
    if value <= 0:
        return 0.0
    return (value - rap) / value * 100


def score_item(item: Dict[str, Any]) -> float:
    """
    Composite buy score.
      gap      = core signal
      demand   = 0-5  → up to +20 pts
      trend    = 0-5  → up to +10 pts
      hyped    = +5, rare = +5, projected = -5 (extra risk)
    """
    gap    = item.get("gap", 0.0)
    demand = item.get("demand", 0)
    trend  = item.get("trend", 0)

    d_score = (demand / 5) * 20 if demand > 0 else 0
    t_score = (trend  / 5) * 10 if trend  > 0 else 0

    bonus = 0
    if item.get("hyped"):     bonus += 5
    if item.get("rare"):      bonus += 5
    if item.get("projected"): bonus -= 5

    return gap + d_score + t_score + bonus


def buy_reason(item: Dict[str, Any]) -> str:
    """Human-readable buy rationale for a single item."""
    parts = []

    gap = item.get("gap", 0)
    if gap >= 30:
        parts.append(f"value is **{gap:.0f}% above RAP** — strong upside potential")
    elif gap >= 10:
        parts.append(f"trades **{gap:.0f}% below community value** — looks underpriced")
    elif gap >= 0:
        parts.append("fairly priced relative to community value")
    else:
        parts.append(f"⚠️ currently trading **{abs(gap):.0f}% above** estimated value — risky")

    demand = item.get("demand", 0)
    if demand >= 4:
        parts.append(f"demand is **{DEMAND_LABELS[demand]}** — easy to resell")
    elif demand in (2, 3):
        parts.append(f"demand is **{DEMAND_LABELS[demand]}**")
    elif demand == 1:
        parts.append("⚠️ demand is **Terrible** — hard to resell")

    trend = item.get("trend", 0)
    if trend == 3:
        parts.append("price is **actively rising** 📈")
    elif trend == 2:
        parts.append("price is **stable**")
    elif trend == 1:
        parts.append("⚠️ price is **lowering** — proceed with caution")

    if item.get("hyped"):     parts.append("🔥 currently hyped")
    if item.get("rare"):      parts.append("💎 rare item — holds value well")
    if item.get("projected"): parts.append("📊 value is projected (not confirmed)")

    return " · ".join(parts) if parts else "No strong signals available."


# ================== CATALOG (FOR-SALE) ==================

async def fetch_forsale_limiteds(session: aiohttp.ClientSession) -> List[Dict]:
    """
    Fetch limiteds currently on sale via Roblox catalog API
    (NOT the economy/resale endpoint that gets blocked on Railway).
    Enrich with Rolimons value data and score each item.
    """
    url = "https://catalog.roblox.com/v1/search/items"
    params = {
        "category":        "Collectibles",
        "salesTypeFilter": 1,    # 1 = actively for sale by Roblox
        "limit":           FORSALE_LIMIT,
        "sortType":        3,    # newest first
    }

    catalog_ids: List[int]      = []
    catalog_prices: Dict[int, int] = {}

    try:
        async with session.get(url, params=params, headers=HEADERS, timeout=20) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                for entry in data.get("data", []):
                    aid   = entry.get("id")
                    price = entry.get("lowestPrice") or entry.get("price") or 0
                    if aid:
                        catalog_ids.append(int(aid))
                        catalog_prices[int(aid)] = int(price)
    except Exception as e:
        print(f"[forsale] Catalog fetch error: {e}")

    if not catalog_ids:
        return []

    rolimons = await fetch_rolimons_raw(session)
    results: List[Dict] = []

    for aid in catalog_ids:
        base = rolimons.get(aid, {
            "id": aid, "name": f"New Item #{aid}",
            "rap": 0.0, "value": 0.0,
            "demand": 0, "trend": 0,
            "projected": False, "hyped": False, "rare": False,
        })
        enriched              = dict(base)
        enriched["sale_price"] = catalog_prices.get(aid, 0)
        enriched["gap"]        = compute_gap(enriched["rap"], enriched["value"])
        enriched["score"]      = score_item(enriched)
        results.append(enriched)

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# ================== NEW RELEASES ==================

async def fetch_new_releases(session: aiohttp.ClientSession, count: int = NEW_ITEM_COUNT) -> List[Dict]:
    """
    New = highest asset IDs in Rolimons (higher ID = created more recently on Roblox).
    Items so new Rolimons hasn't priced yet won't appear here,
    but the /forsale command catches those.
    """
    all_items = await fetch_rolimons_list(session)
    all_items.sort(key=lambda x: x["id"], reverse=True)
    newest = all_items[:count]
    for item in newest:
        item["gap"]   = compute_gap(item["rap"], item["value"])
        item["score"] = score_item(item)
    return newest


# ================== EMBED HELPERS ==================

def _item_line(item: Dict) -> str:
    d_icon = DEMAND_ICONS.get(item.get("demand", 0), "")
    t_icon = TREND_ICONS.get(item.get("trend", 0), "")
    d_lbl  = DEMAND_LABELS.get(item.get("demand", 0), "?")
    t_lbl  = TREND_LABELS.get(item.get("trend", 0), "?")

    tags = ""
    if item.get("hyped"): tags += " 🔥"
    if item.get("rare"):  tags += " 💎"

    sale = f"  |  **On Sale: {item['sale_price']} R$**" if item.get("sale_price") else ""

    return (
        f"RAP: **{int(item['rap'])}** | Value: **{int(item['value'])}** | "
        f"Gap: **{item['gap']:.1f}%**{sale}{tags}\n"
        f"{d_icon} Demand: {d_lbl}  {t_icon} Trend: {t_lbl}\n"
        f"🔗 [Rolimons](https://www.rolimons.com/item/{item['id']})  "
        f"• [Roblox](https://www.roblox.com/catalog/{item['id']})"
    )


def build_undervalue_embed(items, scanned, qualified, max_price, trigger):
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    embed = discord.Embed(
        title=f"📈 Undervalue Scan — RAP ≤ {max_price:,} R$",
        description=f"{trigger}\n{now}",
        color=discord.Color.green(),
    )
    embed.add_field(
        name="Results",
        value=f"Checked **{scanned:,}** items · **{qualified}** qualified",
        inline=False,
    )
    if not items:
        embed.add_field(
            name="No Results",
            value="Nothing matched. Try raising `max_price` or lowering `min_gap`.",
            inline=False,
        )
        return embed

    for i, item in enumerate(items, 1):
        embed.add_field(name=f"{i}. {item['name']}", value=_item_line(item), inline=False)

    embed.set_footer(text="Gap = (Value − RAP) / Value × 100  |  Positive = potential upside")
    return embed


def build_new_releases_embed(items: List[Dict]) -> discord.Embed:
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    embed = discord.Embed(
        title="🆕 Newest Limiteds on Rolimons",
        description=(
            f"Sorted by asset ID (higher = more recently created) · {now}\n"
            "RAP / Value may be unset for brand-new items — use `/forsale` to see what you can buy right now."
        ),
        color=discord.Color.gold(),
    )
    if not items:
        embed.add_field(name="No Data", value="Could not fetch items from Rolimons.", inline=False)
        return embed

    for i, item in enumerate(items, 1):
        embed.add_field(name=f"{i}. {item['name']}", value=_item_line(item), inline=False)

    return embed


def build_forsale_embed(items: List[Dict]) -> discord.Embed:
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    embed = discord.Embed(
        title="🛒 Limiteds Currently On Sale from Roblox",
        description=f"Ranked by buy score · {now}",
        color=discord.Color.blue(),
    )

    if not items:
        embed.add_field(
            name="None Found",
            value=(
                "Roblox catalog returned no for-sale limiteds right now.\n"
                "This can happen if Railway's IP is temporarily rate-limited by the catalog API.\n"
                "Try `/forsale` again in a few minutes, or use `/scan` to find resale deals."
            ),
            inline=False,
        )
        return embed

    # Top pick callout
    best = items[0]
    embed.add_field(
        name=f"⭐ BEST BUY → {best['name']}",
        value=(
            f"{buy_reason(best)}\n"
            f"**Sale: {best['sale_price']} R$** | RAP: {int(best['rap'])} | "
            f"Value: {int(best['value'])} | Score: {best['score']:.1f}\n"
            f"[🛒 Buy now on Roblox](https://www.roblox.com/catalog/{best['id']})"
        ),
        inline=False,
    )

    # Remaining items
    for i, item in enumerate(items[1:9], 2):
        embed.add_field(name=f"{i}. {item['name']}", value=_item_line(item), inline=False)

    embed.set_footer(text="Score = gap + demand + trend + bonuses  |  Higher = better opportunity")
    return embed


def build_buynow_embed(item: Dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"💰 BUY NOW: {item['name']}",
        url=f"https://www.roblox.com/catalog/{item['id']}",
        description=buy_reason(item),
        color=discord.Color.brand_red(),
    )
    embed.add_field(name="Sale Price", value=f"**{item.get('sale_price', '?')} R$**", inline=True)
    embed.add_field(name="RAP",        value=f"{int(item['rap'])} R$",                inline=True)
    embed.add_field(name="Value Est.", value=f"{int(item['value'])} R$",              inline=True)
    embed.add_field(name="Gap",        value=f"{item['gap']:.1f}%",                   inline=True)
    embed.add_field(
        name="Demand",
        value=f"{DEMAND_ICONS.get(item['demand'],'')} {DEMAND_LABELS.get(item['demand'],'?')}",
        inline=True,
    )
    embed.add_field(
        name="Trend",
        value=f"{TREND_ICONS.get(item['trend'],'')} {TREND_LABELS.get(item['trend'],'?')}",
        inline=True,
    )
    embed.add_field(name="Buy Score", value=f"{item['score']:.1f}", inline=True)

    tags = []
    if item.get("hyped"):     tags.append("🔥 Hyped")
    if item.get("rare"):      tags.append("💎 Rare")
    if item.get("projected"): tags.append("📊 Projected (treat value with caution)")
    if tags:
        embed.add_field(name="Signals", value="  ".join(tags), inline=False)

    embed.add_field(
        name="Links",
        value=(
            f"[🛒 Buy on Roblox](https://www.roblox.com/catalog/{item['id']})  "
            f"[📊 Rolimons](https://www.rolimons.com/item/{item['id']})"
        ),
        inline=False,
    )
    embed.set_footer(text="This is not financial advice. Limiteds carry real risk — always research before buying.")
    return embed


# ================== POST HELPERS ==================

async def _get_channel():
    if CHANNEL_ID == 0:
        return None
    return client.get_channel(CHANNEL_ID)


async def post_undervalue(trigger, max_price=AUTO_MAX_PRICE, top_n=AUTO_TOP_N,
                          min_rap=AUTO_MIN_RAP, min_gap=AUTO_MIN_GAP, mode=AUTO_MODE):
    channel = await _get_channel()
    if not channel:
        return
    items, scanned, qualified = await run_scan(max_price, top_n, min_rap, min_gap, mode)
    await channel.send(embed=build_undervalue_embed(items, scanned, qualified, max_price, trigger))


async def run_scan(max_price, top_n, min_rap, min_gap, mode):
    async with aiohttp.ClientSession() as session:
        all_items = await fetch_rolimons_list(session)

    candidates = [
        i for i in all_items
        if i["rap"] > 0 and i["rap"] <= max_price and i["rap"] >= min_rap
    ]

    results = []
    for item in candidates:
        gap = compute_gap(item["rap"], item["value"])
        if gap < min_gap:
            continue
        item["gap"]   = gap
        item["score"] = score_item(item)
        results.append(item)

    sort_key = "score" if mode == "score" else "gap"
    results.sort(key=lambda x: x[sort_key], reverse=True)
    return results[:min(top_n, 25)], len(candidates), len(results)


# ================== HOURLY LOOP ==================

async def hourly_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
        print(f"[{now_str}] Running auto scans...")
        await post_undervalue("⏰ Auto Hourly — Undervalue Scan")

        channel = await _get_channel()
        if channel:
            async with aiohttp.ClientSession() as session:
                forsale_items  = await fetch_forsale_limiteds(session)
                new_items      = await fetch_new_releases(session)
            await channel.send(embed=build_forsale_embed(forsale_items))
            await channel.send(embed=build_new_releases_embed(new_items))

        await asyncio.sleep(SCAN_INTERVAL)


# ================== SLASH COMMANDS ==================

@tree.command(
    name="scan",
    description="Scan for undervalued limiteds (trading below community value)",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(
    max_price="Max RAP in Robux (default 10000)",
    min_gap="Minimum gap % between RAP and value (default 0)",
    mode="Sort by: gap or score (default score)",
)
async def scan_cmd(
    interaction: discord.Interaction,
    max_price: int = AUTO_MAX_PRICE,
    min_gap: int   = AUTO_MIN_GAP,
    mode: str      = AUTO_MODE,
):
    await interaction.response.send_message("🔎 Scanning for undervalued limiteds...", ephemeral=True)
    await post_undervalue(
        f"Manual scan by {interaction.user}",
        max_price, AUTO_TOP_N, AUTO_MIN_RAP, min_gap, mode,
    )


@tree.command(
    name="new",
    description="Show the newest Roblox limiteds (recently created)",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(count="How many to show (default 15, max 25)")
async def new_cmd(interaction: discord.Interaction, count: int = 15):
    await interaction.response.send_message("🆕 Fetching newest limiteds...", ephemeral=True)
    async with aiohttp.ClientSession() as session:
        items = await fetch_new_releases(session, min(count, 25))
    channel = await _get_channel()
    if channel:
        await channel.send(embed=build_new_releases_embed(items))


@tree.command(
    name="forsale",
    description="Show limiteds you can buy from Roblox right now + best value pick",
    guild=discord.Object(id=GUILD_ID),
)
async def forsale_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("🛒 Checking what's on sale right now...", ephemeral=True)
    async with aiohttp.ClientSession() as session:
        items = await fetch_forsale_limiteds(session)
    channel = await _get_channel()
    if channel:
        await channel.send(embed=build_forsale_embed(items))


@tree.command(
    name="buynow",
    description="Get ONE best-buy pick from limiteds currently on sale",
    guild=discord.Object(id=GUILD_ID),
)
async def buynow_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("💰 Finding the single best buy right now...", ephemeral=True)
    async with aiohttp.ClientSession() as session:
        items = await fetch_forsale_limiteds(session)

    channel = await _get_channel()
    if not channel:
        return

    if not items:
        await channel.send("❌ No purchaseable limiteds found right now. Try again in a few minutes.")
        return

    # Prefer items that have actual Rolimons data
    scored = [i for i in items if i["value"] > 0 and i["rap"] > 0]
    pick   = scored[0] if scored else items[0]
    await channel.send(embed=build_buynow_embed(pick))


@tree.command(
    name="item",
    description="Look up a specific limited by name",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(name="Part of the item name to search for")
async def item_cmd(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        all_items = await fetch_rolimons_list(session)

    matches = [i for i in all_items if name.lower() in i["name"].lower()]
    if not matches:
        await interaction.followup.send(f"No item found matching `{name}`.", ephemeral=True)
        return

    item          = matches[0]
    item["gap"]   = compute_gap(item["rap"], item["value"])
    item["score"] = score_item(item)

    embed = discord.Embed(
        title=f"🔍 {item['name']}",
        url=f"https://www.rolimons.com/item/{item['id']}",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="RAP",    value=f"{int(item['rap'])} R$",    inline=True)
    embed.add_field(name="Value",  value=f"{int(item['value'])} R$",  inline=True)
    embed.add_field(name="Gap",    value=f"{item['gap']:.1f}%",       inline=True)
    embed.add_field(
        name="Demand",
        value=f"{DEMAND_ICONS.get(item['demand'],'')} {DEMAND_LABELS.get(item['demand'],'?')}",
        inline=True,
    )
    embed.add_field(
        name="Trend",
        value=f"{TREND_ICONS.get(item['trend'],'')} {TREND_LABELS.get(item['trend'],'?')}",
        inline=True,
    )
    embed.add_field(name="Score",  value=f"{item['score']:.1f}",      inline=True)

    tags = []
    if item.get("hyped"):     tags.append("🔥 Hyped")
    if item.get("rare"):      tags.append("💎 Rare")
    if item.get("projected"): tags.append("📊 Projected")
    if tags:
        embed.add_field(name="Signals", value="  ".join(tags), inline=False)

    embed.add_field(name="Verdict", value=buy_reason(item), inline=False)
    embed.add_field(
        name="Links",
        value=(
            f"[📊 Rolimons](https://www.rolimons.com/item/{item['id']})  "
            f"• [🛒 Roblox](https://www.roblox.com/catalog/{item['id']})"
        ),
        inline=False,
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


# ================== STARTUP ==================

@client.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    await tree.sync(guild=guild)
    print(f"✅ {client.user} is online — commands synced to guild {GUILD_ID}")
    client.loop.create_task(hourly_loop())


client.run(TOKEN)
