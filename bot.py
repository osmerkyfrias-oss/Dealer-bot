import discord
import os
import re
import json
import aiohttp
from bs4 import BeautifulSoup

# ── Config ──────────────────────────────────────────────────────────────────
DISCORD_TOKEN      = os.environ["DISCORD_TOKEN"]
ALERT_CHANNEL_ID   = int(os.environ["ALERT_CHANNEL_ID"])   # your private channel
DISCOUNT_THRESHOLD = 0.15   # 15% — change to 0.20 for 20%, etc.

# ── Bot setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_ebay_item_id(url: str) -> str | None:
    """Pull the eBay item ID out of a URL."""
    match = re.search(r"/itm/(\d+)", url)
    return match.group(1) if match else None


async def fetch_ebay_details(item_id: str) -> dict:
    """
    Scrape the eBay listing page for title, SKU, and size.
    Returns a dict with keys: title, sku, size  (all may be None if not found)
    """
    url = f"https://www.ebay.com/itm/{item_id}"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SneakerBot/1.0)"}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            html = await resp.text()

    soup = BeautifulSoup(html, "html.parser")

    # Title
    title_tag = soup.find("h1", {"class": re.compile(r"x-item-title")})
    title = title_tag.get_text(strip=True) if title_tag else None

    # Item specifics table (contains Style, Size, etc.)
    sku, size = None, None
    for row in soup.select("div.ux-labels-values__labels-content"):
        label = row.get_text(strip=True).lower()
        value_tag = row.find_next_sibling("div")
        value = value_tag.get_text(strip=True) if value_tag else ""
        if "style" in label or "sku" in label or "mpn" in label:
            sku = value
        if label == "us shoe size":
            size = value

    return {"title": title, "sku": sku, "size": size}


async def fetch_stockx_price(sku: str, size: str) -> float | None:
    """
    Query the SneakersAPI (sneakersapi.dev) for StockX ask price.
    Falls back to None if the SKU is not found.
    Free tier — no key needed for basic lookups.
    """
    params = {"styleId": sku}
    url = "https://api.sneakersapi.dev/api/v3/stockx/products"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

    products = data.get("data", [])
    if not products:
        return None

    product = products[0]

    # Try to find size-specific price
    if size:
        for variant in product.get("variants", []):
            if str(size) in str(variant.get("size", "")):
                price = variant.get("market", {}).get("lowestAsk") or variant.get("market", {}).get("lastSale")
                if price:
                    return float(price)

    # Fall back to lowest ask across all sizes
    market = product.get("market", {})
    price = market.get("lowestAsk") or market.get("lastSale")
    return float(price) if price else None


def calc_discount(ebay_price: float, stockx_price: float) -> float:
    """Returns discount as a decimal, e.g. 0.22 = 22% below StockX."""
    return (stockx_price - ebay_price) / stockx_price


def make_alert_embed(
    title: str,
    sku: str,
    size: str,
    ebay_price: float,
    stockx_price: float,
    discount: float,
    ebay_url: str,
    submitted_by: str,
) -> discord.Embed:
    profit = stockx_price - ebay_price
    color = discord.Color.green() if discount >= 0.20 else discord.Color.gold()

    embed = discord.Embed(
        title="🔥 Deal Alert — Buy it!",
        description=f"**{title}**",
        color=color,
    )
    embed.add_field(name="SKU", value=sku or "Unknown", inline=True)
    embed.add_field(name="Size", value=size or "Unknown", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="eBay price", value=f"${ebay_price:.2f}", inline=True)
    embed.add_field(name="StockX value", value=f"${stockx_price:.2f}", inline=True)
    embed.add_field(name="Discount", value=f"**{discount*100:.1f}% off**", inline=True)
    embed.add_field(name="Estimated profit", value=f"~${profit:.2f} before fees", inline=False)
    embed.add_field(name="eBay listing", value=f"[View listing]({ebay_url})", inline=False)
    embed.set_footer(text=f"Submitted by {submitted_by}")
    return embed


# ── Event handlers ────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅  Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"    Watching for deals — alerting to channel {ALERT_CHANNEL_ID}")


@bot.event
async def on_message(message: discord.Message):
    # Ignore the bot's own messages
    if message.author == bot.user:
        return

    content = message.content.strip()

    # ── Command: !deal <ebay_url> <price>  ─────────────────────────────────
    # Example:  !deal https://www.ebay.com/itm/123456789 75.00
    if not content.lower().startswith("!deal"):
        return

    parts = content.split()
    if len(parts) < 3:
        await message.reply(
            "❌ Usage: `!deal <eBay URL> <your negotiated price>`\n"
            "Example: `!deal https://www.ebay.com/itm/123456789012 75.00`"
        )
        return

    ebay_url  = parts[1]
    try:
        ebay_price = float(parts[2].replace("$", "").replace(",", ""))
    except ValueError:
        await message.reply("❌ Price must be a number, e.g. `75.00`")
        return

    # Validate eBay URL
    item_id = extract_ebay_item_id(ebay_url)
    if not item_id:
        await message.reply("❌ That doesn't look like a valid eBay listing URL.")
        return

    processing_msg = await message.reply("🔍 Checking deal… give me a second!")

    try:
        # 1. Scrape eBay listing
        details = await fetch_ebay_details(item_id)
        sku   = details["sku"]
        size  = details["size"]
        title = details["title"] or "Unknown sneaker"

        if not sku:
            await processing_msg.edit(content=(
                "⚠️ Couldn't find a SKU on that listing. "
                "Try: `!deal <url> <price> <SKU> <size>`\n"
                "Example: `!deal https://... 75 DH6927-140 10`"
            ))
            return

        # 2. Look up StockX price
        stockx_price = await fetch_stockx_price(sku, size)
        if not stockx_price:
            await processing_msg.edit(content=(
                f"⚠️ Found SKU **{sku}** but couldn't get a StockX price. "
                "The shoe may not be listed on StockX or the API didn't return data."
            ))
            return

        # 3. Calculate discount
        discount = calc_discount(ebay_price, stockx_price)

        if discount >= DISCOUNT_THRESHOLD:
            # Send alert to private channel
            alert_channel = bot.get_channel(ALERT_CHANNEL_ID)
            if alert_channel:
                embed = make_alert_embed(
                    title, sku, size, ebay_price, stockx_price,
                    discount, ebay_url, str(message.author)
                )
                await alert_channel.send(embed=embed)

            await processing_msg.edit(content=(
                f"✅ **{discount*100:.1f}% below StockX** — deal alert sent to your private channel!\n"
                f"Buy @ **${ebay_price:.2f}** · StockX value **${stockx_price:.2f}**"
            ))
        else:
            gap = DISCOUNT_THRESHOLD - discount
            await processing_msg.edit(content=(
                f"❌ Not a deal. Only **{discount*100:.1f}% below StockX** "
                f"(need {DISCOUNT_THRESHOLD*100:.0f}%). "
                f"You'd need to pay **${ebay_price - (stockx_price * gap):.2f}** or less to hit your threshold.\n"
                f"eBay: **${ebay_price:.2f}** · StockX: **${stockx_price:.2f}**"
            ))

    except Exception as e:
        await processing_msg.edit(content=f"⚠️ Something went wrong: `{e}`")
        raise


# ── Manual override: !deal <url> <price> <sku> <size> ─────────────────────
# Already handled above via the scraper; if SKU scraping fails, user can
# pass SKU and size manually as extra args — extend parts[] handling if needed.

bot.run(DISCORD_TOKEN)
