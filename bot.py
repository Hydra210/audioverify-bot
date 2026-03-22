import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask, render_template_string
from threading import Thread
import os, asyncio, aiohttp, json, re
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
    ("verify",        "Verify a Roblox audio asset"),
    ("resetup",       "Restart the full bot setup"),
    ("changecookie",  "Change the stored Roblox cookie"),
    ("changeuni",     "Change the universe ID"),
    ("botlog",        "View the live bot log"),
    ("setupperms",    "Re-run the command permission setup"),
]

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
<title>AudioVerify — Live Log</title>
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
  {% if "error" in l.lower() or "failed" in l.lower() or "❌" in l %}
    <div class="line err">{{ l }}</div>
  {% elif "warn" in l.lower() or "⚠" in l %}
    <div class="line warn">{{ l }}</div>
  {% elif "✅" in l or "ok" in l.lower() or "success" in l.lower() or "ready" in l.lower() %}
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
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), use_reloader=False)

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

async def fetch_roblox_thumb(universe_id: str) -> str | None:
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

async def proxy_get(proxy_url: str, secret: str, path: str, cookie: str = None):
    headers = {"x-proxy-secret": secret}
    if cookie:
        headers["x-cookie-override"] = cookie
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(proxy_url + path, headers=headers,
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                log(f"[proxy] GET {path} → {r.status}")
                if r.status == 200:
                    return await r.json(), None
                return None, f"HTTP {r.status}"
    except Exception as e:
        log(f"[proxy] GET error: {e}")
        return None, str(e)

async def proxy_patch(proxy_url: str, secret: str, path: str, body: dict, cookie: str = None):
    headers = {"Content-Type": "application/json", "x-proxy-secret": secret}
    if cookie:
        headers["x-cookie-override"] = cookie
    try:
        async with aiohttp.ClientSession() as s:
            async with s.patch(proxy_url + path, headers=headers, json=body,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
                log(f"[proxy] PATCH {path} → {r.status}")
                if r.status == 200:
                    return True, None
                data = await r.json()
                return False, data.get("message", f"HTTP {r.status}")
    except Exception as e:
        log(f"[proxy] PATCH error: {e}")
        return False, str(e)

# ============================================================
#                       SETUP STATE
# ============================================================

_state: dict = {}  # user_id -> { step, data, perm_index }

def get_state(uid: int) -> dict:
    return _state.get(uid, {})

def set_state(uid: int, step: str, data: dict = None, perm_index: int = 0):
    _state[uid] = {"step": step, "data": data or {}, "perm_index": perm_index}

# ============================================================
#                       SETUP STEPS
# ============================================================

async def step_welcome(interaction: discord.Interaction):
    e = discord.Embed(
        title="Welcome to ErrorAudioBot",
        description=(
            "This bot lets your Discord members verify Roblox audio assets "
            "directly from your server — adding your game as a collaborator "
            "on any audio in seconds.\n\n"
            "**What we'll set up:**\n"
            "▸ Your Discord server\n"
            "▸ Your Roblox game universe ID\n"
            "▸ Your Roblox account cookie (API auth)\n"
            "▸ Your proxy server details\n"
            "▸ Command permissions per role\n\n"
            "Everything runs in DMs for your privacy."
        ),
        color=ORANGE
    )
    set_state(interaction.user.id, "welcome")
    await interaction.response.send_message(embed=e, view=_ContinueBtn("invite"), ephemeral=True)


async def step_invite(interaction: discord.Interaction, data: dict):
    set_state(interaction.user.id, "await_invite", data)
    e = emb(
        title="Step 1 — Your Discord Server",
        desc=(
            "Reply to this message with your **server invite link**.\n"
            "Example: `https://discord.gg/yourcode`"
        )
    )
    if interaction.response.is_done():
        await interaction.followup.send(embed=e, ephemeral=True)
    else:
        await interaction.response.send_message(embed=e, ephemeral=True)


async def step_universe(interaction: discord.Interaction, data: dict):
    set_state(interaction.user.id, "await_universe", data)
    e = emb(
        title="Step 2 — Roblox Universe ID",
        desc=(
            "Reply to this message with your **Roblox Universe ID**.\n\n"
            "Find it in Roblox Studio → Game Settings → Basic Info, "
            "or from your game's URL on the Roblox website."
        )
    )
    if interaction.response.is_done():
        await interaction.followup.send(embed=e, ephemeral=True)
    else:
        await interaction.response.send_message(embed=e, ephemeral=True)


async def step_cookie_warn1(interaction: discord.Interaction, data: dict):
    set_state(interaction.user.id, "cookie_warn1", data)
    e = discord.Embed(
        title="⚠️ Security Warning — Read This",
        description=(
            "The next step requires your **`.ROBLOSECURITY` cookie**.\n\n"
            "This is the authentication key for your Roblox account. "
            "Anyone who has it can access your account.\n\n"
            "**Risks:**\n"
            "▸ A compromised cookie = compromised Roblox account\n"
            "▸ Roblox may flag logins from unexpected IPs\n"
            "▸ You should use a **dedicated alt account** with only the permissions needed — not your main\n\n"
            "**What we do with it:**\n"
            "▸ Stored in `bot_config.json` on your Render instance only\n"
            "▸ **Not logged anywhere** in Render output or any log\n"
            "▸ Only used to authenticate Roblox Open Cloud API calls via your proxy\n\n"
            "Press **I Understand, Continue** to proceed."
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
        title="⚠️ Final Warning",
        description=(
            "**Are you absolutely sure?**\n\n"
            "You are about to send your `.ROBLOSECURITY` cookie.\n\n"
            "We **strongly recommend** a dedicated Roblox alt account "
            "— not your main — that only has access to the groups and assets you need.\n\n"
            "You can reset the cookie anytime with `/changecookie`.\n\n"
            "Press **Yes, I Accept the Risks** to continue."
        ),
        color=discord.Color.from_rgb(220, 40, 40)
    )
    if interaction.response.is_done():
        await interaction.followup.send(embed=e, view=_CookieWarn2Btn(), ephemeral=True)
    else:
        await interaction.response.send_message(embed=e, view=_CookieWarn2Btn(), ephemeral=True)


async def step_cookie_input(interaction: discord.Interaction, data: dict):
    set_state(interaction.user.id, "await_cookie", data)
    e = emb(
        title="Step 3 — Enter Your Cookie",
        desc=(
            "Reply to this message with your **`.ROBLOSECURITY` cookie**.\n\n"
            "To find it:\n"
            "1. Open Roblox in your browser\n"
            "2. Open DevTools (F12) → Application → Cookies → roblox.com\n"
            "3. Copy the value of `.ROBLOSECURITY`\n\n"
            "Your message will be deleted immediately after the bot reads it."
        )
    )
    if interaction.response.is_done():
        await interaction.followup.send(embed=e, ephemeral=True)
    else:
        await interaction.response.send_message(embed=e, ephemeral=True)


async def step_proxy_url(interaction: discord.Interaction, data: dict):
    set_state(interaction.user.id, "await_proxy_url", data)
    e = emb(
        title="Step 4 — Proxy Server URL",
        desc=(
            "Reply with your **proxy server URL** (the Render URL for your server.js).\n"
            "Example: `https://your-proxy.onrender.com`"
        )
    )
    if interaction.response.is_done():
        await interaction.followup.send(embed=e, ephemeral=True)
    else:
        await interaction.response.send_message(embed=e, ephemeral=True)


async def step_proxy_secret(interaction: discord.Interaction, data: dict):
    set_state(interaction.user.id, "await_proxy_secret", data)
    e = emb(
        title="Step 4 continued — Proxy Secret",
        desc="Reply with your **proxy secret** — the `PROXY_SECRET` env var on your proxy server."
    )
    if interaction.response.is_done():
        await interaction.followup.send(embed=e, ephemeral=True)
    else:
        await interaction.response.send_message(embed=e, ephemeral=True)


async def step_perms(channel: discord.DMChannel, uid: int, guild: discord.Guild, data: dict):
    state    = get_state(uid)
    idx      = state.get("perm_index", 0)

    if idx >= len(COMMANDS_WITH_PERMS):
        cfg      = load_config()
        guild_id = str(data["guild_id"])
        g        = gcfg(cfg, guild_id)
        g["setup_complete"] = True
        set_gcfg(cfg, guild_id, g)
        log(f"[setup] complete for guild {guild_id}")
        e = discord.Embed(
            title="Setup Complete!",
            description=(
                "AudioVerify Bot is ready.\n\n"
                "**Commands:**\n"
                + "\n".join(f"▸ `/{n}` — {d}" for n, d in COMMANDS_WITH_PERMS)
                + "\n\n"
                "Use `/botlog` to view the live log."
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
        options.append(discord.SelectOption(
            label=f"@{r.name}", value=str(r.id), description=f"ID: {r.id}"
        ))
        if len(options) >= 25:
            break

    e = emb(
        title=f"Permissions — `/{cmd_name}`",
        desc=(
            f"**Description:** {cmd_desc}\n\n"
            "Select the role(s) that can use this command.\n"
            "Pick `@everyone` to allow all members."
        )
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

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.green)
    async def go(self, interaction: discord.Interaction, _btn):
        state = get_state(interaction.user.id)
        data  = state.get("data", {})
        dispatch = {
            "invite":    lambda: step_invite(interaction, data),
        }
        fn = dispatch.get(self.next_step)
        if fn:
            await fn()
        else:
            await interaction.response.send_message(embed=emb("❌ Unknown step."), ephemeral=True)


class _ConfirmServerView(discord.ui.View):
    def __init__(self, guild_info: dict, data: dict):
        super().__init__(timeout=120)
        self.guild_info = guild_info
        self.data       = data

    @discord.ui.button(label="Yes, that's my server", style=discord.ButtonStyle.green)
    async def yes(self, interaction: discord.Interaction, _btn):
        self.data["guild_id"]   = self.guild_info["id"]
        self.data["guild_name"] = self.guild_info["name"]
        await step_universe(interaction, self.data)

    @discord.ui.button(label="❌ No, re-enter", style=discord.ButtonStyle.red)
    async def no(self, interaction: discord.Interaction, _btn):
        await step_invite(interaction, self.data)


class _ConfirmGameView(discord.ui.View):
    def __init__(self, game: dict, universe_id: str, data: dict):
        super().__init__(timeout=120)
        self.game        = game
        self.universe_id = universe_id
        self.data        = data

    @discord.ui.button(label="Yes, that's my game", style=discord.ButtonStyle.green)
    async def yes(self, interaction: discord.Interaction, _btn):
        self.data["universe_id"] = self.universe_id
        self.data["game_name"]   = self.game.get("name", "Unknown")
        await step_cookie_warn1(interaction, self.data)

    @discord.ui.button(label="❌ No, re-enter", style=discord.ButtonStyle.red)
    async def no(self, interaction: discord.Interaction, _btn):
        await step_universe(interaction, self.data)


class _CookieWarn1Btn(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="I Understand, Continue →", style=discord.ButtonStyle.grey)
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

    @discord.ui.button(label="Yes, I Accept the Risks →", style=discord.ButtonStyle.grey)
    async def go(self, interaction: discord.Interaction, _btn):
        state = get_state(interaction.user.id)
        await step_cookie_input(interaction, state.get("data", {}))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, _btn):
        _state.pop(interaction.user.id, None)
        await interaction.response.send_message(embed=emb("Setup cancelled."), ephemeral=True)


class _VerifyConfirmView(discord.ui.View):
    def __init__(self, audio_id: str, name: str, proxy_url: str,
                 secret: str, cookie: str, universe_id: str):
        super().__init__(timeout=60)
        self.audio_id    = audio_id
        self.name        = name
        self.proxy_url   = proxy_url
        self.secret      = secret
        self.cookie      = cookie
        self.universe_id = universe_id

    @discord.ui.button(label=" Verify This Audio", style=discord.ButtonStyle.green)
    async def verify(self, interaction: discord.Interaction, _btn):
        await interaction.response.defer(ephemeral=True)
        log(f"[verify] granting {self.audio_id} → universe {self.universe_id}")
        ok, err = await proxy_patch(
            self.proxy_url, self.secret,
            f"/asset/{self.audio_id}/permissions",
            {"subjectId": self.universe_id},
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
            log(f"[verify] ❌ {self.audio_id}: {err}")
            e = discord.Embed(
                title="❌ Verification Failed",
                description=f"`{self.audio_id}` — {err}",
                color=discord.Color.red()
            )
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
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
            min_values=1,
            max_values=min(len(options), 10),
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
        log(f"[perms] /{self.cmd_name} → {selected}")

        uid   = interaction.user.id
        state = get_state(uid)
        set_state(uid, "perms", self.data, self.idx + 1)

        await interaction.response.send_message(
            embed=emb(f" `/{self.cmd_name}` permissions saved."), ephemeral=True
        )

        guild = bot.get_guild(self.guild_id)
        if guild:
            await step_perms(interaction.channel, uid, guild, self.data)
        else:
            await interaction.channel.send(embed=emb("❌ Bot not found in that server. Make sure it's been invited."))


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
                f"👥 {inv.get('approximate_member_count', 0):,} members\n"
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
        raw = message.content.strip()
        if not raw.isdigit():
            await message.channel.send(embed=emb("❌ Universe ID must be a number. Try again."))
            return
        game = await fetch_roblox_game(raw)
        if not game:
            await message.channel.send(embed=emb("❌ No game found with that universe ID. Check and try again."))
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
        cookie = message.content.strip()
        try:
            await message.delete()
        except Exception:
            pass
        if len(cookie) < 50:
            await message.channel.send(embed=emb("❌ That doesn't look valid — the cookie should be a long string. Try again."))
            return
        data["cookie"] = cookie
        log(f"[setup] cookie received for guild {data.get('guild_id', '?')} — not stored in log")
        set_state(uid, "await_proxy_url", data)
        await message.channel.send(embed=emb(
            title="Cookie saved — Step 4: Proxy Server",
            desc=(
                "Reply with your **proxy server URL** "
                "(the Render URL where your `server.js` is deployed).\n"
                "Example: `https://your-proxy.onrender.com`"
            )
        ))

    # ── await_proxy_url ──
    elif step == "await_proxy_url":
        data["proxy_url"] = message.content.strip().rstrip("/")
        set_state(uid, "await_proxy_secret", data)
        await message.channel.send(embed=emb(
            title="Step 4 continued — Proxy Secret",
            desc="Reply with your **proxy secret** — the `PROXY_SECRET` env var on your proxy server."
        ))

    # ── await_proxy_secret ──
    elif step == "await_proxy_secret":
        data["proxy_secret"] = message.content.strip()

        # Ping proxy
        pong, perr = await proxy_get(data["proxy_url"], data["proxy_secret"], "/ping")
        if perr and "ok" not in str(pong):
            log(f"[setup] proxy ping failed: {perr} — continuing anyway")

        # Save config
        cfg      = load_config()
        guild_id = str(data["guild_id"])
        set_gcfg(cfg, guild_id, {
            "guild_id":       guild_id,
            "guild_name":     data.get("guild_name", ""),
            "universe_id":    data.get("universe_id", ""),
            "game_name":      data.get("game_name", ""),
            "cookie":         data.get("cookie", ""),
            "proxy_url":      data.get("proxy_url", ""),
            "proxy_secret":   data.get("proxy_secret", ""),
            "setup_complete": False,
            "command_roles":  {},
        })
        log(f"[setup] config saved for guild {guild_id}")

        guild = bot.get_guild(int(guild_id))
        if not guild:
            await message.channel.send(embed=emb(
                "Core config saved, but the bot isn't in that server yet.\n"
                "Invite the bot to your server first, then run `/setupperms` to finish."
            ))
            _state.pop(uid, None)
            return

        e = discord.Embed(
            title="Core Setup Saved — Now Setting Permissions",
            description=(
                f"**Server:** {data.get('guild_name')}\n"
                f"**Game:** {data.get('game_name')}\n"
                f"**Universe ID:** `{data.get('universe_id')}`\n"
                f"**Proxy:** `{data.get('proxy_url')}`\n\n"
                "Now select which roles can use each command."
            ),
            color=ORANGE
        )
        await message.channel.send(embed=e)
        set_state(uid, "perms", data, 0)
        await step_perms(message.channel, uid, guild, data)


# ============================================================
#                       SLASH COMMANDS
# ============================================================

@bot.tree.command(name="setup", description="Set up AudioVerify Bot for your server")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def cmd_setup(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.DMChannel):
        e = discord.Embed(
            title="Setup must be done in DMs",
            description=(
                "For your privacy, setup only runs in a **Direct Message** with the bot.\n\n"
                "Click the bot's profile → **Message** → then run `/setup` again there."
            ),
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=e, ephemeral=True)
        return
    set_state(interaction.user.id, "welcome")
    await step_welcome(interaction)


@bot.tree.command(name="verify", description="Verify a Roblox audio asset for your game")
@app_commands.describe(audio_id="The Roblox audio asset ID")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def cmd_verify(interaction: discord.Interaction, audio_id: str):
    await interaction.response.defer(ephemeral=True)

    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    if not guild_id:
        await interaction.followup.send(embed=emb("❌ This command must be used inside a server."), ephemeral=True)
        return

    cfg  = load_config()
    g    = gcfg(cfg, guild_id)

    if not g.get("setup_complete"):
        await interaction.followup.send(embed=emb("❌ Bot isn't set up yet — run `/setup` first."), ephemeral=True)
        return

    if not has_perm(cfg, guild_id, "verify", interaction.user):
        await interaction.followup.send(embed=emb("❌ You don't have permission to use this command."), ephemeral=True)
        return

    if not audio_id.strip().isdigit():
        await interaction.followup.send(embed=emb("❌ Invalid audio ID — must be a number."), ephemeral=True)
        return

    proxy_url    = g.get("proxy_url", "")
    proxy_secret = g.get("proxy_secret", "")
    cookie       = g.get("cookie", "")
    universe_id  = g.get("universe_id", "")

    log(f"[verify] fetching asset {audio_id}")
    asset, err = await proxy_get(proxy_url, proxy_secret, f"/asset/{audio_id}", cookie)

    if not asset:
        await interaction.followup.send(
            embed=emb(f"❌ Couldn't fetch audio info: `{err}`\nCheck the ID and try again."),
            ephemeral=True
        )
        return

    name         = asset.get("Name") or "Unknown"
    description  = (asset.get("Description") or "No description.")[:300]
    creator      = asset.get("Creator", {})
    creator_name = creator.get("Name", "Unknown")
    asset_url    = f"https://www.roblox.com/library/{audio_id}"

    e = discord.Embed(
        title=f" {name}",
        description=description,
        url=asset_url,
        color=ORANGE
    )
    e.add_field(name="Creator",    value=creator_name,                        inline=True)
    e.add_field(name="Asset ID",   value=f"`{audio_id}`",                     inline=True)
    e.add_field(name="Asset Page", value=f"[View on Roblox]({asset_url})",    inline=False)
    e.set_footer(text="Press Verify to add this audio to your game's dashboard.")

    view = _VerifyConfirmView(audio_id, name, proxy_url, proxy_secret, cookie, universe_id)
    await interaction.followup.send(embed=e, view=view, ephemeral=True)


@bot.tree.command(name="resetup", description="Restart the full bot setup")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def cmd_resetup(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.DMChannel):
        await interaction.response.send_message(
            embed=emb("Run this command in a DM with the bot.", color=discord.Color.red()),
            ephemeral=True
        )
        return
    set_state(interaction.user.id, "welcome")
    await step_welcome(interaction)


@bot.tree.command(name="changecookie", description="Change your stored Roblox cookie")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def cmd_changecookie(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    if not guild_id:
        await interaction.response.send_message(embed=emb("❌ Must be used in a server."), ephemeral=True)
        return
    cfg = load_config()
    g   = gcfg(cfg, guild_id)
    if not g:
        await interaction.response.send_message(embed=emb("❌ Run `/setup` first."), ephemeral=True)
        return
    if not has_perm(cfg, guild_id, "changecookie", interaction.user):
        await interaction.response.send_message(embed=emb("❌ No permission."), ephemeral=True)
        return
    if not isinstance(interaction.channel, discord.DMChannel):
        await interaction.response.send_message(
            embed=emb("Please run this in a DM with the bot for your security."), ephemeral=True
        )
        return
    data = {k: g.get(k, "") for k in ("guild_id", "guild_name", "universe_id", "game_name", "proxy_url", "proxy_secret")}
    set_state(interaction.user.id, "cookie_warn1", data)
    await step_cookie_warn1(interaction, data)


@bot.tree.command(name="cc", description="Alias for /changecookie — change your stored Roblox cookie")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def cmd_cc(interaction: discord.Interaction):
    await cmd_changecookie.callback(interaction)


@bot.tree.command(name="changeuni", description="Change the universe ID")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def cmd_changeuni(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    if not guild_id:
        await interaction.response.send_message(embed=emb("❌ Must be used in a server."), ephemeral=True)
        return
    cfg = load_config()
    g   = gcfg(cfg, guild_id)
    if not g:
        await interaction.response.send_message(embed=emb("❌ Run `/setup` first."), ephemeral=True)
        return
    if not has_perm(cfg, guild_id, "changeuni", interaction.user):
        await interaction.response.send_message(embed=emb("❌ No permission."), ephemeral=True)
        return
    if not isinstance(interaction.channel, discord.DMChannel):
        await interaction.response.send_message(
            embed=emb("Run this in a DM with the bot."), ephemeral=True
        )
        return
    data = {k: g.get(k, "") for k in ("guild_id", "guild_name", "cookie", "proxy_url", "proxy_secret")}
    set_state(interaction.user.id, "await_universe", data)
    await step_universe(interaction, data)


@bot.tree.command(name="cu", description="Alias for /changeuni — change the universe ID")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def cmd_cu(interaction: discord.Interaction):
    await cmd_changeuni.callback(interaction)


@bot.tree.command(name="botlog", description="Get the link to view the live bot log")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def cmd_botlog(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    if guild_id:
        cfg = load_config()
        if not has_perm(cfg, guild_id, "botlog", interaction.user):
            await interaction.response.send_message(embed=emb("❌ No permission."), ephemeral=True)
            return
    render_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not render_url:
        render_url = "https://your-bot.onrender.com"
    log_url = f"{render_url}/log"
    e = emb(
        title="Live Bot Log",
        desc=(
            f"**[Click here to view the live log]({log_url})**\n\n"
            "Auto-refreshes every 5 seconds.\n"
            "🟢 success  🟡 warning  🔴 error"
        )
    )
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="setupperms", description="Re-run command permission setup")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def cmd_setupperms(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id) if interaction.guild_id else None
    if not guild_id:
        await interaction.response.send_message(embed=emb("❌ Must be used in a server."), ephemeral=True)
        return
    cfg = load_config()
    g   = gcfg(cfg, guild_id)
    if not g:
        await interaction.response.send_message(embed=emb("❌ Run `/setup` first."), ephemeral=True)
        return
    if not has_perm(cfg, guild_id, "setupperms", interaction.user):
        await interaction.response.send_message(embed=emb("❌ No permission."), ephemeral=True)
        return

    guild = interaction.guild
    if not guild:
        await interaction.response.send_message(embed=emb("❌ Bot not found in server."), ephemeral=True)
        return

    data = {k: g.get(k, "") for k in ("guild_id", "guild_name", "universe_id", "game_name")}
    uid  = interaction.user.id
    set_state(uid, "perms", data, 0)

    await interaction.response.send_message(
        embed=emb("Starting permission setup — check your DMs."), ephemeral=True
    )
    dm = await interaction.user.create_dm()
    await step_perms(dm, uid, guild, data)


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
bot.run(TOKEN)
