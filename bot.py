# requirements:
#   pip install discord.py aiohttp python-dotenv Pillow
import os, json, asyncio, aiohttp, discord, io
from pathlib import Path
from discord.ext import tasks
from datetime import datetime, timezone
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw, ImageFont

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID    = int(os.getenv("CHANNEL_ID", "0"))
STATE_FILE    = os.getenv("STATE_FILE", "last_id.json")

ASSETS_DIR  = Path(__file__).parent / "assets"
ASSET_BASE  = "https://vpg-prod-user-uploads.fra1.cdn.digitaloceanspaces.com/"
TEAM_API    = "https://api.virtualprogaming.com/public/teams/{slug}/"
USER_API    = "https://api.virtualprogaming.com/public/users/{username}/"
FONT_BOLD   = ASSETS_DIR / "DejaVuSans-Bold.ttf"
FREE_AGENT_IMG   = ASSETS_DIR / "free_agent.png"
DEFAULT_AVATAR_IMG = ASSETS_DIR / "default_avatar.png"

# --- feeds to monitor ---
SOURCES = [
    {
        "key": "Holland",
        "label": "Eerste Divisie",
        "api": "https://api.virtualprogaming.com/public/communities/Holland/movement/?limit=12&offset=0",
        "color": discord.Color.orange(),
    },
    {
        "key": "Holland-6v6-next",
        "label": "6v6 Divisie",
        "api": "https://api.virtualprogaming.com/public/communities/Holland-6v6-next/movement/?limit=12&offset=0",
        "color": discord.Color.blurple(),
    },
]

intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)

# --- simple caches to avoid repeated fetches ---
image_bytes_cache: dict[str, bytes | None] = {}   # image_id -> raw bytes (or None if unavailable)
slug_logo_cache: dict[str, str | None] = {}       # team slug -> logo_id (fallback lookup)
nationality_cache: dict[str, str | None] = {}     # username -> nationality code (or None if unavailable)

def _tag_flag(subcode: str) -> str:
    """Build a Unicode subdivision flag (e.g. England/Scotland/Wales) from a lowercase tag string like 'gbeng'."""
    return chr(0x1F3F4) + "".join(chr(0xE0000 + ord(c)) for c in subcode) + chr(0xE007F)

# Great Britain's home nations use ISO 3166-2 subdivision codes rather than a plain country code.
SUBDIVISION_FLAGS = {
    "GB-ENG": _tag_flag("gbeng"),
    "GB-SCT": _tag_flag("gbsct"),
    "GB-WLS": _tag_flag("gbwls"),
}

UNKNOWN_NATIONALITY_FLAG = "🌍"

def flag_for_nationality(code: str | None) -> str:
    """Return a flag (emoji if recognized, raw code otherwise), or a globe if no code is known."""
    if not code:
        return UNKNOWN_NATIONALITY_FLAG
    code = code.strip().upper()
    if code in SUBDIVISION_FLAGS:
        return SUBDIVISION_FLAGS[code]
    if len(code) == 2 and code.isalpha():
        base = 0x1F1E6
        return "".join(chr(base + (ord(c) - ord("A"))) for c in code)
    return code

async def fetch_nationality(session: aiohttp.ClientSession, username: str | None) -> str | None:
    if not username:
        return None
    if username in nationality_cache:
        return nationality_cache[username]
    nat = None
    try:
        async with session.get(USER_API.format(username=username), timeout=10) as r:
            if r.status == 200:
                payload = await r.json()
                nat = payload.get("nationality")
    except Exception:
        pass
    nationality_cache[username] = nat
    return nat

def _empty_state():
    return {"last_ids": {src["key"]: 0 for src in SOURCES}}

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "last_ids" not in data:
            data = _empty_state()
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

async def fetch_image_bytes(session: aiohttp.ClientSession, image_id: str | None) -> bytes | None:
    """Fetch raw image bytes from the VPG asset CDN for a given image id."""
    if not image_id:
        return None
    if image_id in image_bytes_cache:
        return image_bytes_cache[image_id]
    try:
        async with session.get(ASSET_BASE + image_id, timeout=10) as r:
            if r.status == 200:
                data = await r.read()
                image_bytes_cache[image_id] = data
                return data
    except Exception:
        pass
    image_bytes_cache[image_id] = None
    return None

async def fetch_logo_id_from_slug(session: aiohttp.ClientSession, slug: str | None) -> str | None:
    """Fallback: look up a team's logo_id via the team detail API when the movement feed omits it."""
    if not slug:
        return None
    if slug in slug_logo_cache:
        return slug_logo_cache[slug]
    logo_id = None
    try:
        async with session.get(TEAM_API.format(slug=slug), timeout=10) as r:
            if r.status == 200:
                payload = await r.json()
                logo_id = payload.get("logo_id")
    except Exception:
        pass
    slug_logo_cache[slug] = logo_id
    return logo_id

async def fetch_team_crest_bytes(session: aiohttp.ClientSession, logo_id: str | None, slug: str | None) -> bytes | None:
    data = await fetch_image_bytes(session, logo_id)
    if data:
        return data
    fallback_id = await fetch_logo_id_from_slug(session, slug)
    if fallback_id and fallback_id != logo_id:
        return await fetch_image_bytes(session, fallback_id)
    return None

