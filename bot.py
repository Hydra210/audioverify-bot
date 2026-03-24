import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask, render_template_string
from threading import Thread
import os, asyncio, aiohttp, json, re, time, random
from datetime import datetime, timezone
from collections import deque
from pathlib import Path

# ============================================================
#                          CONFIG
# ============================================================

TOKEN       = os.getenv("DISCORD_TOKEN")
CONFIG_FILE = Path("bot_config.json")
LOG_BUFFER  = deque(maxlen=500)
ORANGE      = discord.Color.from_rgb(255, 136, 0)

COMMANDS_WITH_PERMS = [
    ("verify",      "Verify a Roblox audio asset"),
    ("resetup",     "Restart the full bot setup"),
    ("addcookie",   "Add another Roblox cookie"),
    ("adduniverse", "Add another universe ID"),
    ("botlog",      "View the live bot log"),
    ("setupperms",  "Re-run the command permission setup"),
]

# ============================================================
#                     RATE LIMITING
# ============================================================

VERIFY_USER_MAX    = 5    # max verifies per user per window
VERIFY_GUILD_MAX   = 15   # max verifies per guild per window
VERIFY_WINDOW      = 60   # seconds
GUILD_CONCURRENCY  = 3    # max parallel verify requests per guild

_user_rl:  dict = {}  # user_id  -> deque of timestamps
_guild_rl: dict = {}  # guild_id -> deque of timestamps
_guild_sem: dict = {} # guild_id -> asyncio.Semaphore

def _check_rl(store: dict, key, max_calls: int, window: int):
    now = time.time()
    q   = store.setdefault(key, deque())
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= max_calls:
        wait = int(window - (now - q[0])) + 1
        return False, wait
    q.append(now)
    return True, 0

def check_verify_rate(guild_id: str, user_id: int):
    ok, wait = _check_rl(_user_rl, user_id, VERIFY_USER_MAX, VERIFY_WINDOW)
    if not ok:
        return False, f"You're verifying too fast. Try again in {wait}s."
    ok, wait = _check_rl(_guild_rl, guild_id, VERIFY_GUILD_MAX, VERIFY_WINDOW)
    if not ok:
        return False, f"Server is verifying too fast. Try again in {wait}s."
    return True, None

def get_guild_sem(guild_id: str) -> asyncio.Semaphore:
    if guild_id not in _guild_sem:
        _guild_sem[guild_id] = asyncio.Semaphore(GUILD_CONCURRENCY)
    return _guild_sem[guild_id]

# ============================================================
#                         LOGGING
# ============================================================

def log(msg: str):
    ts   = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    LOG_BUFFER.append(line)
    print(line)

# ============================================================
#                        CONFIG I/O
# ============================================================

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}

def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

def gcfg(cfg: dict, guild_id: str) -> dict:
    return cfg.get(str(guild_id), {})

def set_gcfg(cfg: dict, guild_id: str, data: dict):
    cfg[str(guild_id)] = data
    save_config(cfg)

def save_data_update(data: dict):
    cfg      = load_config()
    guild_id = str(data["guild_id"])
    g        = gcfg(cfg, guild_id)
    for field in ("universe_ids", "game_names", "cookies", "proxy_url", "proxy_secret"):
        if field in data:
            g[field] = data[field]
    set_gcfg(cfg, guild_id, g)

# ============================================================
#                    COOKIE ROTATION
# ============================================================

_cookie_index: dict = {}  # guild_id -> int

def next_cookie(cfg: dict, guild_id: str) -> str:
    g       = gcfg(cfg, guild_id)
    cookies = g.get("cookies", [])
    if not cookies:
        return g.get("cookie", "")
    idx     = _cookie_index.get(guild_id, 0) % len(cookies)
    _cookie_index[guild_id] = idx + 1
    return cookies[idx]

# ============================================================
#                        BOT + FLASK
# ============================================================

intents         = discord.Intents.all()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

flask_app = Flask(__name__)

