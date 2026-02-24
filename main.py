import os
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple

import aiohttp
import discord
from discord import app_commands

# =========================
# CONFIG
# =========================

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

# Your server ID (guild) for instant slash commands
GUILD_ID = 1475935690465480979

# Auto-post defaults
AUTO_MAX_PRICE = 200
AUTO_TOP_N = 10
AUTO_MIN_RAP = 0
AUTO_MIN_GAP_PCT = -999  # include everything; negative means allow lowest > RAP too
AUTO_MODE = "gap"        # gap | momentum | mixed
ANALYZE_LIMIT = 50
SCAN_EVERY_SECONDS = 3600  # 1 hour

# Concurrency limits
MAX_CONCURRENT_REQUESTS = 10

# =========================

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# =========================
# ROBLOX DATA FETCHING
# =========================

async def fetch_catalog_candidates(session: aiohttp.ClientSession, max_price: int, limit: int) -> List[int]:
    """
    Get a candidate list from Roblox catalog search restricted by max_price.
    NOTE: Roblox may change params; if results go empty, adjust sortType.
    """
    url = (
        "https://catalog.roblox.com/v2/search/items/details"
        f"?salesTypeFilter=2"
        f"&limit={limit}"
        f"&minPrice=0"
        f"&maxPrice={max_price}"
        f"&sortType=3"
    )
    headers = {"User-Agent": "LimitedTrendBot/1.0", "Accept": "application/json"}
    async with session.get(url, headers=headers, timeout=25) as r:
        data = await r.json()

    ids: List[int] = []
    for item in data.get("data", []):
        if item.get("itemType") == "Asset" and isinstance(item.get("id"), int):
            ids.append(item["id"])
    return ids


async def fetch_resale_data(session: aiohttp.ClientSession, asset_id: int) -> Optional[Dict[str, Any]]:
    """
    Pull structured resale data for an asset:
    - recentAveragePrice (RAP)
    - lowestResalePrice
    - sales count / volume may not always be present
    """
    url = f"https://economy.roblox.com/v1/assets/{asset_id}/resale-data"
    headers = {"User-Agent": "LimitedTrendBot/1.0", "Accept": "application/json"}

    async with session.get(url, headers=headers, timeout=25) as r:
        if r.status != 200:
            return None
        data = await r.json()

    rap = data.get("recentAveragePrice")
    lowest = data.get("lowestResalePrice")

    if not isinstance(rap, (int, float)) or not isinstance(lowest, (int, float)):
        return None

    # Optional fields (may or may not exist)
    num_sellers = data.get("numSellers")
    sales = data.get("sales")  # sometimes present, sometimes not

    return {
        "asset_id": asset_id,
        "rap": float(rap),
        "lowest": float(lowest),
        "num_sellers": int(num_sellers) if isinstance(num_sellers, int) else None,
        "sales": int(sales) if isinstance(sales, int) else None,
    }


