import os
import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands

# ================== CONFIG ==================

TOKEN      = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
GUILD_ID   = 1475935690465480979

AUTO_MAX_PRICE  = 10_000
AUTO_TOP_N      = 10
AUTO_MIN_RAP    = 0
AUTO_MIN_GAP    = 0
AUTO_MODE       = "score"
SCAN_INTERVAL   = 3600

NEW_ITEM_COUNT  = 20
FORSALE_LIMIT   = 120

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

DEMAND_LABELS = {0: "Unassigned", 1: "Terrible", 2: "Low", 3: "Normal", 4: "High", 5: "Amazing"}
TREND_LABELS  = {0: "Unassigned", 1: "Lowering", 2: "Stable", 3: "Raising", 4: "Fluctuating", 5: "Projected"}
DEMAND_ICONS  = {0: "", 1: "❌", 2: "🔻", 3: "🟡", 4: "🟢", 5: "🚀"}
TREND_ICONS   = {0: "", 1: "📉", 2: "➡️", 3: "📈", 4: "〰️", 5: "📊"}

CATALOG_SUBCATEGORY_MAP = {
    "hats":       4,
    "faces":      7,
    "gear":       19,
    "heads":      2,
    "accessories":61,
}

# ================== ROLIMONS CACHE ==================
# [0] name  [1] acronym  [2] rap  [3] value  [4] default_value
# [5] demand(0-5)  [6] trend(0-5)  [7] projected  [8] hyped  [9] rare

_rolimons_cache: Optional[Tuple[float, Dict[int, Dict]]] = None
ROLIMONS_CACHE_TTL = 300


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


# ================== ROBLOX CATALOG DETAILS API ==================

async def fetch_item_details(session: aiohttp.ClientSession, asset_ids: List[int]) -> List[Dict]:
    """
    POST to catalog details endpoint to get description, creator,
    stock remaining, price, and asset type for a batch of IDs.
    Returns a list of raw detail dicts from Roblox.
    """
    url  = "https://catalog.roblox.com/v1/catalog/items/details"
    body = {"items": [{"itemType": "Asset", "id": aid} for aid in asset_ids]}
    try:
        async with session.post(url, json=body, headers=HEADERS, timeout=20) as r:
            if r.status != 200:
                return []
            data = await r.json(content_type=None)
            return data.get("data", [])
    except Exception as e:
        print(f"[catalog details] Error: {e}")
        return []


async def fetch_single_item_details(session: aiohttp.ClientSession, asset_id: int) -> Optional[Dict]:
    """Fetch detailed Roblox page data for one item."""
    results = await fetch_item_details(session, [asset_id])
    return results[0] if results else None


async def fetch_creator_name(session: aiohttp.ClientSession, creator_id: int, creator_type: str) -> str:
    """Resolve a creator ID to a display name."""
    try:
        if creator_type == "Group":
            url = f"https://groups.roblox.com/v1/groups/{creator_id}"
        else:
            url = f"https://users.roblox.com/v1/users/{creator_id}"
        async with session.get(url, headers=HEADERS, timeout=10) as r:
            if r.status == 200:
                d = await r.json(content_type=None)
                return d.get("name") or d.get("displayName") or "Unknown"
    except Exception:
        pass
    return "Unknown"


# ================== ROBLOX ECONOMY / SALES ==================

async def fetch_recent_sales(session: aiohttp.ClientSession, asset_id: int) -> Dict:
    """
    Try two Roblox endpoints for price/sales history.
    Both may be blocked on Railway (cloud IP block).
    Falls back to Rolimons data if both fail.
    """
    result = {
        "price_datapoints": [],   # list of {date, avg}
        "resale_records":   [],   # list of recent individual sales
        "source":           None,
    }

    # Attempt 1 — resale data (includes RAP and price history datapoints)
    try:
        url = f"https://economy.roblox.com/v1/assets/{asset_id}/resale-data"
        async with session.get(url, headers=HEADERS, timeout=10) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                result["price_datapoints"] = data.get("priceDataPoints", [])
                result["source"] = "Roblox Economy API"
                return result
    except Exception:
        pass

    # Attempt 2 — resale records (individual recent transactions)
    try:
        url = f"https://economy.roblox.com/v2/assets/{asset_id}/resale-records?limit=10&cursor="
        async with session.get(url, headers=HEADERS, timeout=10) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                result["resale_records"] = data.get("data", [])
                result["source"] = "Roblox Resale Records"
                return result
    except Exception:
        pass

    # Fallback — Rolimons has RAP + value, which implies recent trade history
    result["source"] = "rolimons_fallback"
    return result


