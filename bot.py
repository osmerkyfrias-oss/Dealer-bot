import discord
import os
import re
import aiohttp
from bs4 import BeautifulSoup

# ── Config ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN      = os.environ["DISCORD_TOKEN"]
ALERT_CHANNEL_ID   = int(os.environ["ALERT_CHANNEL_ID"])
DISCOUNT_THRESHOLD = 0.15

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_ebay_item_id(url: str) -> str | None:
    match = re.search(r"/itm/(\d+)", url)
    return match.group(1) if match else None


async def fetch_ebay_details(item_id: str) -> dict:
    url = f"https://www.ebay.com/itm/{item_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            html = await resp.text()

    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.find("h1", {"class": re.compile(r"x-item-title")})
    title = title_tag.get_text(strip=True) if title_tag else None

    sku, size = None, None
    labels = soup.select("div.ux-labels-values__labels-content")
    values = soup.select("div.ux-labels-values__values-content")

    for label_el, value_el in zip(labels, values):
        label = label_el.get_text(strip=True).lower()
        value = value_el.get_text(strip=True)
        print(f"[eBay] '{label}' = '{value}'")

        if any(k in label for k in ["style code", "style", "sku", "mpn", "model number", "model"]):
            if re.search(r"[A-Za-z]{1,3}\d{4,}|[A-Za-z]{2}\d{4}-\d{3}|\d{6}-\d{3}", value):
                sku = value.strip()

        if any(k in label for k in ["us shoe size", "shoe size", "size"]):
            m = re.search(r"\d+\.?\d*", value)
            if m:
                size = m.group()

    print(f"[eBay] title={title} sku={sku} size={size}")
    return {"title": title, "sku": sku, "size": size}


