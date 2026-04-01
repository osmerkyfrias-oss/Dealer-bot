import discord
import os
import re
import aiohttp
from bs4 import BeautifulSoup

# ── Config ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN      = os.environ["DISCORD_TOKEN"]
ALERT_CHANNEL_ID   = int(os.environ["ALERT_CHANNEL_ID"])
DISCOUNT_THRESHOLD = 0.15

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_ebay_item_id(url: str) -> str | None:
    # Handles URLs with tracking params like ?_trkparms=...
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
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            status = resp.status
            html = await resp.text()

    print(f"[eBay] Status {status} for item {item_id}, page length {len(html)}")

    soup = BeautifulSoup(html, "html.parser")

    # Title
    title_tag = soup.find("h1", {"class": re.compile(r"x-item-title")})
    title = title_tag.get_text(strip=True) if title_tag else None
    print(f"[eBay] Title: {title}")

    # Item specifics
    sku, size = None, None
    labels = soup.select("div.ux-labels-values__labels-content")
    values = soup.select("div.ux-labels-values__values-content")
    print(f"[eBay] Found {len(labels)} label/value pairs")

    for label_el, value_el in zip(labels, values):
        label = label_el.get_text(strip=True).lower()
        value = value_el.get_text(strip=True)
        print(f"[eBay] Spec: '{label}' = '{value}'")

        if any(k in label for k in ["style code", "style", "sku", "mpn", "model number", "model"]):
            if re.search(r"[A-Za-z]{1,3}\d{4,}|[A-Za-z]{2}\d{4}-\d{3}|\d{6}-\d{3}", value):
                sku = value.strip()
                print(f"[eBay] Detected SKU: {sku}")

        if any(k in label for k in ["us shoe size", "shoe size", "size"]):
            size_match = re.search(r"\d+\.?\d*", value)
            if size_match:
                size = size_match.group()
                print(f"[eBay] Detected size: {size}")

    return {"title": title, "sku": sku, "size": size}


async def fetch_stockx_price(sku: str, size: str) -> float | None:
    params = {"styleId": sku}
    url = "https://api.sneakersapi.dev/api/v3/stockx/products"
    print(f"[StockX] Looking up SKU: {sku}, size: {size}")

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            status = resp.status
            data = await resp.json()
            print(f"[StockX] Status {status}, response: {str(data)[:300]}")
            if status != 200:
                return None

    products = data.get("data", [])
    if not products:
        print("[StockX] No products found")
        return None

    product = products[0]
    print(f"[StockX] Product: {product.get('title')} | market: {product.get('market')}")

    if size:
        for variant in product.get("variants", []):
            if str(size) in str(variant.get("size", "")):
                price = variant.get("market", {}).get("lowestAsk") or variant.get("market", {}).get("lastSale")
                if price:
                    print(f"[StockX] Size-specific price: {price}")
                    return float(price)

    market = product.get("market", {})
    price = market.get("lowestAsk") or market.get("lastSale")
    print(f"[StockX] Fallback price: {price}")
    return float(price) if price else None


def calc_discount(ebay_price: float, stockx_price: float) -> float:
    return (stockx_price - ebay_price) / stockx_price


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
    embed.add_field(name="Estimated profit", value=f"~${profit:.2f} before fees", inline=False)
    embed.add_field(name="eBay listing", value=f"[View listing]({ebay_url})", inline=False)
    embed.set_footer(text=f"Submitted by {submitted_by}")
    return embed


# ── Events ────────────────────────────────────────────────────────────────────

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
            "❌ Usage: `!deal <eBay URL> <negotiated price>`\n"
            "Example: `!deal https://www.ebay.com/itm/123456789012 75.00`\n"
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
        await message.reply("❌ That doesn't look like a valid eBay listing URL.")
        return

    processing_msg = await message.reply(f"🔍 Checking deal for item `{item_id}`… give me a second!")

    try:
        details = await fetch_ebay_details(item_id)
        sku   = manual_sku  or details["sku"]
        size  = manual_size or details["size"]
        title = details["title"] or f"eBay item {item_id}"

        print(f"[Bot] Final — title: {title} | sku: {sku} | size: {size} | price: {ebay_price}")

        if not sku:
            await processing_msg.edit(content=(
                f"⚠️ Found listing **{title}** but couldn't detect the Style Code automatically.\n"
                f"Look it up on the eBay page under 'Item Specifics' and pass it manually:\n"
                f"`!deal {ebay_url} {ebay_price} <StyleCode> <size>`\n"
                f"Example: `!deal {ebay_url} {ebay_price} GW9526 10`"
            ))
            return

        stockx_price = await fetch_stockx_price(sku, size)
        if not stockx_price:
            await processing_msg.edit(content=(
                f"⚠️ Style Code **{sku}** found but no StockX price returned.\n"
                f"The shoe may not be actively listed. Try passing the SKU manually to double-check:\n"
                f"`!deal {ebay_url} {ebay_price} {sku} {size or '?'}`"
            ))
            return

        discount = calc_discount(ebay_price, stockx_price)

        if discount >= DISCOUNT_THRESHOLD:
            alert_channel = bot.get_channel(ALERT_CHANNEL_ID)
            if alert_channel:
                embed = make_alert_embed(title, sku, size, ebay_price, stockx_price, discount, ebay_url, str(message.author))
                await alert_channel.send(embed=embed)
            await processing_msg.edit(content=(
                f"✅ **{discount*100:.1f}% below StockX** — deal alert sent to your private channel!\n"
                f"Buy @ **${ebay_price:.2f}** · StockX value **${stockx_price:.2f}**"
            ))
        else:
            needed = stockx_price * (1 - DISCOUNT_THRESHOLD)
            await processing_msg.edit(content=(
                f"❌ Not a deal. Only **{discount*100:.1f}% below StockX** (need {DISCOUNT_THRESHOLD*100:.0f}%).\n"
                f"You'd need to pay **${needed:.2f}** or less.\n"
                f"eBay: **${ebay_price:.2f}** · StockX: **${stockx_price:.2f}**"
            ))

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[ERROR] {tb}")
        await processing_msg.edit(content=f"⚠️ Error: `{type(e).__name__}: {e}`")

bot.run(DISCORD_TOKEN)