async def fetch_rolimons_sales_page(session: aiohttp.ClientSession, asset_id: int) -> List[Dict]:
    """
    Rolimons item page exposes recent trade/sale activity as JSON.
    This is separate from the itemdetails API.
    """
    url = f"https://www.rolimons.com/itemapi/item/{asset_id}"
    try:
        async with session.get(url, headers=HEADERS, timeout=15) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                # recent_trades is a list of [timestamp, price, ...]
                return data.get("recent_trades", [])
    except Exception:
        pass
    return []


# ================== CATALOG SEARCH ==================

async def fetch_forsale_limiteds(session: aiohttp.ClientSession,
                                  max_price: int = 0,
                                  subcategory: int = 0) -> List[Dict]:
    """Fetch limiteds currently on sale from Roblox catalog, enriched with Rolimons data."""
    url    = "https://catalog.roblox.com/v1/search/items"
    params: Dict[str, Any] = {
        "category":        "Collectibles",
        "salesTypeFilter": 1,
        "limit":           FORSALE_LIMIT,
        "sortType":        3,
    }
    if max_price > 0:
        params["maxPrice"] = max_price
    if subcategory > 0:
        params["subcategory"] = subcategory

    catalog_ids: List[int]         = []
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
        print(f"[forsale] Catalog error: {e}")

    if not catalog_ids:
        return []

    # Enrich with Rolimons + Roblox catalog details in parallel
    rolimons    = await fetch_rolimons_raw(session)
    rblx_detail_list = await fetch_item_details(session, catalog_ids[:50])  # batch up to 50
    rblx_detail_map: Dict[int, Dict] = {d["id"]: d for d in rblx_detail_list if "id" in d}

    results: List[Dict] = []
    for aid in catalog_ids:
        base = rolimons.get(aid, {
            "id": aid, "name": f"New Item #{aid}",
            "rap": 0.0, "value": 0.0,
            "demand": 0, "trend": 0,
            "projected": False, "hyped": False, "rare": False,
        })
        enriched = dict(base)
        enriched["sale_price"] = catalog_prices.get(aid, 0)
        enriched["gap"]        = compute_gap(enriched["rap"], enriched["value"])
        enriched["score"]      = score_item(enriched)

        # Pull extra catalog page data if available
        rblx = rblx_detail_map.get(aid, {})
        enriched["stock_remaining"] = rblx.get("unitsAvailableForConsumption")
        enriched["total_sold"]      = rblx.get("countRemaining")  # some items expose this
        enriched["description"]     = (rblx.get("description") or "")[:120]
        enriched["creator_name"]    = rblx.get("creatorName", "")
        enriched["creator_type"]    = rblx.get("creatorType", "")

        results.append(enriched)

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


async def fetch_new_releases(session: aiohttp.ClientSession, count: int = NEW_ITEM_COUNT) -> List[Dict]:
    all_items = await fetch_rolimons_list(session)
    all_items.sort(key=lambda x: x["id"], reverse=True)
    newest = all_items[:count]
    for item in newest:
        item["gap"]   = compute_gap(item["rap"], item["value"])
        item["score"] = score_item(item)
    return newest


# ================== SCORING ==================

def compute_gap(rap: float, value: float) -> float:
    if value <= 0:
        return 0.0
    return (value - rap) / value * 100


def score_item(item: Dict[str, Any]) -> float:
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
    parts = []
    gap = item.get("gap", 0)
    if gap >= 30:
        parts.append(f"value is **{gap:.0f}% above RAP** — strong upside")
    elif gap >= 10:
        parts.append(f"trades **{gap:.0f}% below community value** — underpriced")
    elif gap >= 0:
        parts.append("fairly priced vs community value")
    else:
        parts.append(f"⚠️ **{abs(gap):.0f}% above** estimated value — risky")

    demand = item.get("demand", 0)
    if demand >= 4:
        parts.append(f"demand is **{DEMAND_LABELS[demand]}** — easy to resell")
    elif demand in (2, 3):
        parts.append(f"demand is **{DEMAND_LABELS[demand]}**")
    elif demand == 1:
        parts.append("⚠️ demand is **Terrible** — hard to resell")

    trend = item.get("trend", 0)
    if trend == 3:   parts.append("price **actively rising** 📈")
    elif trend == 2: parts.append("price is **stable**")
    elif trend == 1: parts.append("⚠️ price **lowering** — be cautious")

    if item.get("hyped"): parts.append("🔥 currently hyped")
    if item.get("rare"):  parts.append("💎 rare item")
    return " · ".join(parts) if parts else "No strong signals."


