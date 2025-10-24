# requirements:
#   pip install discord.py aiohttp python-dotenv
import os, json, asyncio, aiohttp, discord, re
from discord.ext import tasks
from datetime import datetime, timezone
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID    = int(os.getenv("CHANNEL_ID", "0"))
STATE_FILE    = os.getenv("STATE_FILE", "last_id.json")

# --- feeds to monitor ---
SOURCES = [
    {
        "key": "Holland",
        "label": "Holland",
        "api": "https://api.virtualprogaming.com/public/communities/Holland/movement/?limit=12&offset=0",
        "color": discord.Color.blurple(),
    },
    {
        "key": "Holland-5v5-next",
        "label": "Holland 5v5 Next",
        "api": "https://api.virtualprogaming.com/public/communities/Holland-5v5-next/movement/?limit=12&offset=0",
        "color": discord.Color.orange(),
    },
]

intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)

# --- simple caches to avoid repeated fetches ---
logo_cache: dict[str, str] = {}     # slug -> absolute image URL
imageid_cache: dict[str, str] = {}  # image_id -> absolute image URL

def _empty_state():
    return {"last_ids": {src["key"]: 0 for src in SOURCES}}

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # backward compatibility with old single-feed format
        if "last_ids" not in data:
            data = _empty_state()
        # ensure keys for all sources exist
        for src in SOURCES:
            data["last_ids"].setdefault(src["key"], 0)
        return data
    except Exception:
        return _empty_state()

def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass

def when_str(ts: str | None) -> str:
    if not ts:
        return "unknown"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Amsterdam"))
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return ts or "unknown"

async def probe_image(session: aiohttp.ClientSession, url: str | None) -> str | None:
    if not url:
        return None
    try:
        async with session.head(url, timeout=8, allow_redirects=True) as r:
            ct = r.headers.get("content-type","").lower()
            if r.status == 200 and ("image" in ct or ct == "application/octet-stream"):
                return str(r.url)
    except Exception:
        return None
    return None

async def resolve_image_id(session: aiohttp.ClientSession, image_id: str | None) -> str | None:
    """Try common CDN paths when API gives an image_id."""
    if not image_id:
        return None
    if image_id in imageid_cache:
        return imageid_cache[image_id]

    candidates = [
        f"https://virtualprogaming.com/media/{image_id}.png",
        f"https://virtualprogaming.com/media/{image_id}.webp",
        f"https://api.virtualprogaming.com/public/media/{image_id}.png",
        f"https://api.virtualprogaming.com/public/media/{image_id}.webp",
    ]
    for url in candidates:
        ok = await probe_image(session, url)
        if ok:
            imageid_cache[image_id] = ok
            return ok
    return None

async def fetch_logo_from_slug(session: aiohttp.ClientSession, slug: str | None) -> str | None:
    """Fetch team page HTML and extract a logo URL via og:image or /media/... references."""
    if not slug:
        return None
    if slug in logo_cache:
        return logo_cache[slug]

    page_url = f"https://virtualprogaming.com/team/{slug}"
    try:
        async with session.get(page_url, timeout=12) as resp:
            if resp.status != 200:
                return None
            html = await resp.text()

        # 1) Try Open Graph image
        m = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, flags=re.I)
        if m:
            og = m.group(1)
            ok = await probe_image(session, og)
            if ok:
                logo_cache[slug] = ok
                return ok

        # 2) Fallback: grab first /media/... image in HTML
        m2 = re.search(r'(https?://[^"\']*/media/[^"\']+\.(?:png|webp|jpg|jpeg))', html, flags=re.I)
        if m2:
            url = m2.group(1)
            ok = await probe_image(session, url)
            if ok:
                logo_cache[slug] = ok
                return ok
    except Exception:
        return None
    return None

