import discord
import os
import re
import aiohttp
import json
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
        if any(k in label for k in ["style code", "style", "sku", "mpn", "model"]):
            if re.search(r"[A-Za-z]{1,3}\d{4,}|[A-Za-z]{2}\d{4}-\d{3}|\d{6}-\d{3}", value):
                sku = value.strip()
        if any(k in label for k in ["us shoe size", "shoe size", "size"]):
            m = re.search(r"\d+\.?\d*", value)
            if m:
                size = m.group()

    print(f"[eBay] title={title} sku={sku} size={size}")
    return {"title": title, "sku": sku, "size": size}


async def fetch_stockx_price(sku: str, size: str) -> tuple:
    """
    Search StockX directly using their internal search API.
    Returns (price, product_title) or (None, None).
    """
    print(f"[StockX] Searching: {sku} size {size}")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "app-platform": "Iron",
        "app-version": "2022.09.04.01",
        "x-powered-by": "SR Project Israel Team",
    }

    search_url = f"https://xw7sbct9v6-2.algolianet.com/1/indexes/products/query"
    # Use StockX's Algolia search
    algolia_url = "https://stockx.com/api/browse"
    params = {
        "_search": sku,
        "dataType": "product",
        "market": "US",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(algolia_url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            status = resp.status
            text = await resp.text()
            print(f"[StockX] Browse status: {status}, body: {text[:300]}")
            if status != 200:
                return None, None
            data = json.loads(text)

    edges = data.get("Products", []) or data.get("data", {}).get("browse", {}).get("results", {}).get("edges", [])
    if not edges:
        print("[StockX] No results from browse")
        return None, None

    # Find best matching product
    product = None
    for edge in edges[:5]:
        node = edge.get("node", edge)
        node_sku = node.get("styleId", "") or node.get("sku", "")
        if sku.lower() in node_sku.lower() or node_sku.lower() in sku.lower():
            product = node
            break
    if not product:
        product = edges[0].get("node", edges[0])

    title = product.get("title") or product.get("name")
    market = product.get("market", {})
    lowest_ask = market.get("lowestAsk") or product.get("lowestAsk")
    last_sale  = market.get("lastSale")  or product.get("lastSale")
    price = lowest_ask or last_sale

    print(f"[StockX] Product: {title} | lowestAsk: {lowest_ask} | lastSale: {last_sale}")
    return (float(price) if price else None), title


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
            "Or with manual SKU: `!deal <url> <price> <StyleCode> <size>`"
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

    processing_msg = await message.reply(f"🔍 Checking deal…")

    try:
        # If SKU provided manually, skip eBay scrape entirely (much faster)
        if manual_sku:
            sku   = manual_sku
            size  = manual_size
            title = f"eBay item {item_id}"
        else:
            details = await fetch_ebay_details(item_id)
            sku   = details["sku"]
            size  = details["size"]
            title = details["title"] or f"eBay item {item_id}"

        if not sku:
            await processing_msg.edit(content=(
                f"⚠️ Couldn't auto-detect the Style Code from the listing.\n"
                f"Find it on the eBay page under **Item Specifics → Style Code** and pass it manually:\n"
                f"`!deal {ebay_url} {ebay_price} <StyleCode> <size>`\n"
                f"Example: `!deal {ebay_url} {ebay_price} DJ5982-060 10`"
            ))
            return

        await processing_msg.edit(content=f"🔍 Found style code **{sku}** — checking StockX…")

        stockx_price, stockx_title = await fetch_stockx_price(sku, size)
        if stockx_title and title.startswith("eBay item"):
            title = stockx_title

        if not stockx_price:
            await processing_msg.edit(content=(
                f"⚠️ Couldn't get a StockX price for **{sku}**.\n"
                f"Check if it's listed on StockX manually: https://stockx.com/search?s={sku}"
            ))
            return

        discount = (stockx_price - ebay_price) / stockx_price

        if discount >= DISCOUNT_THRESHOLD:
            alert_channel = bot.get_channel(ALERT_CHANNEL_ID)
            if alert_channel:
                embed = make_alert_embed(title, sku, size, ebay_price, stockx_price, discount, ebay_url, str(message.author))
                await alert_channel.send(embed=embed)
            await processing_msg.edit(content=(
                f"✅ **{discount*100:.1f}% below StockX** — deal alert sent to your private channel!\n"
                f"Buy @ **${ebay_price:.2f}** · StockX: **${stockx_price:.2f}**"
            ))
        else:
            needed = stockx_price * (1 - DISCOUNT_THRESHOLD)
            await processing_msg.edit(content=(
                f"❌ Not a deal — **{discount*100:.1f}% below StockX** (need {DISCOUNT_THRESHOLD*100:.0f}%).\n"
                f"You'd need to pay **${needed:.2f}** or less.\n"
                f"eBay: **${ebay_price:.2f}** · StockX: **${stockx_price:.2f}**"
            ))

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        await processing_msg.edit(content=f"⚠️ Error: `{type(e).__name__}: {e}`")

bot.run(DISCORD_TOKEN)