# ================== SCAN ==================

async def run_scan(max_price, top_n, min_rap, min_gap, mode):
    async with aiohttp.ClientSession() as session:
        all_items = await fetch_rolimons_list(session)
    candidates = [i for i in all_items if i["rap"] > 0 and i["rap"] <= max_price and i["rap"] >= min_rap]
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


# ================== EMBED HELPERS ==================

def _item_line(item: Dict) -> str:
    d_icon = DEMAND_ICONS.get(item.get("demand", 0), "")
    t_icon = TREND_ICONS.get(item.get("trend", 0), "")
    d_lbl  = DEMAND_LABELS.get(item.get("demand", 0), "?")
    t_lbl  = TREND_LABELS.get(item.get("trend", 0), "?")
    tags   = ("🔥" if item.get("hyped") else "") + ("💎" if item.get("rare") else "")
    sale   = f"  |  **On Sale: {item['sale_price']} R$**" if item.get("sale_price") else ""

    stock_str = ""
    if item.get("stock_remaining") is not None:
        stock_str = f"  |  Stock: **{item['stock_remaining']}** left"

    return (
        f"RAP: **{int(item['rap'])}** | Value: **{int(item['value'])}** | "
        f"Gap: **{item['gap']:.1f}%**{sale}{stock_str} {tags}\n"
        f"{d_icon} {d_lbl}  {t_icon} {t_lbl}\n"
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
    embed.add_field(name="Results", value=f"Checked **{scanned:,}** items · **{qualified}** qualified", inline=False)
    if not items:
        embed.add_field(name="No Results", value="Nothing matched. Try raising `max_price` or lowering `min_gap`.", inline=False)
        return embed
    for i, item in enumerate(items, 1):
        embed.add_field(name=f"{i}. {item['name']}", value=_item_line(item), inline=False)
    embed.set_footer(text="Gap = (Value − RAP) / Value × 100  |  Positive = potential upside")
    return embed


def build_new_releases_embed(items: List[Dict]) -> discord.Embed:
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    embed = discord.Embed(
        title="🆕 Newest Limiteds",
        description=(
            f"Sorted by asset ID (higher = more recently created on Roblox) · {now}\n"
            "Use `/forsale` to see which of these you can buy right now."
        ),
        color=discord.Color.gold(),
    )
    if not items:
        embed.add_field(name="No Data", value="Could not fetch from Rolimons.", inline=False)
        return embed
    for i, item in enumerate(items, 1):
        embed.add_field(name=f"{i}. {item['name']}", value=_item_line(item), inline=False)
    return embed


def build_forsale_embed(items: List[Dict]) -> discord.Embed:
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    embed = discord.Embed(
        title="🛒 Limiteds On Sale from Roblox Right Now",
        description=f"Ranked by buy score · {now}",
        color=discord.Color.blue(),
    )
    if not items:
        embed.add_field(
            name="None Found",
            value=(
                "Roblox catalog returned no for-sale limiteds.\n"
                "Railway's IP may be temporarily rate-limited. Try again in a few minutes."
            ),
            inline=False,
        )
        return embed

    best = items[0]
    stock_note = f"  |  **{best.get('stock_remaining', '?')} left in stock**" if best.get("stock_remaining") is not None else ""
    embed.add_field(
        name=f"⭐ BEST BUY → {best['name']}",
        value=(
            f"{buy_reason(best)}\n"
            f"**Sale: {best['sale_price']} R$** | RAP: {int(best['rap'])} | "
            f"Value: {int(best['value'])} | Score: {best['score']:.1f}{stock_note}\n"
            + (f"*{best['description']}*\n" if best.get("description") else "")
            + f"[🛒 Buy on Roblox](https://www.roblox.com/catalog/{best['id']})"
        ),
        inline=False,
    )
    for i, item in enumerate(items[1:9], 2):
        embed.add_field(name=f"{i}. {item['name']}", value=_item_line(item), inline=False)
    embed.set_footer(text="Score = gap + demand + trend + bonuses  |  Higher = better")
    return embed


def build_buynow_embed(item: Dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"💰 BUY NOW: {item['name']}",
        url=f"https://www.roblox.com/catalog/{item['id']}",
        description=buy_reason(item),
        color=discord.Color.brand_red(),
    )
    if item.get("description"):
        embed.add_field(name="Description", value=item["description"], inline=False)

    embed.add_field(name="Sale Price",    value=f"**{item.get('sale_price', '?')} R$**",  inline=True)
    embed.add_field(name="RAP",           value=f"{int(item['rap'])} R$",                  inline=True)
    embed.add_field(name="Value Est.",    value=f"{int(item['value'])} R$",                inline=True)
    embed.add_field(name="Gap",           value=f"{item['gap']:.1f}%",                     inline=True)
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

    if item.get("stock_remaining") is not None:
        embed.add_field(name="Stock Left", value=str(item["stock_remaining"]), inline=True)
    if item.get("creator_name"):
        embed.add_field(name="Creator", value=f"{item['creator_name']} ({item.get('creator_type','?')})", inline=True)

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
    embed.set_footer(text="Not financial advice. Limiteds carry real risk — always research before buying.")
    return embed


def build_details_embed(item: Dict, rblx: Dict, creator: str) -> discord.Embed:
    """Full item page embed combining Rolimons + Roblox catalog data."""
    embed = discord.Embed(
        title=f"🔍 {item['name']}",
        url=f"https://www.roblox.com/catalog/{item['id']}",
        color=discord.Color.blurple(),
    )

    desc = rblx.get("description", "").strip()
    if desc:
        embed.description = desc[:300] + ("..." if len(desc) > 300 else "")

    # Price & value
    embed.add_field(name="RAP",          value=f"{int(item['rap'])} R$",    inline=True)
    embed.add_field(name="Value Est.",   value=f"{int(item['value'])} R$",  inline=True)
    embed.add_field(name="Gap",          value=f"{item['gap']:.1f}%",       inline=True)

    # Sales info from Roblox
    price = rblx.get("price") or rblx.get("lowestPrice")
    if price:
        embed.add_field(name="Current Price", value=f"{price} R$",          inline=True)
    if rblx.get("unitsAvailableForConsumption") is not None:
        embed.add_field(name="Stock Left",  value=str(rblx["unitsAvailableForConsumption"]), inline=True)
    if rblx.get("saleCount") is not None:
        embed.add_field(name="Total Sold",  value=f"{rblx['saleCount']:,}", inline=True)

    # Creator
    embed.add_field(
        name="Creator",
        value=f"{creator} ({rblx.get('creatorType','?')})",
        inline=True,
    )

    # Rolimons signals
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
    if item.get("projected"): tags.append("📊 Projected")
    if tags:
        embed.add_field(name="Signals", value="  ".join(tags), inline=False)

    embed.add_field(name="Verdict", value=buy_reason(item), inline=False)
    embed.add_field(
        name="Links",
        value=(
            f"[🛒 Roblox Catalog](https://www.roblox.com/catalog/{item['id']})  "
            f"[📊 Rolimons](https://www.rolimons.com/item/{item['id']})"
        ),
        inline=False,
    )
    return embed


def build_sales_embed(item: Dict, sales_data: Dict, rolimons_trades: List) -> discord.Embed:
    """Recent sales / price history embed."""
    embed = discord.Embed(
        title=f"💹 Recent Sales — {item['name']}",
        url=f"https://www.rolimons.com/item/{item['id']}",
        color=discord.Color.teal(),
    )
    embed.add_field(name="RAP",   value=f"{int(item['rap'])} R$",   inline=True)
    embed.add_field(name="Value", value=f"{int(item['value'])} R$", inline=True)
    embed.add_field(name="Gap",   value=f"{item['gap']:.1f}%",      inline=True)

    source = sales_data.get("source", "unknown")

    # — Roblox price datapoints (if endpoint wasn't blocked) —
    datapoints = sales_data.get("price_datapoints", [])
    if datapoints:
        lines = []
        for pt in datapoints[-10:]:  # last 10 datapoints
            ts  = pt.get("date", "")[:10]
            avg = pt.get("value", 0)
            lines.append(f"`{ts}` — avg **{avg:,} R$**")
        embed.add_field(
            name="📅 Price History (Roblox)",
            value="\n".join(lines) or "No data",
            inline=False,
        )

    # — Roblox individual resale records (if endpoint wasn't blocked) —
    records = sales_data.get("resale_records", [])
    if records:
        lines = []
        for rec in records[:8]:
            price  = rec.get("price", 0)
            seller = rec.get("seller", {}).get("name", "?")
            lines.append(f"**{price:,} R$** sold by {seller}")
        embed.add_field(
            name="🧾 Recent Individual Sales (Roblox)",
            value="\n".join(lines) or "No data",
            inline=False,
        )

    # — Rolimons recent trades (always attempted as fallback) —
    if rolimons_trades:
        lines = []
        for trade in rolimons_trades[:8]:
            try:
                ts    = datetime.fromtimestamp(trade[0], tz=timezone.utc).strftime("%Y-%m-%d")
                price = trade[1]
                lines.append(f"`{ts}` — **{price:,} R$**")
            except Exception:
                continue
        if lines:
            embed.add_field(
                name="📊 Recent Trades (Rolimons)",
                value="\n".join(lines),
                inline=False,
            )

    # If all sources failed
    if not datapoints and not records and not rolimons_trades:
        embed.add_field(
            name="No Sales Data",
            value=(
                "Roblox economy API is blocked on Railway (cloud IP restriction).\n"
                "Rolimons trade data was also unavailable for this item.\n"
                f"View full history manually: [Rolimons item page](https://www.rolimons.com/item/{item['id']})"
            ),
            inline=False,
        )

    embed.set_footer(text=f"Data source: {source}")
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


# ================== HOURLY LOOP ==================

async def hourly_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M UTC')}] Running auto scans...")
        await post_undervalue("⏰ Auto Hourly — Undervalue Scan")
        channel = await _get_channel()
        if channel:
            async with aiohttp.ClientSession() as session:
                forsale_items = await fetch_forsale_limiteds(session)
                new_items     = await fetch_new_releases(session)
            await channel.send(embed=build_forsale_embed(forsale_items))
            await channel.send(embed=build_new_releases_embed(new_items))
        await asyncio.sleep(SCAN_INTERVAL)


# ================== SLASH COMMANDS ==================

@tree.command(name="scan", description="Scan for undervalued limiteds (below community value)", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(max_price="Max RAP in Robux", min_gap="Min gap % between RAP and value", mode="Sort by: gap or score")
async def scan_cmd(interaction: discord.Interaction, max_price: int = AUTO_MAX_PRICE, min_gap: int = AUTO_MIN_GAP, mode: str = AUTO_MODE):
    await interaction.response.send_message("🔎 Scanning...", ephemeral=True)
    await post_undervalue(f"Manual scan by {interaction.user}", max_price, AUTO_TOP_N, AUTO_MIN_RAP, min_gap, mode)


@tree.command(name="new", description="Show the newest Roblox limiteds", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(count="How many to show (default 15, max 25)")
async def new_cmd(interaction: discord.Interaction, count: int = 15):
    await interaction.response.send_message("🆕 Fetching newest limiteds...", ephemeral=True)
    async with aiohttp.ClientSession() as session:
        items = await fetch_new_releases(session, min(count, 25))
    channel = await _get_channel()
    if channel:
        await channel.send(embed=build_new_releases_embed(items))


@tree.command(name="forsale", description="Show limiteds on sale from Roblox right now", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(max_price="Filter by max sale price (0 = no limit)", category="Filter by type: hats, faces, gear, heads, accessories")
async def forsale_cmd(interaction: discord.Interaction, max_price: int = 0, category: str = ""):
    await interaction.response.send_message("🛒 Checking catalog...", ephemeral=True)
    subcategory = CATALOG_SUBCATEGORY_MAP.get(category.lower(), 0)
    async with aiohttp.ClientSession() as session:
        items = await fetch_forsale_limiteds(session, max_price, subcategory)
    channel = await _get_channel()
    if channel:
        await channel.send(embed=build_forsale_embed(items))


@tree.command(name="buynow", description="Get ONE best-buy pick from limiteds currently on sale", guild=discord.Object(id=GUILD_ID))
async def buynow_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("💰 Finding the best buy right now...", ephemeral=True)
    async with aiohttp.ClientSession() as session:
        items = await fetch_forsale_limiteds(session)
    channel = await _get_channel()
    if not channel:
        return
    if not items:
        await channel.send("❌ No purchaseable limiteds found. Try again in a few minutes.")
        return
    scored = [i for i in items if i["value"] > 0 and i["rap"] > 0]
    pick   = scored[0] if scored else items[0]
    await channel.send(embed=build_buynow_embed(pick))


@tree.command(name="details", description="Full item page — description, creator, stock, value signals", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(name="Part of the item name to search for")
async def details_cmd(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        all_items = await fetch_rolimons_list(session)
        matches   = [i for i in all_items if name.lower() in i["name"].lower()]

        if not matches:
            await interaction.followup.send(f"No item found matching `{name}`.", ephemeral=True)
            return

        item          = matches[0]
        item["gap"]   = compute_gap(item["rap"], item["value"])
        item["score"] = score_item(item)

        # Fetch full Roblox catalog page data
        rblx    = await fetch_single_item_details(session, item["id"]) or {}
        creator = ""
        if rblx.get("creatorTargetId"):
            creator = await fetch_creator_name(session, rblx["creatorTargetId"], rblx.get("creatorType", "User"))

    channel = await _get_channel()
    if channel:
        await channel.send(embed=build_details_embed(item, rblx, creator))
    await interaction.followup.send("✅ Posted to channel.", ephemeral=True)


@tree.command(name="sales", description="Recent sale prices and trade history for an item", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(name="Part of the item name to search for")
async def sales_cmd(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        all_items = await fetch_rolimons_list(session)
        matches   = [i for i in all_items if name.lower() in i["name"].lower()]

        if not matches:
            await interaction.followup.send(f"No item found matching `{name}`.", ephemeral=True)
            return

        item          = matches[0]
        item["gap"]   = compute_gap(item["rap"], item["value"])
        item["score"] = score_item(item)

        # Fetch sales data from both sources concurrently
        sales_data, rolimons_trades = await asyncio.gather(
            fetch_recent_sales(session, item["id"]),
            fetch_rolimons_sales_page(session, item["id"]),
        )

    channel = await _get_channel()
    if channel:
        await channel.send(embed=build_sales_embed(item, sales_data, rolimons_trades))
    await interaction.followup.send("✅ Posted to channel.", ephemeral=True)


@tree.command(name="item", description="Quick lookup for a specific limited", guild=discord.Object(id=GUILD_ID))
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
    embed = discord.Embed(title=f"🔍 {item['name']}", url=f"https://www.rolimons.com/item/{item['id']}", color=discord.Color.blurple())
    embed.add_field(name="RAP",    value=f"{int(item['rap'])} R$",    inline=True)
    embed.add_field(name="Value",  value=f"{int(item['value'])} R$",  inline=True)
    embed.add_field(name="Gap",    value=f"{item['gap']:.1f}%",       inline=True)
    embed.add_field(name="Demand", value=f"{DEMAND_ICONS.get(item['demand'],'')} {DEMAND_LABELS.get(item['demand'],'?')}", inline=True)
    embed.add_field(name="Trend",  value=f"{TREND_ICONS.get(item['trend'],'')} {TREND_LABELS.get(item['trend'],'?')}",    inline=True)
    embed.add_field(name="Score",  value=f"{item['score']:.1f}", inline=True)
    tags = []
    if item.get("hyped"): tags.append("🔥 Hyped")
    if item.get("rare"):  tags.append("💎 Rare")
    if item.get("projected"): tags.append("📊 Projected")
    if tags:
        embed.add_field(name="Signals", value="  ".join(tags), inline=False)
    embed.add_field(name="Verdict", value=buy_reason(item), inline=False)
    embed.add_field(name="Links", value=f"[📊 Rolimons](https://www.rolimons.com/item/{item['id']})  • [🛒 Roblox](https://www.roblox.com/catalog/{item['id']})", inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ================== STARTUP ==================


@client.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    
    # Clear all old commands first
    tree.clear_commands(guild=guild)
    await tree.sync(guild=guild)
    
    # Now register and sync the new ones
    await tree.sync(guild=guild)
    
    print(f"✅ {client.user} is online — commands synced to guild {GUILD_ID}")
    client.loop.create_task(hourly_loop())


client.run(TOKEN)
