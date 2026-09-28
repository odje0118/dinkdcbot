import os
import re
import sqlite3
import asyncio
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DROPS_CHANNEL_ID = 1540706808262430792
DEATHS_CHANNEL_ID = 1540800494547640420
LEADERBOARD_CHANNEL_ID = 1553383319696048208
WEEKLY_LOOT_ANNOUNCEMENT_CHANNEL_ID = 1553886530823524424
WEEKLY_LOOT_WINNER_ROLE_NAME = "Weekly Loot Winner"

LOOT_MILESTONES = [
    10_000_000,
    25_000_000,
    50_000_000,
    100_000_000,
    250_000_000,
    500_000_000,
    1_000_000_000,
    2_500_000_000,
    5_000_000_000,
    10_000_000_000,
]

PVP_KILL_MILESTONES = [1, 5, 10, 25, 50, 100, 250, 500, 1_000]
BIG_DROP_ANNOUNCEMENT_MIN_GP = 10_000_000

DB_FILE = os.getenv("DB_FILE", "leaderboard.db")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
db_lock = asyncio.Lock()
update_lock = asyncio.Lock()
leaderboard_view = None


def _log_interaction_readable(interaction, action, values=None, player=None):
    """Read-only interaction logging; does not alter interaction behavior."""
    try:
        user = interaction.user
        guild = interaction.guild.name if interaction.guild else "DM"
        channel = getattr(interaction.channel, "name", None) or "DM"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        print("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print("🔘 BUTTON / MENU USED")
        print(f"👤 User: {user} (ID: {user.id})")
        print(f"🎯 Action: {action}")
        if player:
            print(f"📋 Player: {player}")
        if values:
            print(f"🔹 Selection: {', '.join(map(str, values))}")
        print(f"📍 Server: {guild}")
        print(f"💬 Channel: #{channel}")
        print(f"🕐 Time: {timestamp}")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
    except Exception as e:
        print(f"[INTERACTION LOG ERROR] {e}")
def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            message_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            player TEXT NOT NULL,
            value_gp INTEGER DEFAULT 0,
            completion_count INTEGER DEFAULT 0,
            source TEXT DEFAULT '',
            loot_item TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_events_player ON events(player);
        CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS player_aliases (
            old_key TEXT PRIMARY KEY,
            current_name TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS player_aliases (
            old_key TEXT PRIMARY KEY,
            current_name TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS weekly_loot_wins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            week_monday TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS player_discord_links (
            player_key TEXT PRIMARY KEY,
            player_name TEXT NOT NULL,
            discord_id INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS milestone_announcements (
            player_key TEXT NOT NULL,
            milestone_type TEXT NOT NULL,
            threshold INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (player_key, milestone_type, threshold)
        );
    """)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
    if "loot_item" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN loot_item TEXT DEFAULT ''")
    conn.commit()
    conn.close()


def parse_gp(text: str) -> int:
    """Convert 78.8K, 1.08M, 4.99M, 825293 etc. into GP."""
    if not text:
        return 0

    cleaned = text.upper().replace(",", "").replace("GP", "").strip()
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*([KMB])?", cleaned)
    if not match:
        return 0

    number = float(match.group(1))
    suffix = match.group(2)

    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1)
    return int(number * multiplier)


def format_gp(value: int) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,}"


def player_key(name: str) -> str:
    """Case/space-insensitive key used only for grouping player names."""
    if not name:
        return ""
    return re.sub(r"\s+", "", name).casefold()


def display_player_name(name: str) -> str:
    """Preserve the exact spacing used by Dink in its notification."""
    return (name or "").strip()


def resolve_player_name(name: str) -> str:
    """Return the current canonical player name for a Dink username."""
    name = display_player_name(name)
    if not name:
        return name

    conn = db()
    current = name
    seen = set()

    # Follow aliases so chained name changes keep working.
    while True:
        key = player_key(current)
        if not key or key in seen:
            break
        seen.add(key)

        row = conn.execute(
            "SELECT current_name FROM player_aliases WHERE old_key = ?",
            (key,),
        ).fetchone()
        if not row:
            break
        current = row["current_name"]

    conn.close()
    return display_player_name(current)


def get_embed_text(embed: discord.Embed) -> str:
    parts = [
        embed.title or "",
        embed.description or "",
    ]
    for field in embed.fields:
        parts.append(field.name or "")
        parts.append(field.value or "")
    return "\n".join(parts)


def parse_loot_item(embed: discord.Embed) -> str:
    """Extract item names from Dink's %LOOT% content."""
    candidates = []

    for field in embed.fields:
        name = (field.name or "").strip().casefold()
        value = (field.value or "").strip()
        if name in {"loot", "items", "drop", "drops", "loot items"} and value:
            candidates.append(value)

    # Older Dink embeds put the actual loot line directly in the description.
    if embed.description:
        candidates.append(embed.description)

    for value in candidates:
        items = []

        for raw in value.splitlines():
            line = re.sub(r"[*_`]", "", raw.strip()).strip()
            if not line:
                continue

            # Skip Dink metadata and notification text.
            if re.match(
                r"^(?:total\s+value|completion\s+count|(?:van|from))\s*:",
                line,
                re.I,
            ):
                continue
            if re.search(
                r"\b(?:heeft een klapper geslagen|got a drop|has looted)\b",
                line,
                re.I,
            ):
                continue

            # Actual Dink format, e.g.:
            # "1 x Ancient totem (997K)"
            # "58 x Runite ore (585K)"
            m = re.match(
                r"^\d+\s*[x×]\s*(.+?)\s*\(\s*[\d.,]+\s*[KMB]?\s*(?:GP)?\s*\)\s*$",
                line,
                re.I,
            )
            if m:
                item_name = m.group(1).strip()
                if item_name:
                    items.append(item_name)
                    continue

            # Other possible formats:
            # "Ancient totem — 997K GP"
            # "Ancient totem (997K)"
            line = re.sub(
                r"\s*[—-]\s*[\d.,]+\s*[KMB]?\s*GP\s*$",
                "",
                line,
                flags=re.I,
            )
            line = re.sub(
                r"\s*\(\s*[\d.,]+\s*[KMB]?\s*(?:GP)?\s*\)\s*$",
                "",
                line,
                flags=re.I,
            )
            line = re.sub(
                r"\s+\(?[\d.,]+\s*[KMB]?\s*GP\)?\s*$",
                "",
                line,
                flags=re.I,
            )
            line = re.sub(r"^\d+\s*[x×]\s*", "", line, flags=re.I).strip()

            if line:
                items.append(line)

        if items:
            return ", ".join(items)

    return ""



def parse_pvp_kill(message: discord.Message):
    """Parse Dink Player Kill notifications posted in the drops channel."""
    if not message.embeds:
        return None

    embed = message.embeds[0]
    text = get_embed_text(embed)
    if "Player Kill" not in text:
        return None

    description = (embed.description or "").strip()
    player = None

    # Common Dink wording: "Player has ... killed/gePK'd ..."
    if description:
        first_line = next((x.strip() for x in description.splitlines() if x.strip()), "")
        m = re.match(
            r"^(.+?)\s+(?:has|heeft|left|killed|PK'd|gePK'd)\b",
            first_line,
            re.I,
        )
        if m:
            player = m.group(1).strip()

        # Fallback for wording where the killer is followed by "gePK'd".
        if not player:
            m = re.match(r"^(.+?)\s+.*?\bgePK'd\b", first_line, re.I)
            if m:
                player = m.group(1).strip()

    if not player and embed.author and embed.author.name:
        player = embed.author.name.strip()

    if not player:
        return None

    return {
        "event_type": "pvp_kill",
        "player": player,
        "value_gp": 0,
        "completion_count": 0,
        "source": "Player Kill",
        "loot_item": "",
    }


def parse_loot(message: discord.Message):
    if not message.embeds:
        return None

    embed = message.embeds[0]
    text = get_embed_text(embed)

    if "Loot Drop" not in text:
        return None

    # Dink format: "POLM heeft een klapper geslagen!"
    player = None
    if embed.description:
        m = re.search(r"^(.+?)\s+heeft een klapper geslagen!", embed.description, re.I | re.M)
        if m:
            player = m.group(1).strip()

    if not player:
        # English fallback
        if embed.description:
            m = re.search(r"^(.+?)\s+got a drop", embed.description, re.I | re.M)
            if m:
                player = m.group(1).strip()

    if not player:
        # The embed author/title area can sometimes contain the player.
        if embed.author and embed.author.name:
            player = embed.author.name.strip()

    if not player:
        return None

    completion_count = 0
    total_value = 0
    source = ""
    loot_item = parse_loot_item(embed)

    for field in embed.fields:
        name = (field.name or "").strip().lower()
        value = (field.value or "").strip()

        if name == "completion count":
            m = re.search(r"\d+", value.replace(",", ""))
            if m:
                completion_count = int(m.group())
        elif name == "total value":
            total_value = parse_gp(value)

    # Dink has used both "Van:" and "From:" over time. Search the
    # complete embed text because the label may be outside the description.
    source_text = get_embed_text(embed)
    source_match = re.search(
        r"(?:^|[\r\n])\s*(?:Van|From)\s*:\s*([^\r\n]+)",
        source_text,
        re.I,
    )
    if source_match:
        source = source_match.group(1).strip()

    return {
        "event_type": "loot",
        "player": player,
        "value_gp": total_value,
        "completion_count": completion_count,
        "source": source,
        "loot_item": loot_item,
    }


def parse_death(message: discord.Message):
    if not message.embeds:
        return None

    embed = message.embeds[0]
    text = get_embed_text(embed)

    if "Player Death" not in text:
        return None

    description = embed.description or ""
    player = None
    value_gp = 0

    # Screenshot example:
    # "Gim_rody has just been PKed by Chiraqching for 78.8K gp..."
    m = re.search(
        r"^(.+?)\s+has just been PKed.*?for\s+([\d.,]+\s*[KMB]?)\s*gp",
        description,
        re.I | re.M,
    )
    if m:
        player = m.group(1).strip()
        value_gp = parse_gp(m.group(2))

    if not player:
        # Dink's other death wording:
        # "POLM is gaan liggen.. kleine jongen!"
        m = re.search(r"^(.+?)\s+is gaan liggen", description, re.I | re.M)
        if m:
            player = m.group(1).strip()

    if not player:
        # Generic fallback: first non-empty line
        lines = [x.strip() for x in description.splitlines() if x.strip()]
        if lines:
            first = lines[0]
            # Avoid treating generic text as a player.
            if "has just" not in first.lower() and "is " not in first.lower():
                player = first

    if not player:
        if embed.author and embed.author.name:
            player = embed.author.name.strip()

    if not player:
        return None

    return {
        "event_type": "death",
        "player": player,
        "value_gp": value_gp,
        "completion_count": 0,
        "source": "",
    }


def resolve_player_alias(name: str) -> str:
    """Resolve an OSRS username through the stored name-change aliases."""
    current = display_player_name(name)
    if not current:
        return current

    conn = db()
    try:
        seen = set()
        for _ in range(20):
            key = player_key(current)
            if not key or key in seen:
                break
            seen.add(key)

            row = conn.execute(
                "SELECT current_name FROM player_aliases WHERE old_key = ?",
                (key,),
            ).fetchone()
            if not row:
                break

            next_name = display_player_name(row["current_name"])
            if not next_name or player_key(next_name) == key:
                break
            current = next_name
        return current
    finally:
        conn.close()


async def save_event(message: discord.Message, parsed: dict) -> bool:
    """Insert a Dink event or repair an existing event."""
    # Dink will continue reporting the new/old OSRS name independently of
    # the leaderboard profile. Resolve aliases before storing the event.
    parsed = dict(parsed)
    parsed["player"] = resolve_player_name(parsed["player"])

    parsed = dict(parsed)
    parsed["player"] = resolve_player_alias(parsed["player"])

    async with db_lock:
        conn = db()
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO events
            (message_id, channel_id, event_type, player, value_gp,
             completion_count, source, loot_item, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (message.id, message.channel.id, parsed["event_type"],
             parsed["player"], parsed["value_gp"],
             parsed["completion_count"], parsed.get("source", ""),
             parsed.get("loot_item", ""), message.created_at.isoformat()),
        )
        inserted = cur.rowcount == 1
        changed = False

        if not inserted:
            # Re-process the complete parsed event. This repairs older rows and
            # also handles Dink notifications that were edited after posting.
            cur = conn.execute(
                """
                UPDATE events
                SET player = ?, value_gp = ?, completion_count = ?,
                    source = ?, loot_item = ?
                WHERE message_id = ? AND channel_id = ? AND event_type = ?
                """,
                (parsed["player"], parsed["value_gp"],
                 parsed["completion_count"], parsed.get("source", ""),
                 parsed.get("loot_item", ""), message.id,
                 message.channel.id, parsed["event_type"]),
            )
            changed = cur.rowcount > 0

        conn.commit()
        conn.close()
    return inserted or changed


async def process_message(message: discord.Message) -> bool:
    if message.channel.id == DROPS_CHANNEL_ID:
        parsed = parse_pvp_kill(message)
        if not parsed:
            parsed = parse_loot(message)
    elif message.channel.id == DEATHS_CHANNEL_ID:
        parsed = parse_death(message)
    else:
        return False

    if not parsed:
        return False

    player = resolve_player_alias(parsed["player"])

    # Capture the totals before saving so only milestones actually crossed by
    # this event are announced. Existing historical totals will not cause a
    # flood of old milestone announcements after this feature is deployed.
    previous_loot_total = (
        get_player_loot_total(player)
        if parsed["event_type"] == "loot"
        else None
    )
    previous_pvp_kills = (
        get_player_pvp_kills(player)
        if parsed["event_type"] == "pvp_kill"
        else None
    )

    changed = await save_event(message, parsed)

    if changed:
        await process_milestone_announcements(
            message,
            parsed,
            previous_loot_total=previous_loot_total,
            previous_pvp_kills=previous_pvp_kills,
        )

    return changed


def get_player_loot_total(player: str) -> int:
    conn = db()
    try:
        row = conn.execute(
            """SELECT COALESCE(SUM(value_gp), 0) AS total
               FROM events
               WHERE event_type='loot'
                 AND LOWER(REPLACE(player, ' ', '')) =
                     LOWER(REPLACE(?, ' ', ''))""",
            (player,),
        ).fetchone()
        return int(row["total"] or 0)
    finally:
        conn.close()


def get_player_pvp_kills(player: str) -> int:
    conn = db()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS kills
               FROM events
               WHERE event_type='pvp_kill'
                 AND LOWER(REPLACE(player, ' ', '')) =
                     LOWER(REPLACE(?, ' ', ''))""",
            (player,),
        ).fetchone()
        return int(row["kills"] or 0)
    finally:
        conn.close()


def claim_milestones(player: str, milestone_type: str, thresholds):
    """Atomically claim newly reached milestones so each is announced once."""
    now = datetime.now(timezone.utc).isoformat()
    conn = db()
    claimed = []
    try:
        for threshold in thresholds:
            cur = conn.execute(
                """INSERT OR IGNORE INTO milestone_announcements
                   (player_key, milestone_type, threshold, created_at)
                   VALUES (?, ?, ?, ?)""",
                (player_key(player), milestone_type, threshold, now),
            )
            if cur.rowcount == 1:
                claimed.append(threshold)
        conn.commit()
        return claimed
    finally:
        conn.close()


async def send_milestone_announcement(
    message: discord.Message,
    player: str,
    title: str,
    description: str,
    color: discord.Color,
):
    channel = bot.get_channel(WEEKLY_LOOT_ANNOUNCEMENT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(WEEKLY_LOOT_ANNOUNCEMENT_CHANNEL_ID)
        except Exception as exc:
            print(f"ERROR: Could not access milestone announcement channel: {exc}")
            return

    discord_id = get_linked_discord_id(player)
    mention = f"<@{discord_id}>" if discord_id is not None else None

    if mention:
        description = f"{description}\n\n{mention}"

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    await channel.send(embed=embed)


def build_show_drop_url(message: discord.Message) -> str:
    return (
        f"https://discord.com/channels/"
        f"{message.guild.id if message.guild else '@me'}/"
        f"{message.channel.id}/{message.id}"
    )


async def process_milestone_announcements(
    message: discord.Message,
    parsed: dict,
    previous_loot_total=None,
    previous_pvp_kills=None,
):
    player = parsed["player"]
    event_type = parsed["event_type"]

    if event_type == "loot":
        total_loot = get_player_loot_total(player)

        crossed = [
            threshold for threshold in LOOT_MILESTONES
            if (previous_loot_total or 0) < threshold <= total_loot
        ]
        claimed = claim_milestones(player, "loot_total", crossed)

        for threshold in claimed:
            await send_milestone_announcement(
                message,
                player,
                "🏆 LOOT MILESTONE",
                (
                    f"**{player}** has reached **{format_gp(threshold)} GP** "
                    f"in total recorded loot! 💰"
                ),
                discord.Color.gold(),
            )

        # Every individual loot drop worth 20M+ gets a big-drop announcement.
        # The message ID is used as the unique claim key so edited/reprocessed
        # Dink messages cannot create the same announcement twice.
        value_gp = int(parsed.get("value_gp") or 0)
        if value_gp >= BIG_DROP_ANNOUNCEMENT_MIN_GP:
            claimed = claim_milestones(
                player,
                "big_drop_message",
                [int(message.id)],
            )
            if claimed:
                item = (
                    parsed.get("loot_item")
                    or parsed.get("source")
                    or "Loot drop"
                )
                drop_url = build_show_drop_url(message)
                embed = discord.Embed(
                    title="💎 BIG DROP!",
                    description=(
                        f"**{player}** just received **{item}** worth "
                        f"**{format_gp(value_gp)} GP**! 🎉\n\n"
                        f"[Show Drop]({drop_url})\n\n"
                        f"{mention or ''}"
                    ),
                    color=discord.Color.purple(),
                    timestamp=datetime.now(timezone.utc),
                )
                channel = bot.get_channel(WEEKLY_LOOT_ANNOUNCEMENT_CHANNEL_ID)
                if channel is None:
                    try:
                        channel = await bot.fetch_channel(
                            WEEKLY_LOOT_ANNOUNCEMENT_CHANNEL_ID
                        )
                    except Exception as exc:
                        print(f"ERROR: Could not access big-drop channel: {exc}")
                        return
                discord_id = get_linked_discord_id(player)
                mention = f"<@{discord_id}>" if discord_id is not None else None

                embed.description = (
                    f"{embed.description}\n\n{mention or ''}"
                )
                await channel.send(embed=embed)

    elif event_type == "pvp_kill":
        kills = get_player_pvp_kills(player)

        crossed = [
            threshold for threshold in PVP_KILL_MILESTONES
            if (previous_pvp_kills or 0) < threshold <= kills
        ]
        claimed = claim_milestones(player, "pvp_kills", crossed)

        for threshold in claimed:
            await send_milestone_announcement(
                message,
                player,
                "⚔️ PVP KILL MILESTONE",
                (
                    f"**{player}** has reached **{threshold:,} PvP kills**! "
                    f"⚔️"
                ),
                discord.Color.red(),
            )


def add_chunked_field(embed: discord.Embed, field_name: str, lines):
    """Add a leaderboard field without exceeding Discord's 1024-char field limit."""
    if not lines:
        return

    chunks = []
    current = ""

    for line in lines:
        line = str(line)
        if len(line) > 1000:
            line = line[:997] + "..."

        candidate = line if not current else current + "\n" + line

        if len(candidate) > 1000:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate

    if current:
        chunks.append(current)

    for index, chunk in enumerate(chunks, start=1):
        name = field_name if len(chunks) == 1 else f"{field_name} ({index}/{len(chunks)})"
        embed.add_field(name=name, value=chunk, inline=False)


def weekly_reset_countdown():
    """Return a countdown to the next Monday 00:00 in the bot's local time."""
    now = datetime.now()
    days_until_monday = (7 - now.weekday()) % 7
    next_monday = (now + timedelta(days=days_until_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    if next_monday <= now:
        next_monday += timedelta(days=7)

    remaining = next_monday - now
    total_seconds = max(0, int(remaining.total_seconds()))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    return f"{days}d {hours}h {minutes}m"


def get_weekly_loot_stats(limit=15):
    """Return the current Monday-Sunday weekly ranking.

    The active week always starts at the most recent Monday 00:00 and ends
    at the following Monday 00:00. This is deliberately calculated in Python
    using the bot's normal local datetime behavior, so Monday immediately
    starts a completely fresh weekly ranking.

    ONLY this weekly ranking combines multiple OSRS accounts that are linked
    to the same Discord ID. All other leaderboards continue to use OSRS names.
    Linked players are displayed by Discord mention; unlinked players remain
    shown by their OSRS name.
    """
    now = datetime.now()
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    next_week = week_start + timedelta(days=7)

    conn = db()
    rows = conn.execute(
        """
        WITH grouped AS (
            SELECT
                CASE
                    WHEN pdl.discord_id IS NOT NULL
                        THEN 'discord:' || CAST(pdl.discord_id AS TEXT)
                    ELSE 'player:' || LOWER(REPLACE(e.player, ' ', ''))
                END AS ranking_key,
                CASE
                    WHEN pdl.discord_id IS NOT NULL
                        THEN '<@' || CAST(pdl.discord_id AS TEXT) || '>'
                    ELSE e.player
                END AS display_name,
                SUM(e.value_gp) AS loot_gp,
                COUNT(*) AS loot_drops
            FROM events e
            LEFT JOIN player_discord_links pdl
                ON pdl.player_key = LOWER(REPLACE(e.player, ' ', ''))
            WHERE e.event_type='loot'
              AND datetime(e.created_at) >= datetime(?)
              AND datetime(e.created_at) < datetime(?)
            GROUP BY ranking_key, display_name
        )
        SELECT display_name AS player, loot_gp, loot_drops
        FROM grouped
        ORDER BY loot_gp DESC, display_name COLLATE NOCASE
        LIMIT ?
        """,
        (week_start.strftime("%Y-%m-%d %H:%M:%S"),
         next_week.strftime("%Y-%m-%d %H:%M:%S"),
         limit),
    ).fetchall()
    conn.close()
    return rows


def get_linked_discord_id(player: str):
    conn = db()
    row = conn.execute(
        "SELECT discord_id FROM player_discord_links WHERE player_key = ?",
        (player_key(player),),
    ).fetchone()
    conn.close()
    return int(row["discord_id"]) if row else None


def get_weekly_loot_win_count(player):
    conn = db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS wins FROM weekly_loot_wins WHERE player_id = ?",
            (player_key(player),),
        ).fetchone()
        return int(row["wins"] if row else 0)
    finally:
        conn.close()


def set_linked_discord_id(player: str, discord_id: int):
    conn = db()
    conn.execute(
        """
        INSERT INTO player_discord_links(player_key, player_name, discord_id)
        VALUES (?, ?, ?)
        ON CONFLICT(player_key) DO UPDATE SET
            player_name = excluded.player_name,
            discord_id = excluded.discord_id
        """,
        (player_key(player), display_player_name(player), discord_id),
    )
    conn.commit()
    conn.close()


def remove_linked_discord_id(player: str):
    conn = db()
    conn.execute(
        "DELETE FROM player_discord_links WHERE player_key = ?",
        (player_key(player),),
    )
    conn.commit()
    conn.close()


def get_all_leaderboard_players():
    rows = get_stats()
    return sorted(
        [row["player"] for row in rows if row["player"]],
        key=lambda name: name.casefold(),
    )


def get_stats():
    conn = db()
    rows = conn.execute(
        """
        WITH grouped AS (
            SELECT
                LOWER(REPLACE(player, ' ', '')) AS pkey,
                SUM(CASE WHEN event_type='loot' THEN value_gp ELSE 0 END) AS loot_gp,
                SUM(CASE WHEN event_type='loot' THEN 1 ELSE 0 END) AS loot_drops,
                SUM(CASE WHEN event_type='death' THEN 1 ELSE 0 END) AS deaths,
                SUM(CASE WHEN event_type='death' THEN value_gp ELSE 0 END) AS death_value_gp
            FROM events
            GROUP BY LOWER(REPLACE(player, ' ', ''))
        ),
        latest_names AS (
            SELECT
                LOWER(REPLACE(e.player, ' ', '')) AS pkey,
                e.player AS display_name
            FROM events e
            WHERE e.message_id = (
                SELECT e2.message_id
                FROM events e2
                WHERE LOWER(REPLACE(e2.player, ' ', '')) =
                      LOWER(REPLACE(e.player, ' ', ''))
                ORDER BY datetime(e2.created_at) DESC, e2.message_id DESC
                LIMIT 1
            )
        )
        SELECT
            n.display_name AS player,
            g.loot_gp,
            g.loot_drops,
            g.deaths,
            g.death_value_gp
        FROM grouped g
        JOIN latest_names n ON n.pkey = g.pkey
        """
    ).fetchall()
    conn.close()
    return rows


def get_completion_stats():
    """Return each player's best recorded completion count per activity/source.

    Dink's Completion Count is cumulative (e.g. 117, 118, 119), so summing it
    would massively over-count. We take the maximum for each player/source and
    then add those maxima together.
    """
    conn = db()
    rows = conn.execute(
        """
        SELECT player, source, MAX(completion_count) AS completions
        FROM events
        WHERE event_type='loot' AND completion_count > 0
        GROUP BY player, source
        ORDER BY player COLLATE NOCASE, source COLLATE NOCASE
        """
    ).fetchall()
    conn.close()

    totals = {}
    for row in rows:
        player = row['player']
        totals[player] = totals.get(player, 0) + (row['completions'] or 0)
    return sorted(totals.items(), key=lambda x: (-x[1], x[0].lower()))


def get_player_completions(player: str):
    conn = db()
    rows = conn.execute(
        """
        SELECT source, MAX(completion_count) AS completions
        FROM events
        WHERE event_type='loot' AND completion_count > 0 AND LOWER(player)=LOWER(?)
        GROUP BY source
        ORDER BY completions DESC
        """,
        (player,),
    ).fetchall()
    conn.close()
    return rows


def get_player_stats(player: str):
    key = player_key(player)
    conn = db()
    row = conn.execute(
        """
        SELECT
            (
                SELECT e2.player
                FROM events e2
                WHERE e2.event_type IN ('loot', 'death')
                  AND LOWER(REPLACE(e2.player, ' ', '')) = LOWER(REPLACE(?, ' ', ''))
                ORDER BY datetime(e2.created_at) DESC, e2.message_id DESC
                LIMIT 1
            ) AS player,
            SUM(CASE WHEN event_type='loot' THEN value_gp ELSE 0 END) AS loot_gp,
            SUM(CASE WHEN event_type='loot' THEN 1 ELSE 0 END) AS loot_drops,
            SUM(CASE WHEN event_type='death' THEN 1 ELSE 0 END) AS deaths,
            SUM(CASE WHEN event_type='death' THEN value_gp ELSE 0 END) AS death_value_gp
        FROM events
        WHERE LOWER(REPLACE(player, ' ', '')) = LOWER(REPLACE(?, ' ', ''))
        """,
        (player, player),
    ).fetchone()
    conn.close()
    return row


def get_top_activity_per_player(limit=15):
    """Return each player's activity/source with the most accumulated loot GP."""
    conn = db()
    rows = conn.execute(
        """
        WITH grouped AS (
            SELECT
                LOWER(REPLACE(player, ' ', '')) AS pkey,
                player,
                COALESCE(NULLIF(TRIM(source), ''), 'Unknown / Other') AS source,
                SUM(value_gp) AS loot_gp,
                COUNT(*) AS loot_drops
            FROM events
            WHERE event_type='loot'
            GROUP BY pkey, source
        ),
        ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY pkey
                    ORDER BY loot_gp DESC, loot_drops DESC, source COLLATE NOCASE
                ) AS rn
            FROM grouped
        )
        SELECT player, source, loot_gp, loot_drops
        FROM ranked
        WHERE rn = 1
        ORDER BY loot_gp DESC, player COLLATE NOCASE
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return rows



def get_player_loot_events(player: str):
    conn = db()
    rows = conn.execute(
        """
        SELECT player, value_gp, loot_item, source, created_at, message_id, channel_id
        FROM events
        WHERE event_type='loot'
          AND LOWER(REPLACE(player, ' ', '')) = LOWER(REPLACE(?, ' ', ''))
        ORDER BY created_at DESC, message_id DESC
        """,
        (player,),
    ).fetchall()
    conn.close()
    return rows


class PlayerDropsPages(discord.ui.View):
    """Paginated private view for all drops belonging to one player."""

    def __init__(self, player, rows, owner_id):
        super().__init__(timeout=300)
        self.player = player
        self.rows = list(rows)
        self.owner_id = owner_id
        self.page = 0
        self.per_page = 10
        self._refresh_buttons()

    @property
    def total_pages(self):
        return max(1, (len(self.rows) + self.per_page - 1) // self.per_page)

    def build_embed(self):
        start = self.page * self.per_page
        page_rows = self.rows[start:start + self.per_page]

        total = sum(row["value_gp"] or 0 for row in self.rows)
        weekly_wins = get_weekly_loot_win_count(self.player)
        guild_id = None
        drops_channel = bot.get_channel(DROPS_CHANNEL_ID)
        if drops_channel and getattr(drops_channel, "guild", None):
            guild_id = drops_channel.guild.id

        lines = []
        for row in page_rows:
            item = row["loot_item"] or "Unknown item"
            jump_url = (
                f"https://discord.com/channels/{guild_id}/"
                f"{row['channel_id']}/{row['message_id']}"
                if guild_id
                else "https://discord.com"
            )
            lines.append(
                f"💎 **{format_gp(row['value_gp'] or 0)} GP** — "
                f"**{item}** • [Show drop]({jump_url})"
            )

        embed = discord.Embed(
            title=f"💎 {self.player} — ALL DROPS",
            description=(
                f"**{len(self.rows):,} "
                f"{'drop' if len(self.rows) == 1 else 'drops'}** • "
                f"**{format_gp(total)} GP** total\n"
                f"🏆 **Weekly Loot Wins: {weekly_wins}**\n"
                f"⚠️ Only Dink drops of **500K GP+** are recorded.\n\n"
                + "\n".join(lines)
            ),
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(
            text=f"Page {self.page + 1}/{self.total_pages} • "
                 f"Showing {start + 1}-{min(start + self.per_page, len(self.rows))} "
                 f"of {len(self.rows)} drops"
        )
        return embed

    def _refresh_buttons(self):
        self.clear_items()

        previous = discord.ui.Button(
            label="Previous",
            emoji="◀️",
            style=discord.ButtonStyle.secondary,
            disabled=self.page <= 0,
        )
        next_button = discord.ui.Button(
            label="Next",
            emoji="▶️",
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= self.total_pages - 1,
        )
        close = discord.ui.Button(
            label="Close",
            emoji="✖️",
            style=discord.ButtonStyle.danger,
        )

        async def previous_callback(interaction):
            _log_interaction_readable(interaction, "Previous Page")
            if not await self._check_owner(interaction):
                return
            self.page -= 1
            self._refresh_buttons()
            await interaction.response.edit_message(
                embed=self.build_embed(),
                view=self,
            )

        async def next_callback(interaction):
            _log_interaction_readable(interaction, "Next Page")
            if not await self._check_owner(interaction):
                return
            self.page += 1
            self._refresh_buttons()
            await interaction.response.edit_message(
                embed=self.build_embed(),
                view=self,
            )

        async def close_callback(interaction):
            _log_interaction_readable(interaction, "Close")
            if not await self._check_owner(interaction):
                return
            self.stop()
            await interaction.response.defer()
            try:
                await interaction.delete_original_response()
            except (discord.NotFound, discord.HTTPException):
                # If Discord has already removed the ephemeral response,
                # there is nothing left to delete.
                pass

        previous.callback = previous_callback
        next_button.callback = next_callback
        close.callback = close_callback

        self.add_item(previous)
        self.add_item(next_button)
        self.add_item(close)

    async def _check_owner(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This player lookup belongs to someone else.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        self.stop()


class ShowAllDropsSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(
            placeholder="Choose a player to show all drops...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="leaderboard_show_all_drops",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        player = self.values[0]
        _log_interaction_readable(interaction, "Player Profile", player=player)
        await send_player_stats(interaction, player)


class LeaderboardCategoryButton(discord.ui.Button):
    def __init__(self, category, label, emoji):
        super().__init__(
            label=label,
            emoji=emoji,
            style=discord.ButtonStyle.secondary,
            custom_id=f"leaderboard_category_{category}",
            row=0,
        )
        self.category = category

    async def callback(self, interaction: discord.Interaction):
        _log_interaction_readable(interaction, f"Leaderboard Category: {self.category.title()}")
        view = self.view
        if view is None:
            await interaction.response.send_message(
                "Leaderboard navigation is unavailable. Please refresh the leaderboard.",
                ephemeral=True,
            )
            return

        embed = view.embeds.get(self.category)
        if embed is None:
            await interaction.response.send_message(
                "This leaderboard category is unavailable right now.",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(embed=embed, view=view)


class LeaderboardView(discord.ui.View):
    """Single-message leaderboard navigation with persistent buttons."""

    def __init__(self, players=None):
        super().__init__(timeout=None)
        self.embeds = {}
        self.player_select = None
        self._build_buttons()
        if players:
            self.set_players(players)

    def _build_buttons(self):
        self.clear_items()
        self.add_item(LeaderboardCategoryButton("loot", "Loot", "💰"))
        self.add_item(LeaderboardCategoryButton("deaths", "Deaths", "💀"))
        self.add_item(LeaderboardCategoryButton("biggest", "Biggest Drop", "💎"))
        self.add_item(LeaderboardCategoryButton("activity", "Activity", "📍"))

        if self.player_select is not None:
            self.add_item(self.player_select)

    def set_players(self, players):
        options = [
            discord.SelectOption(
                label=player[:100],
                value=player[:100],
                description="View player profile",
            )
            for player in players[:25]
        ]

        if not options:
            self.player_select = None
        else:
            self.player_select = ShowAllDropsSelect(options)

        self._build_buttons()

    def set_embeds(self, embeds):
        self.embeds = embeds


def get_biggest_drop_per_player(limit=15):
    """Return each player's single most valuable loot event, with its Discord message ID."""
    conn = db()
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT
                e.player,
                e.value_gp,
                e.source,
                e.loot_item,
                e.created_at,
                e.message_id,
                e.channel_id,
                ROW_NUMBER() OVER (
                    PARTITION BY LOWER(REPLACE(e.player, ' ', ''))
                    ORDER BY e.value_gp DESC, e.created_at ASC, e.message_id ASC
                ) AS rn
            FROM events e
            WHERE e.event_type='loot' AND e.value_gp > 0
        )
        SELECT player, value_gp, source, loot_item, created_at, message_id, channel_id
        FROM ranked
        WHERE rn = 1
        ORDER BY value_gp DESC, player COLLATE NOCASE
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def get_biggest_drops(limit=10):
    conn = db()
    rows = conn.execute(
        """
        SELECT player, value_gp, source, created_at
        FROM events
        WHERE event_type='loot' AND value_gp > 0
        ORDER BY value_gp DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return rows


async def get_or_create_leaderboard_message(channel, setting_key, embed, view=None):
    """Fetch an existing leaderboard message or create it and remember its ID."""
    conn = db()
    setting = conn.execute(
        "SELECT value FROM settings WHERE key=?",
        (setting_key,),
    ).fetchone()
    conn.close()

    message = None
    if setting:
        try:
            message = await channel.fetch_message(int(setting["value"]))
        except (discord.NotFound, discord.Forbidden):
            message = None

    if message:
        await message.edit(embed=embed, view=view)
        return message

    message = await channel.send(embed=embed, view=view)
    conn = db()
    conn.execute(
        """
        INSERT INTO settings(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (setting_key, str(message.id)),
    )
    conn.commit()
    conn.close()
    return message


async def delete_leaderboard_message(channel, setting_key):
    """Delete a previously-created leaderboard message and remove its stored ID."""
    conn = db()
    setting = conn.execute(
        "SELECT value FROM settings WHERE key=?",
        (setting_key,),
    ).fetchone()
    if setting:
        try:
            message = await channel.fetch_message(int(setting["value"]))
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass
    conn.execute("DELETE FROM settings WHERE key=?", (setting_key,))
    conn.commit()
    conn.close()


async def remove_old_combined_leaderboard(channel):
    """Remove the old single-message leaderboard from older bot versions."""
    conn = db()
    setting = conn.execute(
        "SELECT value FROM settings WHERE key='leaderboard_message_id'"
    ).fetchone()

    if not setting:
        conn.close()
        return

    try:
        old_message = await channel.fetch_message(int(setting["value"]))
        await old_message.delete()
        print("Removed old combined leaderboard message.")
    except (discord.NotFound, discord.Forbidden):
        pass

    conn.execute("DELETE FROM settings WHERE key='leaderboard_message_id'")
    conn.commit()
    conn.close()


async def update_leaderboard():
    async with update_lock:
        global leaderboard_view

        channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
        if channel is None:
            try:
                channel = await bot.fetch_channel(LEADERBOARD_CHANNEL_ID)
            except Exception as e:
                print(f"Could not access leaderboard channel: {e}")
                return

        rows = get_stats()
        weekly_loot_rows = get_weekly_loot_stats(15)
        biggest_per_player_rows = get_biggest_drop_per_player(15)
        top_activity_rows = get_top_activity_per_player(15)

        # -------------------- LOOT LEADERBOARD --------------------
        loot_embed = discord.Embed(
            title="💰 LOOT LEADERBOARD",
            description=(
                "━━━━━━━━━━━━━━━━━━━━\n"
                "**TOTAL LOOT RANKING**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Highest recorded loot value per player.\n\n"
                "⚠️ Only Dink drops of **500K GP+** are recorded."
            ),
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )

        loot_rows = sorted(
            [r for r in rows if (r["loot_gp"] or 0) > 0],
            key=lambda r: (r["loot_gp"] or 0),
            reverse=True,
        )

        if loot_rows:
            lines = []
            medals = ["🥇", "🥈", "🥉"]
            for i, row in enumerate(loot_rows[:15], start=1):
                prefix = medals[i - 1] if i <= 3 else f"**{i}.**"
                count = row["loot_drops"] or 0
                lines.append(
                    f"{prefix} **{row['player']}** — **{format_gp(row['loot_gp'] or 0)} GP** "
                    f"↳ **{count:,} {'drop' if count == 1 else 'drops'}**"
                )

            total_loot = sum(r["loot_gp"] or 0 for r in loot_rows)
            total_drops = sum(r["loot_drops"] or 0 for r in loot_rows)

            loot_embed.add_field(
                name="📊 GROUP TOTALS",
                value=(
                    f"💰 **{format_gp(total_loot)} GP** total loot   •   "
                    f"🎁 **{total_drops:,}** drops"
                ),
                inline=False,
            )
            add_chunked_field(loot_embed, "🏆 TOP LOOTERS", lines)

            # -------------------- WEEKLY LOOT --------------------
            weekly_lines = []
            weekly_medals = ["🥇", "🥈", "🥉"]
            for i, row in enumerate(weekly_loot_rows, start=1):
                prefix = weekly_medals[i - 1] if i <= 3 else f"**{i}.**"
                count = row["loot_drops"] or 0
                weekly_lines.append(
                    f"{prefix} **{row['player']}** — **{format_gp(row['loot_gp'] or 0)} GP** "
                    f"↳ **{count:,} {'drop' if count == 1 else 'drops'}**"
                )

            weekly_header = (
                "━━━━━━━━━━━━━━━━━━━━\n"
                "**WEEKLY LOOT RANKING**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Combined loot value per member this week.\n"
                f"⏱️ **Resets in: {weekly_reset_countdown()}**\n\n"
            )

            if weekly_lines:
                weekly_chunks = []
                current = ""
                for line in weekly_lines:
                    candidate = line if not current else current + "\n" + line
                    if len(weekly_header) + len(candidate) > 1000:
                        if current:
                            weekly_chunks.append(current)
                        current = line
                    else:
                        current = candidate
                if current:
                    weekly_chunks.append(current)

                for index, chunk in enumerate(weekly_chunks):
                    value = weekly_header + chunk if index == 0 else chunk
                    loot_embed.add_field(
                        name="\u200b",
                        value=value,
                        inline=False,
                    )
            else:
                loot_embed.add_field(
                    name="\u200b",
                    value=weekly_header + "No loot drops recorded this week yet.",
                    inline=False,
                )
        else:
            loot_embed.description = "No loot drops have been imported yet."

        # -------------------- DEATH LEADERBOARD --------------------
        death_embed = discord.Embed(
            title="💀 DEATH LEADERBOARD",
            description=(
                "━━━━━━━━━━━━━━━━━━━━\n"
                "**MOST DEATHS**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Player deaths reported by Dink, ranked by death count."
            ),
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )

        death_rows = sorted(
            [r for r in rows if (r["deaths"] or 0) > 0],
            key=lambda r: (r["deaths"] or 0),
            reverse=True,
        )

        if death_rows:
            lines = []
            medals = ["🥇", "🥈", "🥉"]
            for i, row in enumerate(death_rows[:15], start=1):
                prefix = medals[i - 1] if i <= 3 else f"**{i}.**"
                lost = row["death_value_gp"] or 0
                suffix = f"\n　↳ 💸 {format_gp(lost)} GP PvP loss" if lost else ""
                lines.append(
                    f"{prefix} **{row['player']}** — **{row['deaths']:,} deaths**{suffix}"
                )

            add_chunked_field(death_embed, "Most Deaths", lines)

            total_deaths = sum(r["deaths"] or 0 for r in death_rows)
            total_loss = sum(r["death_value_gp"] or 0 for r in death_rows)
            death_embed.add_field(
                name="📊 GROUP TOTALS",
                value=(
                    f"💀 **{total_deaths:,}** deaths   •   "
                    f"💸 **{format_gp(total_loss)} GP** lost in PvP"
                ),
                inline=False,
            )
        else:
            death_embed.description = "No deaths have been imported yet."

        # -------------------- BIGGEST DROP PER PLAYER --------------------
        biggest_embed = discord.Embed(
            title="💎 BIGGEST DROP PER PLAYER",
            description=(
                "━━━━━━━━━━━━━━━━━━━━\n"
                "**PERSONAL RECORD DROPS**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Each player's single most valuable recorded drop.\n\n"
                "⚠️ Only Dink drops of **500K GP+** are recorded."
            ),
            color=discord.Color.purple(),
            timestamp=datetime.now(timezone.utc),
        )

        if biggest_per_player_rows:
            lines = []
            medals = ["🥇", "🥈", "🥉"]
            guild_id = getattr(getattr(channel, "guild", None), "id", None)

            for i, row in enumerate(biggest_per_player_rows, start=1):
                prefix = medals[i - 1] if i <= 3 else f"**{i}.**"
                item = f" • {row['loot_item']}" if row["loot_item"] else ""
                jump_url = (
                    f"https://discord.com/channels/{guild_id}/"
                    f"{row['channel_id']}/{row['message_id']}"
                    if guild_id else "https://discord.com"
                )
                lines.append(
                    f"{prefix} **{row['player']}** — "
                    f"**{format_gp(row['value_gp'])} GP**{item} • "
                    f"[View drop]({jump_url})"
                )

            biggest_embed.description = "\n".join(lines)
        else:
            biggest_embed.description = "No loot drops have been imported yet."

        # -------------------- MOST GP EARNED AT --------------------
        activity_embed = discord.Embed(
            title="📍 MOST GP EARNED AT",
            description=(
                "━━━━━━━━━━━━━━━━━━━━\n"
                "**TOP ACTIVITY PER PLAYER**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "The activity where each player has earned the most recorded GP."
            ),
            color=discord.Color.teal(),
            timestamp=datetime.now(timezone.utc),
        )

        if top_activity_rows:
            lines = []
            medals = ["🥇", "🥈", "🥉"]
            for i, row in enumerate(top_activity_rows, start=1):
                prefix = medals[i - 1] if i <= 3 else f"**{i}.**"
                count = row["loot_drops"] or 0
                lines.append(
                    f"{prefix} **{row['player']}** — "
                    f"**{format_gp(row['loot_gp'] or 0)} GP** • "
                    f"{row['source']} ({count:,} {'drop' if count == 1 else 'drops'})"
                )
            activity_embed.description = "\n".join(lines)
        else:
            activity_embed.description = "No loot drops have been imported yet."

        for embed in (loot_embed, death_embed, biggest_embed, activity_embed):
            embed.set_footer(text="Updated automatically")

        embeds = {
            "loot": loot_embed,
            "deaths": death_embed,
            "biggest": biggest_embed,
            "activity": activity_embed,
        }

        # Reuse one persistent View so buttons keep pointing at fresh embeds.
        if leaderboard_view is None:
            leaderboard_view = LeaderboardView()

        leaderboard_view.set_embeds(embeds)
        leaderboard_view.set_players([row["player"] for row in loot_rows[:25]])

        # Migrate away from the previous four-message layout.
        for old_key in (
            "loot_leaderboard_message_id",
            "death_leaderboard_message_id",
            "biggest_drop_per_player_message_id",
            "top_activity_message_id",
            "biggest_drops_message_id",
            "leaderboard_message_id",
        ):
            await delete_leaderboard_message(channel, old_key)

        await get_or_create_leaderboard_message(
            channel,
            "combined_leaderboard_message_id",
            loot_embed,
            view=leaderboard_view,
        )


async def backfill_channel(channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)

    processed = 0
    async for message in channel.history(limit=None, oldest_first=True):
        if await process_message(message):
            processed += 1

    return processed


class DiscordIdModal(discord.ui.Modal):
    def __init__(self, player: str, parent_view):
        super().__init__(title=f"Link Discord ID — {player[:35]}")
        self.player = player
        self.parent_view = parent_view

        self.discord_id = discord.ui.TextInput(
            label="Discord User ID",
            placeholder="Paste the Discord user ID here (or leave blank to unlink)",
            required=False,
            max_length=20,
        )
        self.add_item(self.discord_id)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.discord_id.value.strip()

        if not raw:
            remove_linked_discord_id(self.player)
            await interaction.response.send_message(
                f"🔓 Removed the Discord ID link from **{self.player}**.",
                ephemeral=True,
            )
            await self.parent_view.refresh(interaction)
            return

        if not raw.isdigit():
            await interaction.response.send_message(
                "❌ The Discord ID must contain numbers only.",
                ephemeral=True,
            )
            return

        discord_id = int(raw)

        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This command can only be used in a server.",
                ephemeral=True,
            )
            return

        try:
            member = interaction.guild.get_member(discord_id)
            if member is None:
                member = await interaction.guild.fetch_member(discord_id)
        except (discord.NotFound, discord.HTTPException):
            await interaction.response.send_message(
                f"❌ I could not find Discord member `{discord_id}` in this server.",
                ephemeral=True,
            )
            return

        set_linked_discord_id(self.player, member.id)

        await interaction.response.send_message(
            f"✅ Linked **{self.player}** to {member.mention} (`{member.id}`).",
            ephemeral=True,
        )
        await self.parent_view.refresh(interaction)


class ShowIdsView(discord.ui.View):
    PAGE_SIZE = 25

    def __init__(self, interaction: discord.Interaction):
        super().__init__(timeout=300)
        self.owner_id = interaction.user.id
        self.page = 0
        self.players = get_all_leaderboard_players()
        self.message = None
        self.select = None
        self.rebuild_items()

    def page_count(self):
        return max(1, (len(self.players) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def current_players(self):
        start = self.page * self.PAGE_SIZE
        return self.players[start:start + self.PAGE_SIZE]

    def rebuild_items(self):
        # Remove old dynamic components.
        self.clear_items()

        current = self.current_players()
        options = []

        for player in current:
            linked_id = get_linked_discord_id(player)
            if linked_id:
                label = f"{player} — linked"
                description = f"Discord ID: {linked_id}"
            else:
                label = f"{player} — not linked"
                description = "No Discord ID assigned"

            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=player,
                    description=description[:100],
                )
            )

        if options:
            self.select = discord.ui.Select(
                placeholder="Select a player to assign a Discord ID...",
                options=options,
                min_values=1,
                max_values=1,
                row=0,
            )

            async def select_callback(interaction: discord.Interaction):
                if interaction.user.id != self.owner_id:
                    await interaction.response.send_message(
                        "❌ This menu belongs to the person who opened it.",
                        ephemeral=True,
                    )
                    return

                player = self.select.values[0]
                await interaction.response.send_modal(
                    DiscordIdModal(player, self)
                )

            self.select.callback = select_callback
            self.add_item(self.select)

        previous_button = discord.ui.Button(
            label="◀ Previous",
            style=discord.ButtonStyle.secondary,
            disabled=self.page <= 0,
            row=1,
        )
        next_button = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= self.page_count() - 1,
            row=1,
        )
        close_button = discord.ui.Button(
            label="Close",
            style=discord.ButtonStyle.danger,
            row=1,
        )

        async def previous_callback(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message(
                    "❌ This menu belongs to the person who opened it.",
                    ephemeral=True,
                )
                return
            self.page -= 1
            self.rebuild_items()
            await interaction.response.edit_message(
                embed=self.make_embed(),
                view=self,
            )

        async def next_callback(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message(
                    "❌ This menu belongs to the person who opened it.",
                    ephemeral=True,
                )
                return
            self.page += 1
            self.rebuild_items()
            await interaction.response.edit_message(
                embed=self.make_embed(),
                view=self,
            )

        async def close_callback(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message(
                    "❌ This menu belongs to the person who opened it.",
                    ephemeral=True,
                )
                return
            await interaction.response.edit_message(
                content="🔒 Discord ID manager closed.",
                embed=None,
                view=None,
            )
            self.stop()

        previous_button.callback = previous_callback
        next_button.callback = next_callback
        close_button.callback = close_callback

        self.add_item(previous_button)
        self.add_item(next_button)
        self.add_item(close_button)

    def make_embed(self):
        total = len(self.players)
        linked = sum(
            1 for player in self.players if get_linked_discord_id(player)
        )
        start = self.page * self.PAGE_SIZE + 1 if total else 0
        end = min((self.page + 1) * self.PAGE_SIZE, total)

        embed = discord.Embed(
            title="🔗 PLAYER DISCORD ID MANAGER",
            description=(
                "Select a leaderboard player below to assign their Discord ID.\n\n"
                f"**Players:** {total:,} • **Linked:** {linked:,} • "
                f"**Unlinked:** {total - linked:,}\n"
                f"Showing **{start:,}–{end:,}** • Page **{self.page + 1}/{self.page_count()}**\n\n"
                "After selecting a player, paste their Discord User ID. "
                "Leave it blank to remove an existing link."
            ),
            color=discord.Color.blurple(),
        )
        return embed

    async def refresh(self, interaction: discord.Interaction):
        # Rebuild the player list in case /namechange or /removeplayer changed it.
        self.players = get_all_leaderboard_players()
        self.page = min(self.page, self.page_count() - 1)
        self.rebuild_items()

        if self.message:
            try:
                await self.message.edit(embed=self.make_embed(), view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


async def get_weekly_loot_winner():
    rows = get_weekly_loot_stats(1)
    return rows[0]["player"] if rows else None


async def get_weekly_loot_winner_role(guild: discord.Guild):
    return discord.utils.get(guild.roles, name=WEEKLY_LOOT_WINNER_ROLE_NAME)


async def remove_weekly_loot_role_from_others(
    guild: discord.Guild, keep_member_id: int | None = None
):
    role = await get_weekly_loot_winner_role(guild)
    if role is None:
        return

    # Do NOT use guild.fetch_members() here. That requires the privileged
    # Server Members Intent. Instead, fetch only the Discord IDs that are
    # already linked to leaderboard players. This works without Members Intent
    # and lets us reliably find the previous role holder after a restart.
    conn = db()
    rows = conn.execute(
        "SELECT DISTINCT discord_id FROM player_discord_links"
    ).fetchall()
    conn.close()

    candidate_ids = {
        int(row["discord_id"])
        for row in rows
        if row["discord_id"] is not None
    }

    # Include cached role members too, in case a member is cached but not
    # currently represented in the links table.
    for member in list(role.members):
        candidate_ids.add(member.id)

    for member_id in candidate_ids:
        if keep_member_id is not None and member_id == keep_member_id:
            continue

        try:
            member = guild.get_member(member_id)
            if member is None:
                member = await guild.fetch_member(member_id)

            if role not in member.roles:
                continue

            await member.remove_roles(
                role,
                reason="Weekly Loot Winner rotation",
            )
            print(
                f"Removed Weekly Loot Winner role from {member} "
                f"({member.id})"
            )
        except discord.NotFound:
            # The linked Discord account is no longer in this server.
            continue
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(
                f"Could not remove Weekly Loot Winner role from "
                f"{member_id}: {exc}"
            )


async def grant_weekly_loot_role(player: str, guild: discord.Guild):
    role = await get_weekly_loot_winner_role(guild)
    if role is None:
        return False, f'Role "{WEEKLY_LOOT_WINNER_ROLE_NAME}" was not found in the server.'

    discord_id = get_linked_discord_id(player)
    if discord_id is None:
        return False, f"No Discord ID is linked to **{player}**. Use `/showids` to link it."

    member = guild.get_member(discord_id)
    if member is None:
        try:
            member = await guild.fetch_member(discord_id)
        except (discord.NotFound, discord.HTTPException):
            return False, f"Could not find Discord member `{discord_id}` for **{player}**."

    me = guild.me
    if me is not None and role >= me.top_role:
        return False, (
            f'The role "{role.name}" is higher than or equal to my highest role, '
            "so I cannot manage it."
        )

    await remove_weekly_loot_role_from_others(guild, keep_member_id=member.id)

    try:
        await member.add_roles(
            role,
            reason=f"Weekly Loot Winner: {player}",
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        return False, f"I could not grant the role to **{player}**: `{exc}`"

    return True, f"🏆 **{player}** now has the **{role.name}** role."


WEEKLY_WINNER_LAST_AWARDED_KEY = "weekly_winner_last_awarded_week"


def get_previous_completed_week_key():
    """Return the Monday date for the most recently completed Monday-Sunday week.

    The active week starts every Monday at 00:00. Therefore the completed week
    is always the Monday immediately before the current Monday.
    """
    now = datetime.now()
    this_monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    completed_monday = this_monday - timedelta(days=7)
    return completed_monday.strftime("%Y-%m-%d")


def get_last_awarded_week():
    conn = db()
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (WEEKLY_WINNER_LAST_AWARDED_KEY,),
        ).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def set_last_awarded_week(week_key):
    conn = db()
    try:
        conn.execute(
            """
            INSERT INTO settings(key, value)
            VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (WEEKLY_WINNER_LAST_AWARDED_KEY, week_key),
        )
        conn.commit()
    finally:
        conn.close()


def get_completed_weekly_loot_winner(week_monday):
    """Return the winner of the completed Monday-Sunday week."""
    conn = db()
    try:
        row = conn.execute(
            """
            WITH grouped AS (
                SELECT
                    LOWER(REPLACE(player, ' ', '')) AS pkey,
                    SUM(value_gp) AS loot_gp
                FROM events
                WHERE event_type='loot'
                  AND datetime(created_at) >= datetime(?, '00:00:00')
                  AND datetime(created_at) < datetime(?, '+7 days', '00:00:00')
                GROUP BY pkey
            ),
            latest_names AS (
                SELECT
                    LOWER(REPLACE(e.player, ' ', '')) AS pkey,
                    e.player AS display_name
                FROM events e
                WHERE e.event_type='loot'
                  AND e.message_id = (
                      SELECT e2.message_id
                      FROM events e2
                      WHERE e2.event_type='loot'
                        AND LOWER(REPLACE(e2.player, ' ', '')) =
                            LOWER(REPLACE(e.player, ' ', ''))
                        AND datetime(e2.created_at) >= datetime(?, '00:00:00')
                        AND datetime(e2.created_at) < datetime(?, '+7 days', '00:00:00')
                      ORDER BY datetime(e2.created_at) DESC, e2.message_id DESC
                      LIMIT 1
                  )
            )
            SELECT n.display_name AS player
            FROM grouped g
            JOIN latest_names n ON n.pkey = g.pkey
            ORDER BY g.loot_gp DESC, n.display_name COLLATE NOCASE
            LIMIT 1
            """,
            (week_monday, week_monday, week_monday, week_monday),
        ).fetchone()
        return row["player"] if row else None
    finally:
        conn.close()


def get_completed_weekly_winner_drops(winner, completed_week, discord_id=None):
    """Return the loot drops that contributed to the completed weekly win.

    If the winner has a linked Discord ID, include drops from all OSRS accounts
    linked to that Discord ID, matching the weekly leaderboard's grouping.
    Otherwise only include the winner's OSRS profile.
    """
    conn = db()
    try:
        if discord_id is not None:
            rows = conn.execute(
                """
                SELECT e.player, e.value_gp, e.loot_item, e.source, e.created_at, e.channel_id, e.message_id
                FROM events e
                JOIN player_discord_links pdl
                  ON pdl.player_key = LOWER(REPLACE(e.player, ' ', ''))
                WHERE e.event_type='loot'
                  AND pdl.discord_id = ?
                  AND datetime(e.created_at) >= datetime(?, '00:00:00')
                  AND datetime(e.created_at) < datetime(?, '+7 days', '00:00:00')
                ORDER BY datetime(e.created_at) ASC, e.message_id ASC
                """,
                (int(discord_id), completed_week, completed_week),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT player, value_gp, loot_item, source, created_at, channel_id, message_id
                FROM events
                WHERE event_type='loot'
                  AND LOWER(REPLACE(player, ' ', '')) =
                      LOWER(REPLACE(?, ' ', ''))
                  AND datetime(created_at) >= datetime(?, '00:00:00')
                  AND datetime(created_at) < datetime(?, '+7 days', '00:00:00')
                ORDER BY datetime(created_at) ASC, message_id ASC
                """,
                (winner, completed_week, completed_week),
            ).fetchall()
        return rows
    finally:
        conn.close()


def build_weekly_winner_announcement(
    winner, discord_id, completed_week, role, drops=None, guild_id=None
):
    mention = f"<@{int(discord_id)}>" if discord_id is not None else f"**{winner}**"
    role_mention = role.mention

    # Dutch date formatting without changing the bot's timezone behaviour.
    week_start = datetime.strptime(completed_week, "%Y-%m-%d")
    week_end = week_start + timedelta(days=6)
    dutch_months = {
        1: "januari", 2: "februari", 3: "maart", 4: "april",
        5: "mei", 6: "juni", 7: "juli", 8: "augustus",
        9: "september", 10: "oktober", 11: "november", 12: "december",
    }
    week_range = (
        f"{week_start.day} {dutch_months[week_start.month]} {week_start.year}"
        f" t/m "
        f"{week_end.day} {dutch_months[week_end.month]} {week_end.year}"
    )

    description = (
        f"Congratulations {mention}!\n\n"
        f"You finished **#1** in the weekly loot ranking "
        f"for the completed week **{week_range}**.\n\n"
    )

    if drops:
        total_won = sum(int(row["value_gp"] or 0) for row in drops)
        description += (
            f"💰 **TOTAL THIS WEEK: {format_gp(total_won)} GP**\n\n"
            f"**DROPS THIS WEEK**\n"
        )
        drop_lines = []

        for row in drops[:15]:
            item = row["loot_item"] or row["source"] or "Loot drop"
            value = format_gp(int(row["value_gp"] or 0))
            player_name = row["player"]

            if guild_id:
                drop_url = (
                    f"https://discord.com/channels/"
                    f"{guild_id}/{row['channel_id']}/{row['message_id']}"
                )
                show_drop = f"[Show Drop]({drop_url})"
            else:
                show_drop = "Show Drop"

            if discord_id is not None:
                drop_lines.append(
                    f"• **{item}** — **{value} GP** ({player_name}) • {show_drop}"
                )
            else:
                drop_lines.append(
                    f"• **{item}** — **{value} GP** • {show_drop}"
                )

        description += "\n".join(drop_lines)

        if len(drops) > 15:
            description += f"\n• *...and {len(drops) - 15} more drops*"

        description += "\n\n"

    description += f"🎖️ The {role_mention} role has been granted to you!"

    return discord.Embed(
        title="🏆 WEEKLY LOOT WINNER",
        description=description,
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )


@tasks.loop(hours=1)
async def weekly_loot_role_rotation():
    try:
        # Only the winner of the most recently COMPLETED week receives
        # the role. The active week's current #1 is never awarded.
        completed_week = get_previous_completed_week_key()

        # Prevent re-awarding the same completed week every hour.
        if get_last_awarded_week() == completed_week:
            return

        winner = get_completed_weekly_loot_winner(completed_week)
        if not winner:
            return

        drops_channel = bot.get_channel(DROPS_CHANNEL_ID)
        if drops_channel is None:
            drops_channel = await bot.fetch_channel(DROPS_CHANNEL_ID)

        guild = getattr(drops_channel, "guild", None)
        if guild is None:
            return

        success, message = await grant_weekly_loot_role(winner, guild)
        print(
            f"Weekly Loot Winner rotation for completed week "
            f"{completed_week}: {message}"
        )

        # Only mark the week as awarded after the role operation succeeds.
        if success:
            conn = db()
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO weekly_loot_wins(player_id, week_monday)
                    VALUES(?, ?)
                    """,
                    (player_key(winner), completed_week),
                )
                conn.commit()
            finally:
                conn.close()
            # Announce the completed week's winner only after the role was
            # successfully granted.
            try:
                announcement_channel = bot.get_channel(
                    WEEKLY_LOOT_ANNOUNCEMENT_CHANNEL_ID
                )
                if announcement_channel is None:
                    announcement_channel = await bot.fetch_channel(
                        WEEKLY_LOOT_ANNOUNCEMENT_CHANNEL_ID
                    )

                discord_id = get_linked_discord_id(winner)
                mention = f"<@{int(discord_id)}>" if discord_id is not None else f"**{winner}**"
                winner_role = await get_weekly_loot_winner_role(guild)
                if winner_role is None:
                    raise RuntimeError(
                        f'Role "{WEEKLY_LOOT_WINNER_ROLE_NAME}" was not found in the server.'
                    )

                winner_drops = get_completed_weekly_winner_drops(
                    winner,
                    completed_week,
                    discord_id,
                )

                announcement = build_weekly_winner_announcement(
                    winner,
                    discord_id,
                    completed_week,
                    winner_role,
                    winner_drops,
                    guild.id,
                )
                announcement.set_footer(
                    text="The new weekly loot ranking has now started."
                )
                await announcement_channel.send(embed=announcement)
                print(
                    f"Weekly Loot Winner announcement sent to channel "
                    f"{WEEKLY_LOOT_ANNOUNCEMENT_CHANNEL_ID}."
                )
            except Exception as exc:
                print(
                    f"Weekly Loot Winner announcement failed: "
                    f"{type(exc).__name__}: {exc}"
                )

            # Mark the completed week as processed even if the announcement
            # channel temporarily fails, so the winner is not re-awarded.
            set_last_awarded_week(completed_week)
    except Exception as exc:
        print(f"Weekly Loot Winner rotation error: {type(exc).__name__}: {exc}")


@weekly_loot_role_rotation.before_loop
async def before_weekly_loot_role_rotation():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    init_db()
    try:
        # Sync globally as before.
        synced = await bot.tree.sync()
        print(f"Logged in as {bot.user}. Synced {len(synced)} global slash commands.")

        # Sync directly to the guild that owns the Dink drops channel.
        # Guild slash commands propagate immediately after restart.
        drops_channel = bot.get_channel(DROPS_CHANNEL_ID)
        if drops_channel is None:
            drops_channel = await bot.fetch_channel(DROPS_CHANNEL_ID)

        guild = getattr(drops_channel, "guild", None)
        if guild is not None:
            # Global commands are not automatically included in a guild sync.
            # Copy the global command tree into this guild first, then sync it.
            # This makes newly added commands such as /removeplayer appear
            # immediately after a restart instead of waiting for global
            # command propagation.
            bot.tree.copy_global_to(guild=guild)
            guild_synced = await bot.tree.sync(guild=guild)
            print(
                f"Synced {len(guild_synced)} guild slash commands "
                f"to {guild.name} ({guild.id})."
            )
    except Exception as e:
        print(f"Slash command sync failed: {e}")

    try:
        await update_leaderboard()
        # Register the same persistent view after it has been populated.
        if leaderboard_view is not None:
            bot.add_view(leaderboard_view)
    except Exception as e:
        print(f"Could not initialize combined leaderboard view: {e}")

    if not weekly_loot_role_rotation.is_running():
        weekly_loot_role_rotation.start()

    print("Bot is ready.")


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    if message.channel.id in (DROPS_CHANNEL_ID, DEATHS_CHANNEL_ID):
        if await process_message(message):
            await update_leaderboard()

    await bot.process_commands(message)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    """Re-process edited Dink notifications."""
    if after.author == bot.user:
        return
    if after.channel.id in (DROPS_CHANNEL_ID, DEATHS_CHANNEL_ID):
        if await process_message(after):
            await update_leaderboard()


def get_latest_big_drop_for_player(player: str):
    conn = db()
    try:
        row = conn.execute(
            """
            SELECT message_id, channel_id, loot_item, source, value_gp
            FROM events
            WHERE event_type='loot'
              AND LOWER(REPLACE(player, ' ', '')) =
                  LOWER(REPLACE(?, ' ', ''))
              AND value_gp >= ?
            ORDER BY datetime(created_at) DESC, message_id DESC
            LIMIT 1
            """,
            (player, BIG_DROP_ANNOUNCEMENT_MIN_GP),
        ).fetchone()
        return row
    finally:
        conn.close()


@bot.tree.command(
    name="announcetest",
    description="Test a milestone or big-drop announcement."
)
@app_commands.describe(
    player="OSRS player to use for the test",
    announcement="Announcement type to test",
)
@app_commands.choices(announcement=[
    app_commands.Choice(name="Loot milestone", value="loot"),
    app_commands.Choice(name="PvP kill milestone", value="pvp"),
    app_commands.Choice(name="Big drop", value="bigdrop"),
])
@app_commands.checks.has_permissions(manage_guild=True)
async def announcetest_command(
    interaction: discord.Interaction,
    player: str,
    announcement: app_commands.Choice[str],
):
    await interaction.response.defer(ephemeral=True)

    discord_id = get_linked_discord_id(player)
    mention = f"<@{discord_id}>" if discord_id is not None else None

    if announcement.value == "loot":
        total_loot = get_player_loot_total(player)
        achieved = [threshold for threshold in LOOT_MILESTONES if threshold <= total_loot]

        if not achieved:
            await interaction.followup.send(
                f"❌ **{player}** has not reached any loot milestone yet "
                f"(current total: **{format_gp(total_loot)} GP**).",
                ephemeral=True,
            )
            return

        threshold = max(achieved)
        title = "🏆 LOOT MILESTONE"
        description = (
            f"**{player}** has reached **{format_gp(threshold)} GP** "
            f"in total recorded loot! 💰"
        )
        color = discord.Color.gold()

    elif announcement.value == "pvp":
        kills = get_player_pvp_kills(player)
        achieved = [threshold for threshold in PVP_KILL_MILESTONES if threshold <= kills]

        if not achieved:
            await interaction.followup.send(
                f"❌ **{player}** has not reached any PvP kill milestone yet "
                f"(current total: **{kills:,} kills**).",
                ephemeral=True,
            )
            return

        threshold = max(achieved)
        title = "⚔️ PVP KILL MILESTONE"
        description = f"**{player}** has reached **{threshold:,} PvP kills**! ⚔️"
        color = discord.Color.red()

    else:
        drop = get_latest_big_drop_for_player(player)

        if drop is None:
            await interaction.followup.send(
                f"❌ **{player}** has no recorded drop of "
                f"**{format_gp(BIG_DROP_ANNOUNCEMENT_MIN_GP)} GP+** yet.",
                ephemeral=True,
            )
            return

        item = drop["loot_item"] or drop["source"] or "Loot drop"
        value_gp = int(drop["value_gp"] or 0)
        guild_id = interaction.guild.id if interaction.guild else "@me"
        drop_url = (
            f"https://discord.com/channels/{guild_id}/"
            f"{drop['channel_id']}/{drop['message_id']}"
        )

        title = "💎 BIG DROP!"
        description = (
            f"**{player}** just received **{item}** worth "
            f"**{format_gp(value_gp)} GP**! 🎉\n\n"
            f"[Show Drop]({drop_url})\n\n"
            f"{mention or ''}"
        )
        color = discord.Color.purple()

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    channel = bot.get_channel(WEEKLY_LOOT_ANNOUNCEMENT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(WEEKLY_LOOT_ANNOUNCEMENT_CHANNEL_ID)
        except Exception as exc:
            await interaction.followup.send(
                f"❌ Could not access the announcement channel: `{exc}`",
                ephemeral=True,
            )
            return

    await channel.send(embed=embed)

    if mention:
        await interaction.followup.send(
            f"✅ Test announcement sent using **{player}'s real data** "
            f"and {mention} was tagged.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            f"✅ Test announcement sent using **{player}'s real data**. "
            "No Discord ID is linked to this player, so nobody was tagged.",
            ephemeral=True,
        )


@bot.tree.command(
    name="roletest",
    description="Test the Weekly Loot Winner role and announcement."
)
@app_commands.describe(player="OSRS player to test")
@app_commands.checks.has_permissions(manage_guild=True)
async def roletest_command(interaction: discord.Interaction, player: str):
    await interaction.response.defer(ephemeral=True)

    try:
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "This command can only be used inside the server.",
                ephemeral=True,
            )
            return

        discord_id = get_linked_discord_id(player)
        if discord_id is None:
            await interaction.followup.send(
                f"**{player}** has no linked Discord ID. Use `/showids` to link one first.",
                ephemeral=True,
            )
            return

        # Use the same real role-assignment function as the weekly rotation.
        success, result = await grant_weekly_loot_role(player, guild)
        if not success:
            await interaction.followup.send(
                f"❌ Role test failed: {result}",
                ephemeral=True,
            )
            return

        announcement_channel = bot.get_channel(
            WEEKLY_LOOT_ANNOUNCEMENT_CHANNEL_ID
        )
        if announcement_channel is None:
            announcement_channel = await bot.fetch_channel(
                WEEKLY_LOOT_ANNOUNCEMENT_CHANNEL_ID
            )

        mention = f"<@{int(discord_id)}>"
        winner_role = await get_weekly_loot_winner_role(guild)
        if winner_role is None:
            await interaction.followup.send(
                f'❌ Role "{WEEKLY_LOOT_WINNER_ROLE_NAME}" was not found in the server.',
                ephemeral=True,
            )
            return

        # Use the exact same announcement embed as the real weekly rotation.
        completed_week = get_previous_completed_week_key()
        winner_drops = get_completed_weekly_winner_drops(
            player,
            completed_week,
            discord_id,
        )
        embed = build_weekly_winner_announcement(
            player,
            discord_id,
            completed_week,
            winner_role,
            winner_drops,
            guild.id,
        )
        embed.set_footer(text="The new weekly loot ranking has now started.")

        await announcement_channel.send(embed=embed)

        await interaction.followup.send(
            f"✅ Test successful. {mention} was given the "
            f"**{WEEKLY_LOOT_WINNER_ROLE_NAME}** role and the test announcement "
            f"was sent to <#{WEEKLY_LOOT_ANNOUNCEMENT_CHANNEL_ID}>.",
            ephemeral=True,
        )

    except Exception as exc:
        print(f"/roletest error: {type(exc).__name__}: {exc}")
        if interaction.response.is_done():
            await interaction.followup.send(
                f"❌ Role test failed: `{type(exc).__name__}: {exc}`",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"❌ Role test failed: `{type(exc).__name__}: {exc}`",
                ephemeral=True,
            )


@bot.tree.command(name="debugplayer", description="Debug stored Dink events for a player.")
@app_commands.describe(player="The exact player name to inspect")
@app_commands.checks.has_permissions(manage_guild=True)
async def debugplayer_command(interaction: discord.Interaction, player: str):
    conn = db()
    rows = conn.execute(
        """
        SELECT message_id, event_type, player, value_gp, source, loot_item, created_at
        FROM events
        WHERE LOWER(REPLACE(player, ' ', '')) = LOWER(REPLACE(?, ' ', ''))
        ORDER BY datetime(created_at) DESC, message_id DESC
        LIMIT 25
        """,
        (player,),
    ).fetchall()
    conn.close()
    if not rows:
        await interaction.response.send_message(f"No stored events found for **{player}**.", ephemeral=True)
        return
    total_loot = sum(r["value_gp"] or 0 for r in rows if r["event_type"] == "loot")
    loot_count = sum(1 for r in rows if r["event_type"] == "loot")
    deaths = sum(1 for r in rows if r["event_type"] == "death")
    death_gp = sum(r["value_gp"] or 0 for r in rows if r["event_type"] == "death")
    lines = []
    for r in rows[:15]:
        kind = "💰" if r["event_type"] == "loot" else "💀"
        extra = r["loot_item"] or r["source"] or "no item/source"
        lines.append(f"{kind} **{format_gp(r['value_gp'] or 0)} GP** — {extra}")
    embed = discord.Embed(title=f"🔎 DEBUG — {player}", description=(
        f"**Stored loot:** {loot_count} • **{format_gp(total_loot)} GP**\n"
        f"**Stored deaths:** {deaths} • **{format_gp(death_gp)} GP lost**\n\n" + "\n".join(lines)
    ), color=discord.Color.orange())
    await interaction.response.send_message(embed=embed, ephemeral=True)



@bot.tree.command(name="removeplayer", description="Remove a player and all recorded leaderboard data.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(username="The OSRS username to remove from the leaderboards")
async def removeplayer_command(interaction: discord.Interaction, username: str):
    """Permanently remove a player's recorded events and linked name aliases."""
    await interaction.response.defer(ephemeral=True)

    target_key = player_key(username)
    if not target_key:
        await interaction.followup.send("❌ Please enter a valid OSRS username.", ephemeral=True)
        return

    async with db_lock:
        conn = db()

        # Include the requested name plus any names linked through /namechange.
        keys = {target_key}
        names = {display_player_name(username)}

        changed = True
        while changed:
            changed = False
            rows = conn.execute(
                "SELECT old_key, current_name FROM player_aliases"
            ).fetchall()
            for row in rows:
                old_key = row["old_key"]
                current_name = display_player_name(row["current_name"])
                current_key = player_key(current_name)

                if old_key in keys or current_key in keys:
                    if old_key not in keys:
                        keys.add(old_key)
                        changed = True
                    if current_key and current_key not in keys:
                        keys.add(current_key)
                        changed = True
                    names.add(current_name)

        placeholders = ",".join("?" for _ in keys)

        # Delete every event belonging to the player or one of their linked names.
        deleted_events = conn.execute(
            f"""
            DELETE FROM events
            WHERE LOWER(REPLACE(player, ' ', '')) IN ({placeholders})
            """,
            tuple(keys),
        ).rowcount

        # Remove Discord ID links associated with the deleted player profile.
        deleted_discord_links = conn.execute(
            f"""
            DELETE FROM player_discord_links
            WHERE player_key IN ({placeholders})
            """,
            tuple(keys),
        ).rowcount

        # Remove aliases associated with the deleted player profile.
        deleted_aliases = conn.execute(
            f"""
            DELETE FROM player_aliases
            WHERE old_key IN ({placeholders})
               OR LOWER(REPLACE(current_name, ' ', '')) IN ({placeholders})
            """,
            tuple(keys) + tuple(keys),
        ).rowcount

        conn.commit()
        conn.close()

    await update_leaderboard()

    if deleted_events == 0 and deleted_aliases == 0:
        await interaction.followup.send(
            f"ℹ️ No leaderboard data found for **{username}**.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"✅ Removed **{username}** from the leaderboards.\n"
        f"🗑️ Deleted **{deleted_events:,}** recorded events"
        + (f", **{deleted_aliases:,}** linked name aliases" if deleted_aliases else "")
        + (f", and **{deleted_discord_links:,}** Discord ID link(s)." if deleted_discord_links else "."),
        ephemeral=True,
    )


@removeplayer_command.error
async def removeplayer_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"Remove player command error: {error}")
    if not interaction.response.is_done():
        await interaction.response.send_message(
            f"❌ Remove player failed: {error}",
            ephemeral=True,
        )


@bot.tree.command(
    name="showids",
    description="Open the menu to assign Discord IDs to leaderboard players.",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def showids_command(interaction: discord.Interaction):
    view = ShowIdsView(interaction)
    await interaction.response.send_message(
        embed=view.make_embed(),
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


@showids_command.error
async def showids_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    if isinstance(error, app_commands.errors.MissingPermissions):
        message = "❌ You need **Manage Server** permission to use `/showids`."
    else:
        message = f"❌ `/showids` failed: {error}"

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(name="leaderboard", description="Show the current clan leaderboard.")
async def leaderboard_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await update_leaderboard()
    await interaction.followup.send(
        f"Leaderboard updated in <#{LEADERBOARD_CHANNEL_ID}>.",
        ephemeral=True,
    )


def get_linked_player_accounts(discord_id: int):
    conn = db()
    try:
        rows = conn.execute(
            """
            SELECT player_name
            FROM player_discord_links
            WHERE discord_id = ?
            ORDER BY player_name COLLATE NOCASE
            """,
            (discord_id,),
        ).fetchall()
        return [row["player_name"] for row in rows]
    finally:
        conn.close()


def _progress_bar(current: int, target: int, segments: int = 12) -> str:
    """Create a compact Discord-friendly progress bar."""
    if target <= 0:
        return "████████████"
    ratio = max(0.0, min(1.0, current / target))
    filled = int(ratio * segments)
    return "█" * filled + "░" * (segments - filled)


def _next_milestone(current: int, milestones):
    for threshold in milestones:
        if current < threshold:
            return threshold
    return None


class ProfileCloseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Close",
            emoji="✖️",
            style=discord.ButtonStyle.danger,
            custom_id="profile_close",
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=None,
            embed=None,
            view=None,
            attachments=[],
        )


class PlayerProfileDropsButton(discord.ui.Button):
    def __init__(self, player: str):
        super().__init__(
            label="Show All Drops",
            emoji="💎",
            style=discord.ButtonStyle.secondary,
            custom_id=f"profile_show_all_drops:{player[:70]}",
        )
        self.player = player

    async def callback(self, interaction: discord.Interaction):
        _log_interaction_readable(interaction, "Profile Show All Drops", player=self.player)
        rows = get_player_loot_events(self.player)

        if not rows:
            await interaction.response.send_message(
                f"No recorded drops found for **{self.player}**.",
                ephemeral=True,
            )
            return

        view = PlayerDropsPages(
            player=self.player,
            rows=rows,
            owner_id=interaction.user.id,
        )
        await interaction.response.send_message(
            embed=view.build_embed(),
            view=view,
            ephemeral=True,
        )


class PlayerProfileView(discord.ui.View):
    def __init__(self, player: str):
        super().__init__(timeout=300)
        self.add_item(PlayerProfileDropsButton(player))
        self.add_item(ProfileCloseButton())


async def send_player_stats(interaction: discord.Interaction, player: str):
    row = get_player_stats(player)

    if not row or not row["player"]:
        await interaction.response.send_message(
            f"No data found for **{player}**.",
            ephemeral=True,
        )
        return

    player_name = row["player"]
    loot = int(row["loot_gp"] or 0)
    drops = int(row["loot_drops"] or 0)
    deaths = int(row["deaths"] or 0)
    death_value = int(row["death_value_gp"] or 0)
    pvp_kills = get_player_pvp_kills(player_name)
    weekly_wins = get_weekly_loot_win_count(player_name)

    linked_discord_id = get_linked_discord_id(player_name)

    # Progress toward the next lifetime loot milestone.
    next_loot = _next_milestone(loot, LOOT_MILESTONES)
    if next_loot is None:
        loot_progress = (
            "████████████ **MAX**\n"
            f"**{format_gp(loot)} GP** • All loot milestones completed"
        )
    else:
        previous_loot = 0
        for threshold in LOOT_MILESTONES:
            if loot < threshold:
                break
            previous_loot = threshold

        loot_progress = (
            f"`{_progress_bar(loot - previous_loot, next_loot - previous_loot)}`\n"
            f"**{format_gp(loot)} / {format_gp(next_loot)} GP** "
            f"• Next milestone: **{format_gp(next_loot)} GP**"
        )

    # Progress toward the next lifetime PvP milestone.
    next_pvp = _next_milestone(pvp_kills, PVP_KILL_MILESTONES)
    if next_pvp is None:
        pvp_progress = (
            "████████████ **MAX**\n"
            f"**{pvp_kills:,} kills** • All PvP milestones completed"
        )
    else:
        previous_pvp = 0
        for threshold in PVP_KILL_MILESTONES:
            if pvp_kills < threshold:
                break
            previous_pvp = threshold

        pvp_progress = (
            f"`{_progress_bar(pvp_kills - previous_pvp, next_pvp - previous_pvp)}`\n"
            f"**{pvp_kills:,} / {next_pvp:,} kills** "
            f"• Next milestone: **{next_pvp:,} kills**"
        )

    embed = discord.Embed(
        title=f"👤 {player_name}",
        description="**PLAYER PROFILE**\n━━━━━━━━━━━━━━━━━━━━",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(
        name="💰 LOOT PROGRESS",
        value=loot_progress,
        inline=False,
    )

    embed.add_field(
        name="⚔️ PVP PROGRESS",
        value=pvp_progress,
        inline=False,
    )

    embed.add_field(
        name="📊 STATISTICS",
        value=(
            f"💰 **Total Loot:** {format_gp(loot)} GP\n"
            f"🎁 **Loot Drops:** {drops:,}\n"
            f"💀 **Deaths:** {deaths:,}\n"
            f"💸 **PvP GP Lost:** {format_gp(death_value)} GP\n"
            f"⚔️ **PvP Kills:** {pvp_kills:,}\n"
            f"🏆 **Weekly Loot Wins:** {weekly_wins:,}"
        ),
        inline=False,
    )

    if linked_discord_id:
        embed.add_field(
            name="🔗 DISCORD",
            value=f"<@{linked_discord_id}>",
            inline=True,
        )

        linked_accounts = get_linked_player_accounts(int(linked_discord_id))
        if linked_accounts:
            accounts_text = "\n".join(
                f"• **{account}**" for account in linked_accounts
            )
            embed.add_field(
                name="👥 LINKED OSRS ACCOUNTS",
                value=accounts_text,
                inline=False,
            )
    else:
        embed.add_field(
            name="🔗 DISCORD",
            value="Not linked",
            inline=True,
        )

    embed.set_footer(text="Personal Dink statistics")

    await interaction.response.send_message(
        embed=embed,
        view=PlayerProfileView(player_name),
        ephemeral=True,
    )


@bot.tree.command(name="namechange", description="Merge an old OSRS username into a new username.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    old_name="The player's previous OSRS username",
    new_name="The player's new OSRS username",
)
async def namechange_command(
    interaction: discord.Interaction,
    old_name: str,
    new_name: str,
):
    old_name = display_player_name(old_name)
    new_name = display_player_name(new_name)

    if not old_name or not new_name:
        await interaction.response.send_message(
            "Both the old and new username are required.",
            ephemeral=True,
        )
        return

    old_key = player_key(old_name)
    new_key = player_key(new_name)

    if old_key == new_key:
        await interaction.response.send_message(
            "The old and new username are the same.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    async with db_lock:
        conn = db()
        try:
            # Preserve the user's chosen current spelling.
            # Any existing aliases that pointed at the old profile are moved
            # to the new profile as well, so chains remain intact.
            conn.execute(
                """
                UPDATE player_aliases
                SET current_name = ?
                WHERE old_key = ?
                """,
                (new_name, old_key),
            )

            # If the old name was itself an alias, make that alias point to
            # the new canonical name. Otherwise create it.
            conn.execute(
                """
                INSERT INTO player_aliases(old_key, current_name)
                VALUES(?, ?)
                ON CONFLICT(old_key) DO UPDATE SET current_name=excluded.current_name
                """,
                (old_key, new_name),
            )

            # Merge every historical event under the old name into the new
            # profile. This also merges cleanly if the new name already has
            # existing loot/death events.
            cur = conn.execute(
                """
                UPDATE events
                SET player = ?
                WHERE LOWER(REPLACE(player, ' ', '')) = ?
                """,
                (new_name, old_key),
            )
            merged_events = cur.rowcount

            # Move an existing Discord link from the old profile to the new
            # profile so /namechange does not break role assignment.
            old_discord = conn.execute(
                "SELECT discord_id FROM player_discord_links WHERE player_key = ?",
                (old_key,),
            ).fetchone()
            if old_discord:
                conn.execute(
                    """
                    INSERT INTO player_discord_links(player_key, player_name, discord_id)
                    VALUES(?, ?, ?)
                    ON CONFLICT(player_key) DO UPDATE SET
                        player_name = excluded.player_name,
                        discord_id = excluded.discord_id
                    """,
                    (new_key, new_name, int(old_discord["discord_id"])),
                )
                conn.execute(
                    "DELETE FROM player_discord_links WHERE player_key = ?",
                    (old_key,),
                )

            # Any aliases which ultimately pointed to the old profile should
            # now resolve directly to the new profile.
            conn.execute(
                """
                UPDATE player_aliases
                SET current_name = ?
                WHERE LOWER(REPLACE(current_name, ' ', '')) = ?
                  AND old_key <> ?
                """,
                (new_name, old_key, new_key),
            )

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    await update_leaderboard()
    await interaction.followup.send(
        f"✅ Name change applied: **{old_name}** → **{new_name}**\n"
        f"📦 Merged **{merged_events:,}** historical event(s) into **{new_name}**.\n"
        f"🔗 Future drops and deaths reported under **{old_name}** will now be "
        f"added to **{new_name}**.",
        ephemeral=True,
    )


@namechange_command.error
async def namechange_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    print(f"Name change command error: {error}")
    if not interaction.response.is_done():
        await interaction.response.send_message(
            f"❌ Name change failed: {error}",
            ephemeral=True,
        )


@bot.tree.command(name="refreshnames", description="Refresh displayed player names from the latest stored Dink event.")
@app_commands.checks.has_permissions(manage_guild=True)
async def refreshnames_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    conn = db()
    # For each case/space-insensitive player group, copy the latest-seen spelling
    # to older rows. This changes display formatting only; totals/events remain intact.
    groups = conn.execute(
        """
        SELECT LOWER(REPLACE(player, ' ', '')) AS pkey
        FROM events
        GROUP BY LOWER(REPLACE(player, ' ', ''))
        """
    ).fetchall()

    changed = 0
    for g in groups:
        pkey = g["pkey"]
        latest = conn.execute(
            """
            SELECT player FROM events
            WHERE LOWER(REPLACE(player, ' ', '')) = ?
            ORDER BY datetime(created_at) DESC, message_id DESC
            LIMIT 1
            """,
            (pkey,),
        ).fetchone()
        if not latest:
            continue
        cur = conn.execute(
            """
            UPDATE events SET player = ?
            WHERE LOWER(REPLACE(player, ' ', '')) = ?
              AND player <> ?
            """,
            (latest["player"], pkey, latest["player"]),
        )
        changed += cur.rowcount
    conn.commit()
    conn.close()

    await update_leaderboard()
    await interaction.followup.send(
        f"✅ Refreshed **{changed:,}** stored player-name entries using the latest Dink spelling.",
        ephemeral=True,
    )


@refreshnames_command.error
async def refreshnames_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    msg = "You need **Manage Server** permission to use this command."
    if isinstance(error, app_commands.errors.MissingPermissions):
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    else:
        print(f"Refresh names command error: {error}")


@bot.tree.command(name="player", description="Show detailed stats for a player.")
@app_commands.describe(
    player="OSRS player name (type it manually)",
    member="Discord member linked to the OSRS player",
)
async def player_command(
    interaction: discord.Interaction,
    player: str | None = None,
    member: discord.Member | None = None,
):
    # Allow either an OSRS name or a Discord member lookup.
    if member is not None:
        discord_id = member.id
        conn = db()
        row = conn.execute(
            """
            SELECT player_name
            FROM player_discord_links
            WHERE discord_id = ?
            ORDER BY player_name COLLATE NOCASE
            LIMIT 1
            """,
            (discord_id,),
        ).fetchone()
        conn.close()

        if row is None:
            await interaction.response.send_message(
                f"❌ {member.mention} does not have a linked OSRS player.",
                ephemeral=True,
            )
            return

        await send_player_stats(interaction, row["player_name"])
        return

    if not player:
        await interaction.response.send_message(
            "❌ Please provide an OSRS player name or select a Discord member.",
            ephemeral=True,
        )
        return

    await send_player_stats(interaction, player)


@bot.tree.command(name="backfill", description="Import existing Dink messages from DROPS and DEATHS.")
@app_commands.checks.has_permissions(manage_guild=True)
async def backfill_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    drops = await backfill_channel(DROPS_CHANNEL_ID)
    deaths = await backfill_channel(DEATHS_CHANNEL_ID)

    try:
        await update_leaderboard()
        leaderboard_status = "✅ Leaderboards updated."
    except Exception as e:
        leaderboard_status = f"⚠️ Leaderboard update error: {type(e).__name__}: {e}"
        print(f"Backfill leaderboard update error: {type(e).__name__}: {e}")

    await interaction.followup.send(
        f"Backfill complete.\n"
        f"💰 Processed {drops} loot events.\n"
        f"💀 Processed {deaths} death events.\n"
        f"{leaderboard_status}",
        ephemeral=True,
    )


@backfill_command.error
async def backfill_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    if isinstance(error, app_commands.errors.MissingPermissions):
        if interaction.response.is_done():
            await interaction.followup.send(
                "You need **Manage Server** permission to use this command.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "You need **Manage Server** permission to use this command.",
                ephemeral=True,
            )
    else:
        print(f"Backfill command error: {error}")


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN is missing. Put your bot token in the .env file."
        )

    init_db()
    bot.run(TOKEN)