def _load_crest(data: bytes | None) -> Image.Image:
    if data:
        try:
            return Image.open(io.BytesIO(data)).convert("RGBA")
        except Exception:
            pass
    return Image.open(FREE_AGENT_IMG).convert("RGBA")

def _fit(img: Image.Image, box: int) -> Image.Image:
    img = img.copy()
    img.thumbnail((box, box), Image.LANCZOS)
    return img

def _text_fit(draw: ImageDraw.ImageDraw, text: str, font_path: Path, max_width: int, size: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(font_path), size)
    while size > 14 and draw.textlength(text, font=font) > max_width:
        size -= 2
        font = ImageFont.truetype(str(font_path), size)
    return font

def build_banner(from_crest: bytes | None, to_crest: bytes | None,
                  from_name: str | None, to_name: str | None,
                  accent_rgb: tuple[int, int, int]) -> io.BytesIO:
    W, H = 1000, 340
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    crest_box = 240
    from_img = _fit(_load_crest(from_crest), crest_box)
    to_img   = _fit(_load_crest(to_crest), crest_box)

    crest_cy = 140
    from_cx, to_cx = 190, W - 190
    canvas.alpha_composite(from_img, (from_cx - from_img.width // 2, crest_cy - from_img.height // 2))
    canvas.alpha_composite(to_img,   (to_cx - to_img.width // 2,   crest_cy - to_img.height // 2))

    # arrow (two chevrons) in the accent color, centered between the crests
    ax, ay = W // 2, crest_cy
    chevron_w, chevron_h = 46, 70
    for offset in (-30, 30):
        cx = ax + offset
        draw.polygon(
            [(cx - chevron_w // 2, ay - chevron_h // 2),
             (cx + chevron_w // 2, ay),
             (cx - chevron_w // 2, ay + chevron_h // 2)],
            fill=(*accent_rgb, 255),
        )

    # team names below each crest
    name_y = crest_cy + crest_box // 2 + 24
    max_name_w = 300
    for cx, name in ((from_cx, from_name or "Free Agent"), (to_cx, to_name or "Free Agent")):
        font = _text_fit(draw, name, FONT_BOLD, max_name_w, 34)
        w = draw.textlength(name, font=font)
        draw.text((cx - w / 2, name_y), name, font=font, fill=(235, 235, 235, 255))

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    buf.seek(0)
    return buf

def build_avatar(avatar_bytes: bytes | None) -> io.BytesIO:
    if avatar_bytes:
        try:
            img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
        except Exception:
            img = Image.open(DEFAULT_AVATAR_IMG).convert("RGBA")
    else:
        img = Image.open(DEFAULT_AVATAR_IMG).convert("RGBA")

    side = min(img.size)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((256, 256), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

async def build_embed(session: aiohttp.ClientSession, r: dict, src_label: str, src_color: discord.Color) -> tuple[discord.Embed, list[discord.File]]:
    user = r.get("username") or "unknown"
    frm_name, frm_slug, frm_logo = r.get("from_name"), r.get("from_slug"), r.get("from_logo")
    to_name,  to_slug,  to_logo  = r.get("to_name"),   r.get("to_slug"),   r.get("to_logo")
    amt = r.get("amount") or 0
    ts  = r.get("datetime")

    nationality = await fetch_nationality(session, r.get("username"))
    flag = flag_for_nationality(nationality)

    title = f"[{src_label}] Transfer: {user} {flag}"
    desc  = f"{(frm_name or 'Free agent')} → {(to_name or 'Free agent')}"

    emb = discord.Embed(
        title=title,
        description=desc,
        color=src_color,
        timestamp=datetime.fromisoformat(ts.replace("Z","+00:00")) if ts else None
    )

    if frm_slug:
        emb.add_field(name="Van", value=f"[{frm_name or 'Free agent'}](https://virtualprogaming.com/team/{frm_slug})", inline=True)
    else:
        emb.add_field(name="Van", value=frm_name or "Free agent", inline=True)
    if to_slug:
        emb.add_field(name="Naar", value=f"[{to_name or 'Free agent'}](https://virtualprogaming.com/team/{to_slug})", inline=True)
    else:
        emb.add_field(name="Naar", value=to_name or "Free agent", inline=True)

    emb.add_field(name="Bedrag", value=str(amt), inline=True)
    emb.set_footer(text=when_str(ts))

    avatar_bytes = await fetch_image_bytes(session, r.get("avatar"))
    from_crest_bytes = await fetch_team_crest_bytes(session, frm_logo, frm_slug)
    to_crest_bytes   = await fetch_team_crest_bytes(session, to_logo, to_slug)

    avatar_buf = build_avatar(avatar_bytes)
    banner_buf = build_banner(from_crest_bytes, to_crest_bytes, frm_name, to_name, src_color.to_rgb())

    files = [
        discord.File(avatar_buf, filename="avatar.png"),
        discord.File(banner_buf, filename="banner.png"),
    ]
    emb.set_thumbnail(url="attachment://avatar.png")
    emb.set_image(url="attachment://banner.png")

    return emb, files

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
                            embed, files = await build_embed(session, r, src_label=src["label"], src_color=src["color"])
                            await channel.send(embed=embed, files=files)
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
