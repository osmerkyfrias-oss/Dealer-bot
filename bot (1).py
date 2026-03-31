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
    match = re.search(r"/itm/(\d+)", url)
    return match.group(1) if match else None


async def fetch_ebay_details(item_id: str) -> dict:
    url = f"https://www.ebay.com/itm/{item_id}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            html = await resp.text()

    soup = BeautifulSoup(html, "html.parser")

    # Title
    title_tag = soup.find("h1", {"class": re.compile(r"x-item-title")})
    title = title_tag.get_text(strip=True) if title_tag else None

    # Item specifics — eBay uses paired label/value divs
    sku, size = None, None
    labels = soup.select("div.ux-labels-values__labels-content")
    values = soup.select("div.ux-labels-values__values-content")

    for label_el, value_el in zip(labels, values):
        label = label_el.get_text(strip=True).lower()
        value = value_el.get_text(strip=True)

        # Catch "Style Code", "Style", "SKU", "MPN", "Model Number"
        if any(k in label for k in ["style code", "style", "sku", "mpn", "model number", "model"]):
            # Make sure it looks like a sneaker SKU (letters + digits + dash)
            if re.search(r"[A-Z]{1,3}\d{4,}", value, re.IGNORECASE) or re.search(r"\d{6}-\d{3}", value):
                sku = value.strip()

        # Catch "US Shoe Size", "Shoe Size", "Size"
        if any(k in label for k in ["us shoe size", "shoe size", "size"]):
            size_match = re.search(r"\d+\.?\d*", value)
            if size_match:
                size = size_match.group()

    return {"title": title, "sku": sku, "size": size}


async def fetch_stockx_price(sku: str, size: str) -> float | None:
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

    # Try size-specific price first
    if size:
        for variant in product.get("variants", []):
            if str(size) in str(variant.get("size", "")):
                price = variant.get("market", {}).get("lowestAsk") or variant.get("market", {}).get("lastSale")
                if price:
                    return float(price)

    # Fall back to overall lowest ask
    market = product.get("market", {})
    price = market.get("lowestAsk") or market.get("lastSale")
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

    # Allow manual override: !deal <url> <price> <sku> <size>
    manual_sku  = parts[3] if len(parts) >= 4 else None
    manual_size = parts[4] if len(parts) >= 5 else None

    if len(parts) < 3:
        await message.reply(
            "❌ Usage: `!deal <eBay URL> <negotiated price>`\n"
            "Example: `!deal https://www.ebay.com/itm/123456789012 75.00`\n"
            "Manual override: `!deal <url> <price> <StyleCode> <size>`"
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

    processing_msg = await message.reply("🔍 Checking deal… give me a second!")

    try:
        details = await fetch_ebay_details(item_id)
        sku   = manual_sku  or details["sku"]
        size  = manual_size or details["size"]
        title = details["title"] or "Unknown sneaker"

        if not sku:
            await processing_msg.edit(content=(
                f"⚠️ Found the listing (**{title}**) but couldn't detect the Style Code automatically.\n"
                f"Pass it manually: `!deal {ebay_url} {ebay_price} <StyleCode> <size>`\n"
                f"Example: `!deal {ebay_url} {ebay_price} DH6927-140 10`"
            ))
            return

        stockx_price = await fetch_stockx_price(sku, size)
        if not stockx_price:
            await processing_msg.edit(content=(
                f"⚠️ Style Code **{sku}** found but couldn't get a StockX price.\n"
                f"The shoe may not be actively listed on StockX."
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
                f"You'd need to pay **${needed:.2f}** or less to hit your threshold.\n"
                f"eBay: **${ebay_price:.2f}** · StockX: **${stockx_price:.2f}**"
            ))

    except Exception as e:
        await processing_msg.edit(content=f"⚠️ Something went wrong: `{e}`")
        raise

bot.run(DISCORD_TOKEN)