async def build_embed(session: aiohttp.ClientSession, r: dict, src_label: str, src_color: discord.Color) -> discord.Embed:
    user = r.get("username") or "unknown"
    frm_name, frm_slug, frm_logo = r.get("from_name"), r.get("from_slug"), r.get("from_logo")
    to_name,  to_slug,  to_logo  = r.get("to_name"),   r.get("to_slug"),   r.get("to_logo")
    amt = r.get("amount") or 0
    ts  = r.get("datetime")

    title = f"[{src_label}] Transfer: {user}"
    desc  = f"{(frm_name or 'Free agent')} → {(to_name or 'Free agent')}"

    emb = discord.Embed(
        title=title,
        description=desc,
        color=src_color,
        timestamp=datetime.fromisoformat(ts.replace("Z","+00:00")) if ts else None
    )

    # Linked fields if slugs exist
    if frm_slug:
        emb.add_field(name="From", value=f"[{frm_name or 'Free agent'}](https://virtualprogaming.com/team/{frm_slug})", inline=True)
    else:
        emb.add_field(name="From", value=frm_name or "Free agent", inline=True)
    if to_slug:
        emb.add_field(name="To", value=f"[{to_name or 'Free agent'}](https://virtualprogaming.com/team/{to_slug})", inline=True)
    else:
        emb.add_field(name="To", value=to_name or "Free agent", inline=True)

    emb.add_field(name="Fee", value=str(amt), inline=True)
    emb.set_footer(text=when_str(ts))

    # Try avatar by id
    avatar_url    = await resolve_image_id(session, r.get("avatar"))

    # Prefer destination logo, then source. Try ID first, then slug scraping.
    to_logo_url   = await resolve_image_id(session, to_logo)   or await fetch_logo_from_slug(session, to_slug)
    from_logo_url = await resolve_image_id(session, frm_logo)  or await fetch_logo_from_slug(session, frm_slug)

    if avatar_url:
        emb.set_thumbnail(url=avatar_url)

    big = to_logo_url or from_logo_url
    if big:
        emb.set_image(url=big)

    return emb

def rid(x) -> int:
    try:
        return int(x.get("id", 0))
    except Exception:
        return 0

@client.event
async def on_ready():
    channel = client.get_channel(CHANNEL_ID)
    if channel:
        labels = ", ".join(src["label"] for src in SOURCES)
        await channel.send(embed=discord.Embed(
            title="Transfer bot online",
            description=f"Monitoring feeds: {labels}.",
            color=discord.Color.green()
        ))
    monitor.start()

@tasks.loop(seconds=180)
async def monitor():
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        return

    state = load_state()

    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for src in SOURCES:
                last_seen = int(state["last_ids"].get(src["key"], 0))
                try:
                    async with session.get(src["api"], headers={"Accept":"application/json"}) as resp:
                        if resp.status != 200:
                            continue
                        payload = await resp.json()
                    rows = payload.get("data", [])
                    if not rows:
                        continue

                    new_items = [r for r in rows if rid(r) > last_seen]
                    if not new_items:
                        continue

                    new_items.sort(key=rid)  # oldest first
                    for r in new_items:
                        try:
                            embed = await build_embed(session, r, src_label=src["label"], src_color=src["color"])
                            await channel.send(embed=embed)
                        except Exception:
                            # fallback text
                            frm = r.get("from_name") or "Free agent"
                            to  = r.get("to_name") or "Free agent"
                            user = r.get("username") or "unknown"
                            await channel.send(f"[{src['label']}] Transfer: **{user}** — {frm} → {to} • {when_str(r.get('datetime'))}")
                        last_seen = max(last_seen, rid(r))

                    state["last_ids"][src["key"]] = last_seen
                except Exception:
                    continue
    except Exception:
        pass
    finally:
        save_state(state)

if __name__ == "__main__":
    if not DISCORD_TOKEN or CHANNEL_ID <= 0:
        raise SystemExit("Set DISCORD_TOKEN and a valid CHANNEL_ID in .env")
    client.run(DISCORD_TOKEN)