LOG_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>AudioVerify Live Log</title>
<meta http-equiv="refresh" content="5">
<style>
  body{background:#0d0d0d;color:#c9c9c9;font-family:monospace;padding:24px;margin:0}
  h1{color:#ff8800;font-size:1em;margin-bottom:16px;letter-spacing:1px}
  .line{margin:1px 0;font-size:.82em;white-space:pre-wrap;word-break:break-all;padding:2px 4px}
  .err{color:#ff5555}.warn{color:#ffcc00}.ok{color:#55ff88}
  .foot{margin-top:20px;font-size:.72em;color:#444}
</style>
</head>
<body>
<h1>AUDIOVERIFY BOT &mdash; LIVE LOG</h1>
{% for l in lines %}
  {% if "error" in l.lower() or "failed" in l.lower() %}
    <div class="line err">{{ l }}</div>
  {% elif "warn" in l.lower() %}
    <div class="line warn">{{ l }}</div>
  {% elif "ok" in l.lower() or "success" in l.lower() or "ready" in l.lower() or "synced" in l.lower() %}
    <div class="line ok">{{ l }}</div>
  {% else %}
    <div class="line">{{ l }}</div>
  {% endif %}
{% endfor %}
<div class="foot">auto-refreshes every 5s &bull; {{ n }} lines buffered</div>
</body>
</html>"""

@flask_app.route("/")
def flask_home():
    return "AudioVerify Bot is running."

@flask_app.route("/log")
def flask_log():
    lines = list(LOG_BUFFER)
    return render_template_string(LOG_HTML, lines=lines, n=len(lines))

@flask_app.route("/health")
def flask_health():
    return "OK", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), use_reloader=False)

def keep_alive():
    Thread(target=run_flask, daemon=True).start()

# ============================================================
#                        HELPERS
# ============================================================

def emb(desc: str = None, title: str = None, color: discord.Color = None) -> discord.Embed:
    return discord.Embed(title=title, description=desc, color=color or ORANGE)

def has_perm(cfg: dict, guild_id: str, cmd: str, member: discord.Member) -> bool:
    roles = gcfg(cfg, guild_id).get("command_roles", {}).get(cmd, [])
    if "@everyone" in roles:
        return True
    return bool({str(r.id) for r in member.roles} & set(roles))

async def fetch_discord_invite(code: str):
    url = f"https://discord.com/api/v9/invites/{code}?with_counts=true"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers={"User-Agent": "AudioVerifyBot/1.0"},
                             timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    return await r.json(), None
                return None, f"HTTP {r.status}"
    except Exception as e:
        return None, str(e)

async def fetch_roblox_game(universe_id: str):
    url = f"https://games.roblox.com/v1/games?universeIds={universe_id}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    data = await r.json()
                    games = data.get("data", [])
                    return games[0] if games else None
    except Exception as e:
        log(f"[roblox] fetch_game: {e}")
    return None

async def fetch_roblox_thumb(universe_id: str):
    url = (
        f"https://thumbnails.roblox.com/v1/games/icons"
        f"?universeIds={universe_id}&returnPolicy=PlaceHolder&size=512x512&format=Png&isCircular=false"
    )
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    data = await r.json()
                    items = data.get("data", [])
                    if items:
                        return items[0].get("imageUrl")
    except Exception as e:
        log(f"[roblox] fetch_thumb: {e}")
    return None

async def _backoff(attempt: int, retry_after: int = 0):
    base  = retry_after if retry_after else (2 ** attempt)
    jitter = random.uniform(0.5, 1.5)
    delay  = min(base + jitter, 30)
    log(f"[proxy] backing off {delay:.1f}s (attempt {attempt + 1})")
    await asyncio.sleep(delay)

async def proxy_get(proxy_url: str, secret: str, path: str, cookie: str = None, retries: int = 3):
    headers = {"x-proxy-secret": secret}
    if cookie:
        headers["x-cookie-override"] = cookie
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(proxy_url + path, headers=headers,
                                 timeout=aiohttp.ClientTimeout(total=15)) as r:
                    log(f"[proxy] GET {path} -> {r.status}")
                    if r.status == 200:
                        return await r.json(), None
                    if r.status == 429:
                        retry_after = int(r.headers.get("Retry-After", 5))
                        log(f"[proxy] 429 rate limited (retry-after {retry_after}s)")
                        if attempt < retries - 1:
                            await _backoff(attempt, retry_after)
                            continue
                        return None, "Rate limited by Roblox. Please wait a moment and try again."
                    if r.status in (502, 503, 504):
                        if attempt < retries - 1:
                            await _backoff(attempt)
                            continue
                        return None, f"Proxy server error (HTTP {r.status}). Try again shortly."
                    return None, f"HTTP {r.status}"
        except asyncio.TimeoutError:
            log(f"[proxy] GET timeout (attempt {attempt + 1})")
            if attempt < retries - 1:
                await _backoff(attempt)
                continue
            return None, "Request timed out. Try again."
        except Exception as e:
            log(f"[proxy] GET error: {e}")
            if attempt < retries - 1:
                await _backoff(attempt)
                continue
            return None, str(e)
    return None, "Request failed after retries."

async def proxy_patch(proxy_url: str, secret: str, path: str, body: dict, cookie: str = None, retries: int = 3):
    headers = {"Content-Type": "application/json", "x-proxy-secret": secret}
    if cookie:
        headers["x-cookie-override"] = cookie
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.patch(proxy_url + path, headers=headers, json=body,
                                   timeout=aiohttp.ClientTimeout(total=15)) as r:
                    log(f"[proxy] PATCH {path} -> {r.status}")
                    if r.status == 200:
                        return True, None
                    if r.status == 429:
                        retry_after = int(r.headers.get("Retry-After", 5))
                        log(f"[proxy] 429 rate limited (retry-after {retry_after}s)")
                        if attempt < retries - 1:
                            await _backoff(attempt, retry_after)
                            continue
                        return False, "Rate limited by Roblox. Please wait a moment and try again."
                    if r.status in (502, 503, 504):
                        if attempt < retries - 1:
                            await _backoff(attempt)
                            continue
                        return False, f"Proxy server error (HTTP {r.status}). Try again shortly."
                    try:
                        data = await r.json()
                        return False, data.get("message", f"HTTP {r.status}")
                    except Exception:
                        return False, f"HTTP {r.status}"
        except asyncio.TimeoutError:
            log(f"[proxy] PATCH timeout (attempt {attempt + 1})")
            if attempt < retries - 1:
                await _backoff(attempt)
                continue
            return False, "Request timed out. Try again."
        except Exception as e:
            log(f"[proxy] PATCH error: {e}")
            if attempt < retries - 1:
                await _backoff(attempt)
                continue
            return False, str(e)
    return False, "Request failed after retries."


async def fetch_cookie_profile(proxy_url: str, secret: str, cookie: str):
    """Fetch Roblox profile for a cookie via the proxy /me route.
    Returns (display_name, username, avatar_url) or ('Unknown', 'Unknown', None)."""
    try:
        async with aiohttp.ClientSession() as s:
            headers = {"x-proxy-secret": secret, "x-cookie-override": cookie}
            async with s.get(proxy_url + "/me", headers=headers,
                             timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    data         = await r.json()
                    user_id      = data.get("id")
                    username     = data.get("name") or "Unknown"
                    display_name = data.get("displayName") or username
                    avatar_url   = await _fetch_avatar(s, user_id)
                    return display_name, username, avatar_url
    except Exception as e:
        log(f"[me] error: {e}")
    return "Unknown", "Unknown", None


async def validate_cookie_direct(cookie: str):
    """Validate a cookie by calling the Roblox API directly (no proxy needed).
    Returns (display_name, username, avatar_url) or (None, None, None) if invalid."""
    try:
        async with aiohttp.ClientSession() as s:
            hdrs = {"Cookie": f".ROBLOSECURITY={cookie}"}
            async with s.get("https://users.roblox.com/v1/users/authenticated",
                             headers=hdrs, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data         = await r.json()
                    user_id      = data.get("id")
                    username     = data.get("name") or "Unknown"
                    display_name = data.get("displayName") or username
                    avatar_url   = await _fetch_avatar(s, user_id)
                    return display_name, username, avatar_url
                return None, None, None
    except Exception as e:
        log(f"[validate_cookie] error: {e}")
    return None, None, None


async def _fetch_avatar(session: aiohttp.ClientSession, user_id) -> str | None:
    """Fetch a Roblox avatar headshot URL for a user ID."""
    if not user_id:
        return None
    try:
        url = f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={user_id}&size=150x150&format=Png"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 200:
                imgs = (await r.json()).get("data", [])
                return imgs[0].get("imageUrl") if imgs else None
    except Exception:
        pass
    return None

# ============================================================
#                       SETUP STATE
# ============================================================

_state: dict = {}

def get_state(uid: int) -> dict:
    return _state.get(uid, {})

def set_state(uid: int, step: str, data: dict = None, perm_index: int = 0):
    _state[uid] = {"step": step, "data": data or {}, "perm_index": perm_index}

# ============================================================
#                       SETUP STEPS
# ============================================================

async def step_welcome(interaction: discord.Interaction):
    e = discord.Embed(
        title="AudioVerify Bot Setup",
        description=(
            "This bot lets your Discord members verify Roblox audio assets "
            "directly from your server, adding your game as a collaborator on any audio.\n\n"
            "What we will set up:\n"
            "- Your Discord server\n"
            "- One or more Roblox game universe IDs\n"
            "- One or more Roblox account cookies\n"
            "- Your proxy server details\n"
            "- Command permissions per role\n\n"
            "Everything runs in DMs for your privacy."
        ),
        color=ORANGE
    )
    set_state(interaction.user.id, "welcome")
    await interaction.response.send_message(embed=e, view=_ContinueBtn("invite"), ephemeral=True)


async def step_invite(interaction: discord.Interaction, data: dict):
    set_state(interaction.user.id, "await_invite", data)
    e = emb(title="Step 1 - Your Discord Server",
            desc="Reply to this message with your server invite link.\nExample: `https://discord.gg/yourcode`")
    if interaction.response.is_done():
        await interaction.followup.send(embed=e, ephemeral=True)
    else:
        await interaction.response.send_message(embed=e, ephemeral=True)


async def step_universe(interaction: discord.Interaction, data: dict, adding_more: bool = False):
    if adding_more:
        desc = "Reply with another **Roblox Universe ID** to add, or type `done` to finish."
    else:
        desc = (
            "Reply with your **Roblox Universe ID**.\n\n"
            "Find it in Roblox Studio -> Game Settings -> Basic Info."
        )
    set_state(interaction.user.id, "await_universe", {**data, "adding_more_uni": adding_more})
    e = emb(title="Universe ID", desc=desc)
    if interaction.response.is_done():
        await interaction.followup.send(embed=e, ephemeral=True)
    else:
        await interaction.response.send_message(embed=e, ephemeral=True)


async def step_cookie_warn1(interaction: discord.Interaction, data: dict):
    set_state(interaction.user.id, "cookie_warn1", data)
    e = discord.Embed(
        title="Security Warning - Read This",
        description=(
            "The next step requires your `.ROBLOSECURITY` cookie.\n\n"
            "Risks:\n"
            "- A compromised cookie means a compromised Roblox account\n"
            "- Use a dedicated alt account, not your main\n\n"
            "What we do with it:\n"
            "- Stored in `bot_config.json` on your instance only\n"
            "- Not logged anywhere in any output\n"
            "- Only used to authenticate Roblox API calls via your proxy\n\n"
            "Press continue to proceed."
        ),
        color=discord.Color.red()
    )
    if interaction.response.is_done():
        await interaction.followup.send(embed=e, view=_CookieWarn1Btn(), ephemeral=True)
    else:
        await interaction.response.send_message(embed=e, view=_CookieWarn1Btn(), ephemeral=True)


async def step_cookie_warn2(interaction: discord.Interaction, data: dict):
    set_state(interaction.user.id, "cookie_warn2", data)
    e = discord.Embed(
        title="Final Warning",
        description=(
            "Are you absolutely sure?\n\n"
            "You are about to send your `.ROBLOSECURITY` cookie.\n\n"
            "We strongly recommend a dedicated Roblox alt account that only has access "
            "to the groups and assets you need.\n\n"
            "You can reset cookies anytime with `/addcookie`."
        ),
        color=discord.Color.from_rgb(220, 40, 40)
    )
    if interaction.response.is_done():
        await interaction.followup.send(embed=e, view=_CookieWarn2Btn(), ephemeral=True)
    else:
        await interaction.response.send_message(embed=e, view=_CookieWarn2Btn(), ephemeral=True)


async def step_cookie_input(interaction: discord.Interaction, data: dict, adding_more: bool = False):
    if adding_more:
        desc = "Reply with another `.ROBLOSECURITY` cookie to add, or type `done` to finish."
    else:
        desc = (
            "Reply with your `.ROBLOSECURITY` cookie.\n\n"
            "To find it:\n"
            "1. Open Roblox in your browser\n"
            "2. Open DevTools (F12) -> Application -> Cookies -> roblox.com\n"
            "3. Copy the value of `.ROBLOSECURITY`\n\n"
            "Your message will be deleted immediately after the bot reads it."
        )
    set_state(interaction.user.id, "await_cookie", {**data, "adding_more_cookie": adding_more})
    e = emb(title="Enter Cookie", desc=desc)
    if interaction.response.is_done():
        await interaction.followup.send(embed=e, ephemeral=True)
    else:
        await interaction.response.send_message(embed=e, ephemeral=True)


async def step_proxy_url(interaction: discord.Interaction, data: dict):
    set_state(interaction.user.id, "await_proxy_url", data)
    e = emb(title="Proxy Server URL",
            desc="Reply with your proxy server URL.\nExample: `https://your-proxy.onrender.com`")
    if interaction.response.is_done():
        await interaction.followup.send(embed=e, ephemeral=True)
    else:
        await interaction.response.send_message(embed=e, ephemeral=True)


async def step_proxy_secret(interaction: discord.Interaction, data: dict):
    set_state(interaction.user.id, "await_proxy_secret", data)
    e = emb(title="Proxy Secret",
            desc="Reply with your proxy secret - the `PROXY_SECRET` env var on your proxy server.")
    if interaction.response.is_done():
        await interaction.followup.send(embed=e, ephemeral=True)
    else:
        await interaction.response.send_message(embed=e, ephemeral=True)


async def step_perms(channel: discord.DMChannel, uid: int, guild: discord.Guild, data: dict):
    state = get_state(uid)
    idx   = state.get("perm_index", 0)

    if idx >= len(COMMANDS_WITH_PERMS):
        cfg      = load_config()
        guild_id = str(data["guild_id"])
        g        = gcfg(cfg, guild_id)
        g["setup_complete"] = True
        set_gcfg(cfg, guild_id, g)
        log(f"[setup] complete for guild {guild_id}")
        unis    = data.get("universe_ids", [])
        cookies = data.get("cookies", [])
        e = discord.Embed(
            title="Setup Complete",
            description=(
                f"AudioVerify Bot is ready.\n\n"
                f"Universe IDs: {len(unis)} configured\n"
                f"Cookies: {len(cookies)} configured\n\n"
                "Commands:\n"
                + "\n".join(f"- `/{n}` - {d}" for n, d in COMMANDS_WITH_PERMS)
                + "\n\nUse `/botlog` to view the live log."
            ),
            color=discord.Color.green()
        )
        await channel.send(embed=e)
        _state.pop(uid, None)
        return

    cmd_name, cmd_desc = COMMANDS_WITH_PERMS[idx]
    roles   = guild.roles
    options = [discord.SelectOption(label="@everyone", value="@everyone", description="All members")]
    for r in reversed(roles):
        if r.name == "@everyone" or r.managed:
            continue
        options.append(discord.SelectOption(label=f"@{r.name}", value=str(r.id)))
        if len(options) >= 25:
            break

    e = emb(
        title=f"Permissions - /{cmd_name}",
        desc=f"Description: {cmd_desc}\n\nSelect the role(s) that can use this command."
    )
    view = _RoleSelect(options, cmd_name, data, idx, guild.id)
    await channel.send(embed=e, view=view)

# ============================================================
#                         UI VIEWS
# ============================================================

class _ContinueBtn(discord.ui.View):
    def __init__(self, next_step: str):
        super().__init__(timeout=300)
        self.next_step = next_step

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.green)
    async def go(self, interaction: discord.Interaction, _btn):
        state = get_state(interaction.user.id)
        data  = state.get("data", {})
        if self.next_step == "invite":
            await step_invite(interaction, data)


class _AddAnotherView(discord.ui.View):
    def __init__(self, kind: str, data: dict):
        super().__init__(timeout=120)
        self.kind = kind  # "universe" or "cookie"
        self.data = data

    @discord.ui.button(label="Add Another", style=discord.ButtonStyle.primary)
    async def add(self, interaction: discord.Interaction, _btn):
        if self.kind == "universe":
            await step_universe(interaction, self.data, adding_more=True)
        else:
            await step_cookie_input(interaction, self.data, adding_more=True)

    @discord.ui.button(label="Done", style=discord.ButtonStyle.green)
    async def done(self, interaction: discord.Interaction, _btn):
        if self.kind == "universe":
            if self.data.get("_mode") == "adduniverse":
                save_data_update(self.data)
                _state.pop(interaction.user.id, None)
                await interaction.response.send_message(
                    embed=emb(f"✅ Universe IDs updated. {len(self.data.get('universe_ids', []))} configured."),
                    ephemeral=True
                )
            else:
                await step_cookie_warn1(interaction, self.data)
        else:
            if self.data.get("_mode") == "addcookie":
                save_data_update(self.data)
                _state.pop(interaction.user.id, None)
                await interaction.response.send_message(
                    embed=emb(f"✅ Cookies updated. {len(self.data.get('cookies', []))} configured."),
                    ephemeral=True
                )
            else:
                await step_proxy_url(interaction, self.data)


class _ConfirmServerView(discord.ui.View):
    def __init__(self, guild_info: dict, data: dict):
        super().__init__(timeout=120)
        self.guild_info = guild_info
        self.data       = data

    @discord.ui.button(label="Yes, that's my server", style=discord.ButtonStyle.green)
    async def yes(self, interaction: discord.Interaction, _btn):
        self.data["guild_id"]   = self.guild_info["id"]
        self.data["guild_name"] = self.guild_info["name"]
        self.data["universe_ids"] = []
        await step_universe(interaction, self.data)

    @discord.ui.button(label="No, re-enter", style=discord.ButtonStyle.red)
    async def no(self, interaction: discord.Interaction, _btn):
        await step_invite(interaction, self.data)


class _ConfirmGameView(discord.ui.View):
    def __init__(self, game: dict, universe_id: str, data: dict):
        super().__init__(timeout=120)
        self.game        = game
        self.universe_id = universe_id
        self.data        = data

    @discord.ui.button(label="Yes, add this game", style=discord.ButtonStyle.green)
    async def yes(self, interaction: discord.Interaction, _btn):
        self.data.setdefault("universe_ids", [])
        if self.universe_id not in self.data["universe_ids"]:
            self.data["universe_ids"].append(self.universe_id)
        self.data.setdefault("game_names", {})[self.universe_id] = self.game.get("name", "Unknown")
        unis = self.data["universe_ids"]
        count = len(unis)
        e = emb(
            desc=(
                f"Added. You now have {count} universe ID(s) configured.\n\n"
                "Add another game or continue to the next step."
            )
        )
        await interaction.response.send_message(embed=e, view=_AddAnotherView("universe", self.data), ephemeral=True)

    @discord.ui.button(label="No, re-enter", style=discord.ButtonStyle.red)
    async def no(self, interaction: discord.Interaction, _btn):
        await step_universe(interaction, self.data)


class _CookieWarn1Btn(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="I Understand, Continue", style=discord.ButtonStyle.grey)
    async def go(self, interaction: discord.Interaction, _btn):
        state = get_state(interaction.user.id)
        await step_cookie_warn2(interaction, state.get("data", {}))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, _btn):
        _state.pop(interaction.user.id, None)
        await interaction.response.send_message(embed=emb("Setup cancelled."), ephemeral=True)


class _CookieWarn2Btn(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Yes, I Accept the Risks", style=discord.ButtonStyle.grey)
    async def go(self, interaction: discord.Interaction, _btn):
        state = get_state(interaction.user.id)
        data  = state.get("data", {})
        data.setdefault("cookies", [])
        await step_cookie_input(interaction, data)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, _btn):
        _state.pop(interaction.user.id, None)
        await interaction.response.send_message(embed=emb("Setup cancelled."), ephemeral=True)


class _UniverseSelect(discord.ui.View):
    def __init__(self, universe_ids: list, game_names: dict,
                 audio_id: str, audio_name: str,
                 proxy_url: str, secret: str, cookie: str):
        super().__init__(timeout=60)
        self.audio_id   = audio_id
        self.audio_name = audio_name
        self.proxy_url  = proxy_url
        self.secret     = secret
        self.cookie     = cookie

        options = []
        for uid in universe_ids:
            name = game_names.get(uid, uid)
            options.append(discord.SelectOption(label=name, value=uid, description=f"Universe ID: {uid}"))

        sel          = discord.ui.Select(placeholder="Select which game to add this audio to", options=options)
        sel.callback = self._picked
        self.add_item(sel)

    async def _picked(self, interaction: discord.Interaction):
        universe_id = interaction.data["values"][0]
        await interaction.response.defer(ephemeral=True)
        log(f"[verify] granting {self.audio_id} -> universe {universe_id}")
        ok, err = await proxy_patch(
            self.proxy_url, self.secret,
            f"/asset/{self.audio_id}/permissions",
            {"subjectId": universe_id},
            self.cookie
        )
        if ok:
            log(f"[verify] {self.audio_id} granted")
            e = discord.Embed(
                title="Audio Verified",
                description=f"**{self.audio_name}** (`{self.audio_id}`) added to your game's dashboard.",
                color=discord.Color.green()
            )
        else:
            log(f"[verify] {self.audio_id} failed: {err}")
            e = discord.Embed(
                title="❌ Verification Failed",
                description=f"`{self.audio_id}` - {err}",
                color=discord.Color.red()
            )
        await interaction.followup.send(embed=e, ephemeral=True)


class _CookieSelect(discord.ui.View):
    """Dropdown to pick which Roblox account to verify with."""
    def __init__(self, profiles: list, audio_id: str, name: str,
                 proxy_url: str, secret: str, universe_ids: list, game_names: dict):
        super().__init__(timeout=60)
        self.audio_id    = audio_id
        self.name        = name
        self.proxy_url   = proxy_url
        self.secret      = secret
        self.universe_ids = universe_ids
        self.game_names   = game_names

        self.profiles = profiles
        options = [
            discord.SelectOption(label=display_name, value=str(i), description=f"@{username}")
            for i, (display_name, username, cookie) in enumerate(profiles)
        ]
        sel          = discord.ui.Select(placeholder="Select Roblox account", options=options)
        sel.callback = self._picked
        self.add_item(sel)

    async def _picked(self, interaction: discord.Interaction):
        chosen_cookie = self.profiles[int(interaction.data["values"][0])][2]
        if len(self.universe_ids) == 1:
            await interaction.response.defer(ephemeral=True)
            ok, err = await proxy_patch(
                self.proxy_url, self.secret,
                f"/asset/{self.audio_id}/permissions",
                {"subjectId": self.universe_ids[0]},
                chosen_cookie
            )
            if ok:
                e = discord.Embed(title="Audio Verified",
                    description=f"**{self.name}** (`{self.audio_id}`) added to your game.",
                    color=discord.Color.green())
            else:
                e = discord.Embed(title="❌ Verification Failed",
                    description=f"`{self.audio_id}` - {err}",
                    color=discord.Color.red())
            await interaction.followup.send(embed=e, ephemeral=True)
        else:
            view = _UniverseSelect(
                self.universe_ids, self.game_names,
                self.audio_id, self.name,
                self.proxy_url, self.secret, chosen_cookie
            )
            await interaction.response.send_message(
                embed=emb(desc="Select which game to add this audio to:"),
                view=view, ephemeral=True
            )


class _VerifyConfirmView(discord.ui.View):
    def __init__(self, audio_id: str, name: str, proxy_url: str,
                 secret: str, cookies: list, universe_ids: list, game_names: dict):
        super().__init__(timeout=60)
        self.audio_id    = audio_id
        self.name        = name
        self.proxy_url   = proxy_url
        self.secret      = secret
        self.cookies     = cookies
        self.cookie      = cookies[0] if cookies else ""
        self.universe_ids = universe_ids
        self.game_names   = game_names

    @discord.ui.button(label="Verify This Audio", style=discord.ButtonStyle.green)
    async def verify(self, interaction: discord.Interaction, _btn):
        # if multiple cookies, ask which account to use first
        if len(self.cookies) > 1:
            await interaction.response.defer(ephemeral=True)
            profiles = []
            for c in self.cookies:
                display_name, username, avatar_url = await fetch_cookie_profile(self.proxy_url, self.secret, c)
                profiles.append((display_name, username, c))
            view = _CookieSelect(
                profiles, self.audio_id, self.name,
                self.proxy_url, self.secret,
                self.universe_ids, self.game_names
            )
            await interaction.followup.send(
                embed=emb(desc=(
                    "Select which Roblox account to verify with:\n\n"
                    "-# Note: These accounts are game administrator accounts and/or holder accounts "
                    "and they should all have edit access to all of our games, so any of them can be used."
                )),
                view=view, ephemeral=True
            )
            return

        # single cookie — proceed straight to universe selection or verify
        if len(self.universe_ids) == 1:
            await interaction.response.defer(ephemeral=True)
            universe_id = self.universe_ids[0]
            log(f"[verify] granting {self.audio_id} -> universe {universe_id}")
            ok, err = await proxy_patch(
                self.proxy_url, self.secret,
                f"/asset/{self.audio_id}/permissions",
                {"subjectId": universe_id},
                self.cookie
            )
            if ok:
                log(f"[verify] {self.audio_id} granted")
                e = discord.Embed(
                    title="Audio Verified",
                    description=f"**{self.name}** (`{self.audio_id}`) added to your game's dashboard.",
                    color=discord.Color.green()
                )
            else:
                log(f"[verify] {self.audio_id} failed: {err}")
                e = discord.Embed(
                    title="❌ Verification Failed",
                    description=f"`{self.audio_id}` - {err}",
                    color=discord.Color.red()
                )
            await interaction.followup.send(embed=e, ephemeral=True)
        else:
            view = _UniverseSelect(
                self.universe_ids, self.game_names,
                self.audio_id, self.name,
                self.proxy_url, self.secret, self.cookie
            )
            await interaction.response.send_message(
                embed=emb(desc="Select which game to add this audio to:"),
                view=view, ephemeral=True
            )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, _btn):
        await interaction.response.send_message(embed=emb("Cancelled."), ephemeral=True)


class _RoleSelect(discord.ui.View):
    def __init__(self, options: list, cmd_name: str, data: dict, idx: int, guild_id: int):
        super().__init__(timeout=300)
        self.cmd_name = cmd_name
        self.data     = data
        self.idx      = idx
        self.guild_id = guild_id
        sel = discord.ui.Select(
            placeholder=f"Roles for /{cmd_name}",
            min_values=1, max_values=min(len(options), 10),
            options=options
        )
        sel.callback = self._picked
        self.add_item(sel)

    async def _picked(self, interaction: discord.Interaction):
        selected = interaction.data["values"]
        cfg      = load_config()
        gid      = str(self.data["guild_id"])
        g        = gcfg(cfg, gid)
        if "command_roles" not in g:
            g["command_roles"] = {}
        g["command_roles"][self.cmd_name] = selected
        set_gcfg(cfg, gid, g)
        log(f"[perms] /{self.cmd_name} -> {selected}")

        uid   = interaction.user.id
        set_state(uid, "perms", self.data, self.idx + 1)
        await interaction.response.send_message(
            embed=emb(desc=f"/{self.cmd_name} permissions saved."), ephemeral=True
        )
        guild = bot.get_guild(self.guild_id)
        if guild:
            await step_perms(interaction.channel, uid, guild, self.data)
        else:
            await interaction.channel.send(embed=emb("❌ Bot not found in that server."))

# ============================================================
#                       MESSAGE HANDLER
# ============================================================

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    uid   = message.author.id
    state = get_state(uid)
    is_dm = isinstance(message.channel, discord.DMChannel)

    if not state or not is_dm:
        await bot.process_commands(message)
        return

    step = state.get("step", "")
    data = state.get("data", {})

    # ── await_invite ──
    if step == "await_invite":
        m = re.search(r"discord(?:\.gg|\.com/invite)/([A-Za-z0-9\-]+)", message.content)
        if not m:
            await message.channel.send(embed=emb("❌ Doesn't look like a Discord invite. Try again."))
            return
        inv, err = await fetch_discord_invite(m.group(1))
        if not inv:
            await message.channel.send(embed=emb(f"❌ Couldn't fetch that invite: `{err}`"))
            return
        gi       = inv.get("guild", {})
        icon_url = (
            f"https://cdn.discordapp.com/icons/{gi.get('id')}/{gi.get('icon')}.png"
            if gi.get("icon") else None
        )
        e = discord.Embed(
            title="Is this your server?",
            description=(
                f"**{gi.get('name', 'Unknown')}**\n"
                f"{inv.get('approximate_member_count', 0):,} members\n"
                f"ID: `{gi.get('id')}`"
            ),
            color=ORANGE
        )
        if icon_url:
            e.set_thumbnail(url=icon_url)
        set_state(uid, "confirm_server", data)
        await message.channel.send(embed=e, view=_ConfirmServerView(gi, data))

    # ── await_universe ──
    elif step == "await_universe":
        raw           = message.content.strip()
        adding_more   = data.get("adding_more_uni", False)

        if adding_more and raw.lower() == "done":
            if data.get("_mode") == "adduniverse":
                save_data_update(data)
                _state.pop(uid, None)
                await message.channel.send(embed=emb(
                    f"✅ Universe IDs updated. {len(data.get('universe_ids', []))} configured."
                ))
            else:
                await message.channel.send(embed=emb(
                    desc=f"Done. {len(data.get('universe_ids', []))} universe ID(s) configured."
                ))
                await step_cookie_warn1(
                    await _make_followup_interaction(message.channel, uid),
                    data
                )
            return

        if not raw.isdigit():
            await message.channel.send(embed=emb("❌ Universe ID must be a number. Try again."))
            return
        game = await fetch_roblox_game(raw)
        if not game:
            await message.channel.send(embed=emb("❌ No game found with that universe ID. Try again."))
            return
        thumb = await fetch_roblox_thumb(raw)
        e = discord.Embed(
            title="Is this your game?",
            description=(
                f"**{game.get('name', 'Unknown')}**\n\n"
                f"{(game.get('description') or 'No description.')[:200]}"
            ),
            color=ORANGE
        )
        if thumb:
            e.set_thumbnail(url=thumb)
        e.add_field(name="Playing", value=f"{game.get('playing', 0):,}", inline=True)
        e.add_field(name="Visits",  value=f"{game.get('visits', 0):,}",  inline=True)
        set_state(uid, "confirm_game", data)
        await message.channel.send(embed=e, view=_ConfirmGameView(game, raw, data))

    # ── await_cookie ──
    elif step == "await_cookie":
        raw          = message.content.strip()
        adding_more  = data.get("adding_more_cookie", False)

        try:
            await message.delete()
        except Exception:
            pass

        if adding_more and raw.lower() == "done":
            if data.get("_mode") == "addcookie":
                save_data_update(data)
                _state.pop(uid, None)
                await message.channel.send(embed=emb(
                    f"✅ Cookies updated. {len(data.get('cookies', []))} configured."
                ))
            else:
                await message.channel.send(embed=emb(
                    desc=f"Done. {len(data.get('cookies', []))} cookie(s) configured."
                ))
                await step_proxy_url(
                    await _make_followup_interaction(message.channel, uid),
                    data
                )
            return

        if len(raw) < 50:
            await message.channel.send(embed=emb("❌ That doesn't look valid. The cookie should be a long string. Try again."))
            return

        await message.channel.send(embed=emb("Checking account..."))
        display_name, username, avatar_url = await validate_cookie_direct(raw)
        if username is None:
            await message.channel.send(embed=emb(
                "❌ That cookie is invalid or expired. Please try again with a fresh `.ROBLOSECURITY` cookie."
            ))
            return

        data.setdefault("cookies", [])
        data["cookies"].append(raw)
        log(f"[setup] cookie #{len(data['cookies'])} received — not stored in log")

        count = len(data["cookies"])
        e = discord.Embed(
            title="✅ Cookie Verified",
            description=(
                f"**{display_name}** (`@{username}`)\n\n"
                f"Cookie #{count} saved. Add another or continue."
            ),
            color=discord.Color.green()
        )
        if avatar_url:
            e.set_thumbnail(url=avatar_url)
        await message.channel.send(embed=e, view=_AddAnotherView("cookie", data))

    # ── await_proxy_url ──
    elif step == "await_proxy_url":
        data["proxy_url"] = message.content.strip().rstrip("/")
        set_state(uid, "await_proxy_secret", data)
        await message.channel.send(embed=emb(
            title="Proxy Secret",
            desc="Reply with your proxy secret - the `PROXY_SECRET` env var on your proxy server."
        ))

    # ── await_proxy_secret ──
    elif step == "await_proxy_secret":
        data["proxy_secret"] = message.content.strip()

        try:
            await message.delete()
        except Exception:
            pass

        if data.get("_mode") == "changeproxy":
            save_data_update(data)
            _state.pop(uid, None)
            log(f"[proxy] updated for guild {data['guild_id']}")
            await message.channel.send(embed=emb("✅ Proxy URL and secret updated."))
            return

        cfg      = load_config()
        guild_id = str(data["guild_id"])
        set_gcfg(cfg, guild_id, {
            "guild_id":       guild_id,
            "guild_name":     data.get("guild_name", ""),
            "universe_ids":   data.get("universe_ids", []),
            "game_names":     data.get("game_names", {}),
            "cookies":        data.get("cookies", []),
            "proxy_url":      data.get("proxy_url", ""),
            "proxy_secret":   data.get("proxy_secret", ""),
            "setup_complete": False,
            "command_roles":  {},
        })
        log(f"[setup] config saved for guild {guild_id}")

        guild = bot.get_guild(int(guild_id))
        if not guild:
            await message.channel.send(embed=emb(
                "Config saved, but the bot is not in that server yet.\n"
                "Invite the bot first, then run `/setupperms` to finish."
            ))
            _state.pop(uid, None)
            return

        unis    = data.get("universe_ids", [])
        cookies = data.get("cookies", [])
        e = discord.Embed(
            title="Core Setup Saved - Setting Permissions",
            description=(
                f"Server: {data.get('guild_name')}\n"
                f"Universe IDs: {len(unis)} configured\n"
                f"Cookies: {len(cookies)} configured\n"
                f"Proxy: `{data.get('proxy_url')}`\n\n"
                "Now select which roles can use each command."
            ),
            color=ORANGE
        )
        await message.channel.send(embed=e)
        set_state(uid, "perms", data, 0)
        await step_perms(message.channel, uid, guild, data)


async def _make_followup_interaction(channel, uid):
    """Stub object that mimics interaction.response.is_done() for DM-only flows."""
    class _FakeInteraction:
        def __init__(self):
            self.user      = type("u", (), {"id": uid})()
            self.channel   = channel
            self.guild_id  = None
            self.guild     = None
            self.response  = type("r", (), {"is_done": lambda s: True, "send_message": None})()
            self.followup  = channel
        async def response_send(self, *a, **kw):
            await channel.send(**kw)
    fi           = _FakeInteraction()
    fi.followup  = type("f", (), {"send": channel.send})()
    return fi


# ============================================================
#                    DM GUILD RESOLUTION
# ============================================================

async def safe_send(interaction: discord.Interaction, **kwargs):
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)

class _GuildPickView(discord.ui.View):
    def __init__(self, matches: list, on_resolved):
        super().__init__(timeout=60)
        self.matches    = matches
        self.on_resolved = on_resolved
        options = [
            discord.SelectOption(label=guild.name, value=str(i))
            for i, (_, guild, _) in enumerate(matches)
        ]
        sel          = discord.ui.Select(placeholder="Select which server", options=options)
        sel.callback = self._picked
        self.add_item(sel)

    async def _picked(self, interaction: discord.Interaction):
        idx             = int(interaction.data["values"][0])
        guild_id, guild, g = self.matches[idx]
        await self.on_resolved(interaction, guild_id, guild, g)

async def resolve_dm_guild(interaction: discord.Interaction, on_resolved):
    cfg     = load_config()
    matches = []
    for guild in bot.guilds:
        member = guild.get_member(interaction.user.id)
        if member:
            g = gcfg(cfg, str(guild.id))
            if g:
                matches.append((str(guild.id), guild, g))
    if not matches:
        await safe_send(interaction,
            embed=emb("❌ No configured servers found. Ask a server admin to run `/setup` first."),
            ephemeral=True)
        return
    if len(matches) == 1:
        await on_resolved(interaction, matches[0][0], matches[0][1], matches[0][2])
        return
    view = _GuildPickView(matches, on_resolved)
    await safe_send(interaction,
        embed=emb("Which server do you want to manage?"),
        view=view, ephemeral=True)

# ============================================================
#                       SLASH COMMANDS
# ============================================================

@bot.tree.command(name="setup", description="Set up AudioVerify Bot for your server")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=False, dms=True, private_channels=True)
async def cmd_setup(interaction: discord.Interaction):
    set_state(interaction.user.id, "welcome")
    await step_welcome(interaction)


@bot.tree.command(name="verify", description="Verify a Roblox audio asset for your game")
@app_commands.describe(audio_id="The Roblox audio asset ID")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def cmd_verify(interaction: discord.Interaction, audio_id: str):
    await interaction.response.defer(ephemeral=True)

    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    if not guild_id:
        await interaction.followup.send(embed=emb("❌ This command must be used inside a server."), ephemeral=True)
        return

    cfg = load_config()
    g   = gcfg(cfg, guild_id)

    if not g.get("setup_complete"):
        await interaction.followup.send(embed=emb("❌ Bot is not set up yet. Run `/setup` first."), ephemeral=True)
        return

    if not has_perm(cfg, guild_id, "verify", interaction.user):
        await interaction.followup.send(embed=emb("❌ You do not have permission to use this command."), ephemeral=True)
        return

    if not audio_id.strip().isdigit():
        await interaction.followup.send(embed=emb("❌ Invalid audio ID - must be a number."), ephemeral=True)
        return

    allowed, reason = check_verify_rate(guild_id, interaction.user.id)
    if not allowed:
        await interaction.followup.send(embed=emb(f"⏳ {reason}"), ephemeral=True)
        return

    proxy_url    = g.get("proxy_url", "")
    proxy_secret = g.get("proxy_secret", "")
    universe_ids = g.get("universe_ids", [])
    game_names   = g.get("game_names", {})
    cookie       = next_cookie(cfg, guild_id)

    if not universe_ids:
        await interaction.followup.send(embed=emb("❌ No universe IDs configured. Run `/adduniverse`."), ephemeral=True)
        return

    sem = get_guild_sem(guild_id)
    async with sem:
        log(f"[verify] fetching asset {audio_id}")
        asset, err = await proxy_get(proxy_url, proxy_secret, f"/asset/{audio_id}", cookie)

        if not asset:
            await interaction.followup.send(
                embed=emb(f"❌ Could not fetch audio info: `{err}`\nCheck the ID and try again."),
                ephemeral=True
            )
            return

        name         = asset.get("Name") or "Unknown"
        description  = (asset.get("Description") or "No description.")[:300]
        creator      = asset.get("Creator", {})
        creator_name = creator.get("Name", "Unknown")
        asset_url    = f"https://www.roblox.com/library/{audio_id}"

        e = discord.Embed(title=name, description=description, url=asset_url, color=ORANGE)
        e.add_field(name="Creator",    value=creator_name,                     inline=True)
        e.add_field(name="Asset ID",   value=f"`{audio_id}`",                  inline=True)
        e.add_field(name="Asset Page", value=f"[View on Roblox]({asset_url})", inline=False)
        e.set_footer(text="Press Verify to add this audio to your game.")

        cookies = g.get("cookies", [cookie] if cookie else [])
        view = _VerifyConfirmView(audio_id, name, proxy_url, proxy_secret, cookies, universe_ids, game_names)
        await interaction.followup.send(embed=e, view=view, ephemeral=True)


@bot.tree.command(name="adduniverse", description="Add another Roblox universe ID")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=False, dms=True, private_channels=True)
async def cmd_adduniverse(interaction: discord.Interaction):
    async def do(interaction, guild_id, guild, g):
        cfg = load_config()
        if not has_perm(cfg, guild_id, "adduniverse", interaction.user):
            await safe_send(interaction, embed=emb("❌ No permission."), ephemeral=True)
            return
        data = {
            "guild_id":     guild_id,
            "guild_name":   g.get("guild_name", ""),
            "universe_ids": g.get("universe_ids", []),
            "game_names":   g.get("game_names", {}),
            "cookies":      g.get("cookies", []),
            "proxy_url":    g.get("proxy_url", ""),
            "proxy_secret": g.get("proxy_secret", ""),
            "_mode":        "adduniverse",
        }
        set_state(interaction.user.id, "await_universe", {**data, "adding_more_uni": True})
        await safe_send(interaction,
            embed=emb(title="Add Universe ID", desc="Send your Roblox Universe ID here in this DM."),
            ephemeral=True)
    await resolve_dm_guild(interaction, do)


@bot.tree.command(name="addcookie", description="Add another Roblox account cookie")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=False, dms=True, private_channels=True)
async def cmd_addcookie(interaction: discord.Interaction):
    async def do(interaction, guild_id, guild, g):
        cfg = load_config()
        if not has_perm(cfg, guild_id, "addcookie", interaction.user):
            await safe_send(interaction, embed=emb("❌ No permission."), ephemeral=True)
            return
        data = {
            "guild_id":     guild_id,
            "guild_name":   g.get("guild_name", ""),
            "universe_ids": g.get("universe_ids", []),
            "game_names":   g.get("game_names", {}),
            "cookies":      g.get("cookies", []),
            "proxy_url":    g.get("proxy_url", ""),
            "proxy_secret": g.get("proxy_secret", ""),
            "_mode":        "addcookie",
        }
        set_state(interaction.user.id, "await_cookie", {**data, "adding_more_cookie": True})
        await safe_send(interaction,
            embed=emb(title="Add Cookie",
                      desc="Send your `.ROBLOSECURITY` cookie here. Your message will be deleted immediately."),
            ephemeral=True)
    await resolve_dm_guild(interaction, do)


@bot.tree.command(name="resetup", description="Restart the full bot setup")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=False, dms=True, private_channels=True)
async def cmd_resetup(interaction: discord.Interaction):
    set_state(interaction.user.id, "welcome")
    await step_welcome(interaction)


@bot.tree.command(name="botlog", description="Get the link to the live bot log")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=False, dms=True, private_channels=True)
async def cmd_botlog(interaction: discord.Interaction):
    async def do(interaction, guild_id, guild, g):
        cfg = load_config()
        if not has_perm(cfg, guild_id, "botlog", interaction.user):
            await safe_send(interaction, embed=emb("❌ No permission."), ephemeral=True)
            return
        render_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/") or f"https://{os.getenv('REPLIT_DEV_DOMAIN', 'localhost')}"
        log_url = f"{render_url}/log"
        e = emb(
            title="Live Bot Log",
            desc=(
                f"**[Click here to view the live log]({log_url})**\n\n"
                "Auto-refreshes every 5 seconds.\n"
                "🟢 success  🟡 warning  🔴 error"
            )
        )
        await safe_send(interaction, embed=e, ephemeral=True)
    await resolve_dm_guild(interaction, do)


@bot.tree.command(name="changeproxy", description="Change the proxy URL and secret")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=False, dms=True, private_channels=True)
async def cmd_changeproxy(interaction: discord.Interaction):
    async def do(interaction, guild_id, guild, g):
        data = {
            "guild_id":     guild_id,
            "guild_name":   g.get("guild_name", ""),
            "universe_ids": g.get("universe_ids", []),
            "game_names":   g.get("game_names", {}),
            "cookies":      g.get("cookies", []),
            "proxy_url":    g.get("proxy_url", ""),
            "proxy_secret": g.get("proxy_secret", ""),
            "_mode":        "changeproxy",
        }
        set_state(interaction.user.id, "await_proxy_url", data)
        await safe_send(interaction,
            embed=emb(title="Change Proxy",
                      desc=f"Send your new proxy URL here.\n\nCurrent: `{g.get('proxy_url') or 'not set'}`"),
            ephemeral=True)
    await resolve_dm_guild(interaction, do)


@bot.tree.command(name="setupperms", description="Re-run command permission setup")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=False, dms=True, private_channels=True)
async def cmd_setupperms(interaction: discord.Interaction):
    async def do(interaction, guild_id, guild, g):
        cfg = load_config()
        if not has_perm(cfg, guild_id, "setupperms", interaction.user):
            await safe_send(interaction, embed=emb("❌ No permission."), ephemeral=True)
            return
        data = {k: g.get(k) for k in ("guild_id", "guild_name", "universe_ids", "game_names")}
        uid  = interaction.user.id
        set_state(uid, "perms", data, 0)
        await safe_send(interaction, embed=emb("Starting permission setup — check your DMs."), ephemeral=True)
        dm = await interaction.user.create_dm()
        await step_perms(dm, uid, guild, data)
    await resolve_dm_guild(interaction, do)

# ============================================================
#                           READY
# ============================================================

@bot.event
async def on_ready():
    log(f"logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        log(f"synced {len(synced)} slash commands globally")
    except Exception as e:
        log(f"sync failed: {e}")

# ============================================================
#                           ENTRY
# ============================================================

keep_alive()
time.sleep(5)
bot.run(TOKEN)