async def fetch_asset_names(session: aiohttp.ClientSession, asset_ids: List[int]) -> Dict[int, str]:
    """
    Resolve names for assets in batch.
    """
    if not asset_ids:
        return {}

    url = "https://catalog.roblox.com/v1/catalog/items/details"
    headers = {
        "User-Agent": "LimitedTrendBot/1.0",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {"items": [{"itemType": "Asset", "id": aid} for aid in asset_ids]}

    async with session.post(url, json=payload, headers=headers, timeout=30) as r:
        if r.status != 200:
            return {}
        data = await r.json()

    out: Dict[int, str] = {}
    for item in data.get("data", []):
        try:
            aid = int(item.get("id"))
            name = str(item.get("name", f"Asset {aid}"))
            out[aid] = name
        except Exception:
            continue
    return out


# =========================
# SCORING + RISK
# =========================

def compute_metrics(rap: float, lowest: float) -> Dict[str, float]:
    """
    - gap_pct: (RAP - lowest)/RAP * 100
    - under_rap: positive means lowest below RAP (potentially undervalued)
    """
    if rap <= 0:
        return {"gap_pct": 0.0, "under_rap": 0.0}
    gap = (rap - lowest) / rap * 100.0
    return {"gap_pct": gap, "under_rap": (rap - lowest) / rap}


def risk_rating(rap: float, lowest: float) -> str:
    """
    Simple risk signal:
    - If lowest is far ABOVE RAP → hype/overpay risk
    - If lowest is far BELOW RAP → could be good OR could be RAP manipulation, call it Medium
    """
    if rap <= 0:
        return "High"

    gap_pct = (rap - lowest) / rap * 100.0

    # lowest >> RAP means you're paying above average recent sales
    if gap_pct < -20:
        return "High"
    # lowest slightly above RAP
    if -20 <= gap_pct < 0:
        return "Medium"
    # lowest below RAP
    if 0 <= gap_pct < 25:
        return "Low"
    # very far below RAP can be either opportunity or manipulated RAP; keep it Medium
    return "Medium"


def opportunity_score(rap: float, lowest: float, mode: str) -> float:
    """
    Modes:
    - gap: prefer items where lowest << RAP
    - momentum: prefer items where lowest >= RAP (breakouts) but not too extreme
    - mixed: blend both
    """
    if rap <= 0:
        return -9999.0

    gap_pct = (rap - lowest) / rap * 100.0  # positive if lowest below RAP

    if mode == "gap":
        return gap_pct
    elif mode == "momentum":
        # favor mild negative gap (lowest slightly above rap), penalize extreme
        # peak at -5%, drops off if too high/too low
        return -abs(gap_pct + 5)
    else:  # mixed
        # reward undervalue, but still not crazy
        return gap_pct - (abs(gap_pct) * 0.15)


# =========================
# SCAN CORE
# =========================

async def run_scan(
    max_price: int,
    top_n: int,
    min_rap: int,
    min_gap_pct: float,
    mode: str,
) -> Dict[str, Any]:
    """
    Pull up to ANALYZE_LIMIT candidates and compute ranked results.
    Always returns something (even if empty list).
    """
    async with aiohttp.ClientSession() as session:
        candidates = await fetch_catalog_candidates(session, max_price=max_price, limit=ANALYZE_LIMIT)

        sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        resale_results: List[Dict[str, Any]] = []

        async def worker(aid: int):
            async with sem:
                d = await fetch_resale_data(session, aid)
                if d:
                    resale_results.append(d)

        await asyncio.gather(*(worker(aid) for aid in candidates))

        # Filter by max_price, min_rap, min_gap_pct
        filtered = []
        for x in resale_results:
            rap = x["rap"]
            lowest = x["lowest"]

            if lowest > max_price:
                continue
            if rap < min_rap:
                continue

            metrics = compute_metrics(rap, lowest)
            gap_pct = metrics["gap_pct"]

            # include everything by default; user can restrict
            if gap_pct < min_gap_pct:
                continue

            x["gap_pct"] = gap_pct
            x["risk"] = risk_rating(rap, lowest)
            x["score"] = opportunity_score(rap, lowest, mode)
            filtered.append(x)

        filtered.sort(key=lambda z: z["score"], reverse=True)

        top = filtered[:max(1, min(top_n, 25))]  # cap top_n at 25 to avoid spam

        # Resolve names for top items
        names = await fetch_asset_names(session, [t["asset_id"] for t in top])

        for t in top:
            t["name"] = names.get(t["asset_id"], f"Asset {t['asset_id']}")

        return {
            "candidates": len(candidates),
            "scored": len(filtered),
            "top": top,
            "params": {
                "max_price": max_price,
                "top_n": top_n,
                "min_rap": min_rap,
                "min_gap_pct": min_gap_pct,
                "mode": mode,
            },
        }


# =========================
# EMBED OUTPUT
# =========================

def make_embed(result: Dict[str, Any], trigger: str) -> discord.Embed:
    p = result["params"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    embed = discord.Embed(
        title=f"📈 Limited Scan ≤ {p['max_price']}R$ (Top {min(p['top_n'], 25)})",
        description=f"Trigger: {trigger}\nMode: **{p['mode']}** | min_rap: **{p['min_rap']}** | min_gap: **{p['min_gap_pct']}%**\nChecked: {now}",
        color=discord.Color.green(),
    )

    embed.add_field(
        name="Scan Summary",
        value=f"Candidates: **{result['candidates']}**\nScored: **{result['scored']}**",
        inline=False,
    )

    if not result["top"]:
        embed.add_field(
            name="No results",
            value="No items matched the filters this scan.",
            inline=False,
        )
        return embed

    # Build concise top list
    lines = []
    for i, item in enumerate(result["top"], start=1):
        aid = item["asset_id"]
        name = item["name"]
        lowest = int(item["lowest"])
        rap = int(item["rap"])
        gap = float(item.get("gap_pct", 0.0))
        risk = item.get("risk", "Medium")

        lines.append(
            f"**{i}. {name}** (`{aid}`)\n"
            f"Lowest: **{lowest}** | RAP: **{rap}** | Gap: **{gap:+.1f}%** | Risk: **{risk}**"
        )

    embed.add_field(name="Top Picks", value="\n\n".join(lines), inline=False)
    return embed


# =========================
# POSTING HELPERS
# =========================

async def post_scan_to_channel(trigger: str, max_price: int, top_n: int, min_rap: int, min_gap_pct: float, mode: str):
    if CHANNEL_ID == 0:
        print("CHANNEL_ID is not set. Add it in Railway Variables.")
        return

    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        print("Could not find channel. Check CHANNEL_ID and bot access.")
        return

    try:
        result = await run_scan(
            max_price=max_price,
            top_n=top_n,
            min_rap=min_rap,
            min_gap_pct=min_gap_pct,
            mode=mode,
        )
        embed = make_embed(result, trigger)
        await channel.send(embed=embed)
    except Exception as e:
        await channel.send(f"⚠️ Scan failed: `{type(e).__name__}: {e}`")


async def hourly_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        await post_scan_to_channel(
            trigger="auto-hourly",
            max_price=AUTO_MAX_PRICE,
            top_n=AUTO_TOP_N,
            min_rap=AUTO_MIN_RAP,
            min_gap_pct=AUTO_MIN_GAP_PCT,
            mode=AUTO_MODE,
        )
        await asyncio.sleep(SCAN_EVERY_SECONDS)


# =========================
# SLASH COMMANDS
# =========================

@tree.command(
    name="profit",
    description="Calculate profit after Roblox 30% fee (you receive 70%).",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(
    buy_price="Price you bought the item for",
    sell_price="Sell price you plan to list at",
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
    description="Scan limiteds now (posts publicly) with custom filters.",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(
    max_price="Maximum Robux price (default 200)",
    top_n="How many items to show (max 25, default 10)",
    min_rap="Minimum RAP to include (default 0)",
    min_gap_pct="Minimum gap % (RAP-lowest)/RAP*100 (default -999 = include everything)",
    mode="Ranking mode: gap | momentum | mixed (default gap)",
)
async def scan(
    interaction: discord.Interaction,
    max_price: Optional[int] = None,
    top_n: Optional[int] = None,
    min_rap: Optional[int] = None,
    min_gap_pct: Optional[float] = None,
    mode: Optional[str] = None,
):
    mp = max(1, min(int(max_price) if max_price is not None else AUTO_MAX_PRICE, 100000))
    tn = max(1, min(int(top_n) if top_n is not None else AUTO_TOP_N, 25))
    mr = max(0, int(min_rap) if min_rap is not None else AUTO_MIN_RAP)
    mg = float(min_gap_pct) if min_gap_pct is not None else AUTO_MIN_GAP_PCT
    md = (mode or AUTO_MODE).lower()
    if md not in ("gap", "momentum", "mixed"):
        md = "gap"

    await interaction.response.send_message(
        f"🔎 Scanning now… (≤{mp}R$, top {tn}, min_rap {mr}, min_gap {mg}%, mode {md})",
        ephemeral=True,
    )

    await post_scan_to_channel(
        trigger=f"manual /scan by {interaction.user}",
        max_price=mp,
        top_n=tn,
        min_rap=mr,
        min_gap_pct=mg,
        mode=md,
    )


# =========================
# READY
# =========================

@client.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    await tree.sync(guild=guild)
    print(f"Synced commands to guild {GUILD_ID}")
    print(f"Logged in as {client.user}")
    client.loop.create_task(hourly_loop())


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN not set")

client.run(TOKEN)