async def fetch_stockx_price(sku: str, size: str) -> tuple[float | None, str | None]:
    """
    Try KicksDB (kicks.dev) free tier first — search by SKU.
    Returns (price, product_title).
    """
    print(f"[StockX] Searching for SKU: {sku}")

    # Step 1: search for the product by SKU
    search_url = "https://api.kicks.dev/v3/stockx/search"
    params = {"query": sku, "limit": 1}

    async with aiohttp.ClientSession() as session:
        async with session.get(search_url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            print(f"[StockX] Search status: {resp.status}")
            if resp.status != 200:
                return None, None
            data = await resp.json()

    print(f"[StockX] Search response: {str(data)[:400]}")
    products = data.get("data", [])
    if not products:
        return None, None

    product = products[0]
    product_title = product.get("title")
    slug = product.get("slug") or product.get("id")
    print(f"[StockX] Found: {product_title} | slug: {slug}")

    if not slug:
        # Try to use market data directly from search result
        market = product.get("market", {})
        price = market.get("lowestAsk") or market.get("lastSale")
        return (float(price) if price else None), product_title

    # Step 2: get product detail with size-specific pricing
    detail_url = f"https://api.kicks.dev/v3/stockx/products/{slug}"
    async with aiohttp.ClientSession() as session:
        async with session.get(detail_url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            print(f"[StockX] Detail status: {resp.status}")
            if resp.status != 200:
                # Fall back to search-level market data
                market = product.get("market", {})
                price = market.get("lowestAsk") or market.get("lastSale")
                return (float(price) if price else None), product_title
            detail = await resp.json()

    detail_data = detail_data = detail.get("data", detail)
    print(f"[StockX] Detail market: {str(detail_data.get('market', {}))[:200]}")

    # Try size-specific variant
    if size:
        for variant in detail_data.get("variants", []):
            if str(size) in str(variant.get("size", "")):
                price = variant.get("market", {}).get("lowestAsk") or variant.get("market", {}).get("lastSale")
                if price:
                    print(f"[StockX] Size {size} price: {price}")
                    return float(price), product_title

    # Fall back to overall market
    market = detail_data.get("market", {})
    price = market.get("lowestAsk") or market.get("lastSale")
    print(f"[StockX] Fallback price: {price}")
    return (float(price) if price else None), product_title


def make_alert_embed(title, sku, size, ebay_price, stockx_price, discount, ebay_url, submitted_by):
    profit = stockx_price - ebay_price
    color = discord.Color.green() if discount >= 0.20 else discord.Color.gold()
    embed = discord.Embed(title="🔥 Deal Alert — Buy it!", description=f"**{title}**", color=color)
    embed.add_field(name="Style Code", value=sku or "Unknown", inline=True)
    embed.add_field(name="Size", value=size or "Unknown", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="eBay price", value=f"${ebay_price:.2f}", inline=True)
    embed.add_field(name="StockX value", value=f"${stockx_price:.2f}", inline=True)
    embed.add_field(name="Discount", value=f"**{discount*100:.1f}% off**", inline=True)
    embed.add_field(name="Est. profit (before fees)", value=f"~${profit:.2f}", inline=False)
    embed.add_field(name="eBay listing", value=f"[View listing]({ebay_url})", inline=False)
    embed.set_footer(text=f"Submitted by {submitted_by}")
    return embed


@bot.event
async def on_ready():
    print(f"✅  Logged in as {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    content = message.content.strip()
    if not content.lower().startswith("!deal"):
        return

    parts = content.split()
    manual_sku  = parts[3] if len(parts) >= 4 else None
    manual_size = parts[4] if len(parts) >= 5 else None

    if len(parts) < 3:
        await message.reply(
            "❌ Usage: `!deal <eBay URL> <price>`\n"
            "Manual: `!deal <url> <price> <StyleCode> <size>`"
        )
        return

    ebay_url = parts[1]
    try:
        ebay_price = float(parts[2].replace("$", "").replace(",", ""))
    except ValueError:
        await message.reply("❌ Price must be a number, e.g. `75.00`")
        return

    item_id = extract_ebay_item_id(ebay_url)
    if not item_id:
        await message.reply("❌ Not a valid eBay listing URL.")
        return

    processing_msg = await message.reply(f"🔍 Checking item `{item_id}`…")

    try:
        details = await fetch_ebay_details(item_id)
        sku   = manual_sku  or details["sku"]
        size  = manual_size or details["size"]
        title = details["title"] or f"eBay item {item_id}"

        if not sku:
            await processing_msg.edit(content=(
                f"⚠️ Found **{title}** but couldn't detect the Style Code.\n"
                f"Pass it manually:\n`!deal {ebay_url} {ebay_price} <StyleCode> <size>`\n"
                f"Example: `!deal {ebay_url} {ebay_price} DJ5982-060 10`"
            ))
            return

        stockx_price, stockx_title = await fetch_stockx_price(sku, size)

        if not stockx_price:
            await processing_msg.edit(content=(
                f"⚠️ Found style code **{sku}** but couldn't get a StockX price.\n"
                f"Try passing the SKU manually to double check:\n"
                f"`!deal {ebay_url} {ebay_price} {sku} {size or '10'}`"
            ))
            return

        discount = (stockx_price - ebay_price) / stockx_price

        if discount >= DISCOUNT_THRESHOLD:
            alert_channel = bot.get_channel(ALERT_CHANNEL_ID)
            if alert_channel:
                embed = make_alert_embed(title, sku, size, ebay_price, stockx_price, discount, ebay_url, str(message.author))
                await alert_channel.send(embed=embed)
            await processing_msg.edit(content=(
                f"✅ **{discount*100:.1f}% below StockX** — deal alert sent!\n"
                f"Buy @ **${ebay_price:.2f}** · StockX: **${stockx_price:.2f}**"
            ))
        else:
            needed = stockx_price * (1 - DISCOUNT_THRESHOLD)
            await processing_msg.edit(content=(
                f"❌ Not a deal — only **{discount*100:.1f}% below StockX** (need {DISCOUNT_THRESHOLD*100:.0f}%).\n"
                f"You'd need to pay **${needed:.2f}** or less.\n"
                f"eBay: **${ebay_price:.2f}** · StockX: **${stockx_price:.2f}**"
            ))

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        await processing_msg.edit(content=f"⚠️ Error: `{type(e).__name__}: {e}`")

bot.run(DISCORD_TOKEN)
