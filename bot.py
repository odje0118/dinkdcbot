import os
import re
import sqlite3
import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DROPS_CHANNEL_ID = 1540706808262430792
DEATHS_CHANNEL_ID = 1540800494547640420
LEADERBOARD_CHANNEL_ID = 1553383319696048208
WEEKLY_LOOT_WINNER_ROLE_NAME = "Weekly Loot Winner"

DB_FILE = os.getenv("DB_FILE", "leaderboard.db")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
db_lock = asyncio.Lock()
update_lock = asyncio.Lock()


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

        CREATE TABLE IF NOT EXISTS player_discord_links (
            player_key TEXT PRIMARY KEY,
            player_name TEXT NOT NULL,
            discord_id INTEGER NOT NULL
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


async def save_event(message: discord.Message, parsed: dict) -> bool:
    async with db_lock:
        conn = db()
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO events
            (message_id, channel_id, event_type, player, value_gp,
             completion_count, source, loot_item, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.id,
                message.channel.id,
                parsed["event_type"],
                parsed["player"],
                parsed["value_gp"],
                parsed["completion_count"],
                parsed["source"],
                parsed.get("loot_item", ""),
                message.created_at.isoformat(),
            ),
        )
        inserted = cur.rowcount == 1

        # Repair drops that were imported before source parsing was fixed.
        if not inserted and parsed["event_type"] == "loot":
            updates = []
            params = []

            if parsed.get("source"):
                updates.append("source = ?")
                params.append(parsed["source"])

            if parsed.get("loot_item"):
                updates.append("loot_item = ?")
                params.append(parsed["loot_item"])

            if updates:
                params.extend([message.id, message.channel.id])
                conn.execute(
                    f"""
                    UPDATE events
                    SET {", ".join(updates)}
                    WHERE message_id = ?
                      AND channel_id = ?
                      AND event_type = 'loot'
                    """,
                    params,
                )
                conn.commit()

        conn.close()
    return inserted


async def process_message(message: discord.Message) -> bool:
    if message.channel.id == DROPS_CHANNEL_ID:
        parsed = parse_loot(message)
    elif message.channel.id == DEATHS_CHANNEL_ID:
        parsed = parse_death(message)
    else:
        return False

    if not parsed:
        return False

    return await save_event(message, parsed)


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


def get_weekly_loot_stats(limit=15):
    """Return loot totals for the current Monday-Sunday week."""
    conn = db()
    rows = conn.execute(
        """
        WITH grouped AS (
            SELECT
                LOWER(REPLACE(player, ' ', '')) AS pkey,
                SUM(value_gp) AS loot_gp,
                COUNT(*) AS loot_drops
            FROM events
            WHERE event_type='loot'
              AND datetime(created_at) >= datetime('now', 'localtime', 'weekday 1', '-7 days')
              AND datetime(created_at) < datetime('now', 'localtime', 'weekday 1')
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
                  ORDER BY datetime(e2.created_at) DESC, e2.message_id DESC
                  LIMIT 1
              )
        )
        SELECT n.display_name AS player, g.loot_gp, g.loot_drops
        FROM grouped g
        JOIN latest_names n ON n.pkey = g.pkey
        ORDER BY g.loot_gp DESC, n.display_name COLLATE NOCASE
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return rows


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


class PlayerDropsPaginationView(discord.ui.View):
    def __init__(self, embeds):
        super().__init__(timeout=300)
        self.embeds = embeds
        self.current_page = 0
        self.previous_button = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary, emoji="◀️")
        self.next_button = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary, emoji="▶️")
        self.page_button = discord.ui.Button(label=f"Page 1/{len(embeds)}", style=discord.ButtonStyle.primary, disabled=True)

        self.previous_button.callback = self.previous_page
        self.next_button.callback = self.next_page
        self.add_item(self.previous_button)
        self.add_item(self.page_button)
        self.add_item(self.next_button)
        self.update_buttons()

    def update_buttons(self):
        self.previous_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= len(self.embeds) - 1
        self.page_button.label = f"Page {self.current_page + 1}/{len(self.embeds)}"

    async def previous_page(self, interaction: discord.Interaction):
        if self.current_page > 0:
            self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

    async def next_page(self, interaction: discord.Interaction):
        if self.current_page < len(self.embeds) - 1:
            self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

    async def on_timeout(self):
        self.previous_button.disabled = True
        self.next_button.disabled = True
        self.page_button.disabled = True


class ShowAllDropsSelect(discord.ui.Select):
    def __init__(self, players):
        options = [
            discord.SelectOption(
                label=player[:100],
                value=player[:100],
                description="View all recorded drops"[:100],
            )
            for player in players[:25]
        ]
        super().__init__(
            placeholder="Choose a player...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="leaderboard_show_all_drops",
        )

    async def callback(self, interaction: discord.Interaction):
        player = self.values[0]
        rows = get_player_loot_events(player)

        if not rows:
            await interaction.response.send_message(
                f"No recorded drops found for **{player}**.",
                ephemeral=True,
            )
            return

        total = sum(row["value_gp"] or 0 for row in rows)
        guild_id = interaction.guild_id
        chunks = []
        current = []

        for row in rows:
            item = row["loot_item"] or "Unknown item"
            source = f" • {row['source']}" if row["source"] else ""
            jump_url = (
                f"https://discord.com/channels/{guild_id}/"
                f"{row['channel_id']}/{row['message_id']}"
            )

            line = (
                f"💎 **{format_gp(row['value_gp'] or 0)} GP** — "
                f"**{item}**{source} • [Show drop]({jump_url})"
            )

            candidate = line if not current else "\n".join(current + [line])
            if current and len(candidate) > 3800:
                chunks.append("\n".join(current))
                current = [line]
            else:
                current.append(line)

        if current:
            chunks.append("\n".join(current))

        embeds = []
        for page_index, chunk in enumerate(chunks):
            header = (
                f"**{len(rows):,} {'drop' if len(rows) == 1 else 'drops'}** • "
                f"**{format_gp(total)} GP** total\n"
                f"⚠️ Only Dink drops of **500K GP+** are recorded.\n\n"
            )
            page_embed = discord.Embed(
                title=f"💎 {player} — ALL DROPS",
                description=header + chunk,
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            page_embed.set_footer(
                text=f"Page {page_index + 1}/{len(chunks)} • Updated automatically"
            )
            embeds.append(page_embed)

        # Keep all pages in one ephemeral message and navigate between them
        # with Previous/Next buttons instead of sending multiple messages.
        view = PlayerDropsPaginationView(embeds)
        await interaction.response.send_message(
            embed=embeds[0],
            view=view,
            ephemeral=True,
        )


class ShowAllDropsView(discord.ui.View):
    def __init__(self, players):
        super().__init__(timeout=None)
        self.add_item(ShowAllDropsSelect(players))


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


def get_linked_discord_id(player: str):
    conn = db()
    row = conn.execute(
        "SELECT discord_id FROM player_discord_links WHERE player_key = ?",
        (player_key(player),),
    ).fetchone()
    conn.close()
    return int(row["discord_id"]) if row else None


def set_linked_discord_id(player: str, discord_id: int):
    conn = db()
    conn.execute(
        """
        INSERT INTO player_discord_links (player_key, player_name, discord_id)
        VALUES (?, ?, ?)
        ON CONFLICT(player_key) DO UPDATE SET
            player_name = excluded.player_name,
            discord_id = excluded.discord_id
        """,
        (player_key(player), display_player_name(player), discord_id),
    )
    conn.commit()
    conn.close()


async def get_weekly_loot_winner():
    rows = get_weekly_loot_stats(1)
    return rows[0]["player"] if rows else None


async def get_weekly_loot_winner_role(guild: discord.Guild):
    return discord.utils.get(guild.roles, name=WEEKLY_LOOT_WINNER_ROLE_NAME)


async def remove_weekly_loot_role_from_others(guild: discord.Guild, keep_member_id: int | None = None):
    role = await get_weekly_loot_winner_role(guild)
    if role is None:
        return None

    removed = 0
    for member in role.members:
        if keep_member_id is not None and member.id == keep_member_id:
            continue
        try:
            await member.remove_roles(role, reason="Weekly Loot Winner rotation")
            removed += 1
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"Could not remove Weekly Loot Winner role from {member}: {e}")
    return removed


async def grant_weekly_loot_role(player: str, guild: discord.Guild):
    role = await get_weekly_loot_winner_role(guild)
    if role is None:
        return False, f'Role "{WEEKLY_LOOT_WINNER_ROLE_NAME}" was not found in the server.'

    discord_id = get_linked_discord_id(player)
    if discord_id is None:
        return False, f"No Discord ID is linked to **{player}**. Use `/lbadd {player} <discord id>` first."

    member = guild.get_member(discord_id)
    if member is None:
        try:
            member = await guild.fetch_member(discord_id)
        except (discord.NotFound, discord.HTTPException):
            return False, f"Could not find Discord member `{discord_id}` for **{player}**."

    me = guild.me
    if me is not None and role >= me.top_role:
        return False, f'The role "{role.name}" is higher than or equal to my highest role, so I cannot manage it.'

    await remove_weekly_loot_role_from_others(guild, keep_member_id=member.id)
    try:
        await member.add_roles(role, reason=f"Weekly Loot Winner: {player}")
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"Could not grant Weekly Loot Winner role to {member}: {e}")
        return False, f"I could not grant the role to **{player}**: `{e}`"

    return True, f'🏆 **{player}** now has the **{role.name}** role.'


@tasks.loop(hours=1)
async def weekly_loot_role_rotation():
    try:
        channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(LEADERBOARD_CHANNEL_ID)
        guild = getattr(channel, "guild", None)
        if guild is None:
            return

        winner = await get_weekly_loot_winner()
        if not winner:
            return

        success, message = await grant_weekly_loot_role(winner, guild)
        if not success:
            print(f"Weekly Loot Winner rotation: {message}")
        else:
            print(f"Weekly Loot Winner rotation: {message}")
    except Exception as e:
        print(f"Weekly Loot Winner rotation error: {type(e).__name__}: {e}")


@weekly_loot_role_rotation.before_loop
async def before_weekly_loot_role_rotation():
    await bot.wait_until_ready()


async def update_leaderboard():
    async with update_lock:
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

        # Remove the old combined leaderboard and the old global "Biggest Drops" message.
        await remove_old_combined_leaderboard(channel)
        await delete_leaderboard_message(channel, "biggest_drops_message_id")

        # -------------------- LOOT LEADERBOARD --------------------
        loot_embed = discord.Embed(
            title="💰 LOOT LEADERBOARD",
            description="━━━━━━━━━━━━━━━━━━━━\n**TOTAL LOOT RANKING**\n━━━━━━━━━━━━━━━━━━━━\nHighest recorded loot value per player.\n\n⚠️ Only Dink drops of **500K GP+** are recorded.",
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
                prefix = medals[i-1] if i <= 3 else f"**{i}.**"
                count = row["loot_drops"] or 0
                lines.append(
                    f"{prefix} **{row['player']}** — **{format_gp(row['loot_gp'] or 0)} GP** "
                    f"↳ **{count:,} {'drop' if count == 1 else 'drops'}**"
                )
            total_loot = sum(r["loot_gp"] or 0 for r in loot_rows)
            total_drops = sum(r["loot_drops"] or 0 for r in loot_rows)
            loot_embed.add_field(
                name="📊 GROUP TOTALS",
                value=f"💰 **{format_gp(total_loot)} GP** total loot   •   🎁 **{total_drops:,}** drops\n\n",
                inline=False,
            )
            add_chunked_field(loot_embed, "🏆 TOP LOOTERS", lines)

            weekly_lines = []
            weekly_medals = ["🥇", "🥈", "🥉"]
            for i, row in enumerate(weekly_loot_rows, start=1):
                prefix = weekly_medals[i-1] if i <= 3 else f"**{i}.**"
                count = row["loot_drops"] or 0
                weekly_lines.append(
                    f"{prefix} **{row['player']}** — **{format_gp(row['loot_gp'] or 0)} GP** "
                    f"↳ **{count:,} {'drop' if count == 1 else 'drops'}**"
                )

            weekly_header = (
                "━━━━━━━━━━━━━━━━━━━━\n"
                "**WEEKLY LOOT RANKING**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Highest recorded loot value per player this week.\n"
                "*Resets every Monday*\n\n"
            )

            if weekly_lines:
                # Format this section the same way as TOTAL LOOT RANKING:
                # separator, bold heading, separator, description, then rankings.
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
            description="━━━━━━━━━━━━━━━━━━━━\n**MOST DEATHS**\n━━━━━━━━━━━━━━━━━━━━\nPlayer deaths reported, ranked by death count.",
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
                prefix = medals[i-1] if i <= 3 else f"**{i}.**"
                lost = row["death_value_gp"] or 0
                suffix = f"\n　↳ 💸 {format_gp(lost)} GP PvP loss" if lost else ""
                lines.append(f"{prefix} **{row['player']}** — **{row['deaths']:,} deaths**{suffix}")
            add_chunked_field(death_embed, "Most Deaths", lines)
            total_deaths = sum(r["deaths"] or 0 for r in death_rows)
            total_loss = sum(r["death_value_gp"] or 0 for r in death_rows)
            death_embed.add_field(
                name="📊 GROUP TOTALS",
                value=f"💀 **{total_deaths:,}** deaths   •   💸 **{format_gp(total_loss)} GP** lost in PvP",
                inline=False,
            )
        else:
            death_embed.description = "No deaths have been imported yet."

        # -------------------- BIGGEST DROP PER PLAYER --------------------
        biggest_player_embed = discord.Embed(
            title="💎 BIGGEST DROP PER PLAYER",
            description="━━━━━━━━━━━━━━━━━━━━\n**PERSONAL RECORD DROPS**\n━━━━━━━━━━━━━━━━━━━━\nEach player's single most valuable recorded drop.\n\n⚠️ Only Dink drops of **500K GP+** are recorded.",
            color=discord.Color.purple(),
            timestamp=datetime.now(timezone.utc),
        )
        if biggest_per_player_rows:
            lines = []
            medals = ["🥇", "🥈", "🥉"]
            guild_id = getattr(getattr(channel, "guild", None), "id", None)
            for i, row in enumerate(biggest_per_player_rows, start=1):
                prefix = medals[i-1] if i <= 3 else f"**{i}.**"
                item = f" • {row['loot_item']}" if row["loot_item"] else ""
                source = f" • {row['source']}" if row["source"] else ""
                jump_url = (
                    f"https://discord.com/channels/{guild_id}/"
                    f"{row['channel_id']}/{row['message_id']}"
                    if guild_id else "https://discord.com"
                )
                lines.append(
                    f"{prefix} **{row['player']}** — **{format_gp(row['value_gp'])} GP**{item} • [Show drop]({jump_url})"
                )
            # One list in the embed description — no (2/3), (3/3) field labels.
            biggest_player_embed.description = "\n".join(lines)
        else:
            biggest_player_embed.description = "No loot drops have been imported yet."

        # -------------------- MOST GP BY ACTIVITY --------------------
        activity_embed = discord.Embed(
            title="📍 MOST GP EARNED AT",
            description="━━━━━━━━━━━━━━━━━━━━\n**TOP ACTIVITY PER PLAYER**\n━━━━━━━━━━━━━━━━━━━━\nThe activity where each player has earned the most recorded GP.",
            color=discord.Color.teal(),
            timestamp=datetime.now(timezone.utc),
        )
        if top_activity_rows:
            lines = []
            medals = ["🥇", "🥈", "🥉"]
            for i, row in enumerate(top_activity_rows, start=1):
                prefix = medals[i-1] if i <= 3 else f"**{i}.**"
                lines.append(
                    f"{prefix} **{row['player']}** — **{format_gp(row['loot_gp'] or 0)} GP**"
                    f" • {row['source']} ({row['loot_drops'] or 0:,} {'drop' if (row['loot_drops'] or 0) == 1 else 'drops'})"
                )
            activity_embed.description = "\n".join(lines)
        else:
            activity_embed.description = "No loot drops have been imported yet."
        for _embed in (loot_embed, death_embed, biggest_player_embed, activity_embed):
            _embed.set_footer(text="Updated automatically")

        # Update the four current leaderboard messages.
        leaderboard_messages = [
            ("loot_leaderboard_message_id", loot_embed),
            ("death_leaderboard_message_id", death_embed),
            ("biggest_drop_per_player_message_id", biggest_player_embed),
            ("top_activity_message_id", activity_embed),
        ]

        loot_players = [row["player"] for row in loot_rows[:25]]
        loot_view = ShowAllDropsView(loot_players)

        for setting_key, embed in leaderboard_messages:
            try:
                view = loot_view if setting_key == "loot_leaderboard_message_id" else None
                await get_or_create_leaderboard_message(channel, setting_key, embed, view=view)
            except discord.HTTPException as e:
                print(f"Could not update '{setting_key}': {e}")


async def backfill_channel(channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)

    imported = 0
    async for message in channel.history(limit=None, oldest_first=True):
        if message.webhook_id is None:
            # Dink posts should normally be webhooks. We still parse it
            # because some setups may relay messages through an app bot.
            pass

        if await process_message(message):
            imported += 1

    return imported


@bot.event
async def on_ready():
    init_db()
    try:
        # Sync to each connected guild as well as globally so newly added
        # slash commands appear immediately in the servers where the bot is installed.
        synced = await bot.tree.sync()
        print(f"Logged in as {bot.user}. Synced {len(synced)} global slash commands.")

        for guild in bot.guilds:
            bot.tree.copy_global_to(guild=guild)
            guild_synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(guild_synced)} guild slash commands to {guild.name} ({guild.id}).")
    except Exception as e:
        print(f"Slash command sync failed: {type(e).__name__}: {e}")

    try:
        stats = get_stats()
        loot_players = [
            row["player"]
            for row in sorted(
                [r for r in stats if (r["loot_gp"] or 0) > 0],
                key=lambda r: (r["loot_gp"] or 0),
                reverse=True,
            )[:25]
        ]
        bot.add_view(ShowAllDropsView(loot_players))
    except Exception as e:
        print(f"Could not register loot player dropdown: {e}")

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


@bot.tree.command(name="lbadd", description="Link a leaderboard player to a Discord member ID.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(player="The exact leaderboard player name", discord_id="The Discord user ID to link to this player")
async def lbadd_command(interaction: discord.Interaction, player: str, discord_id: str):
    try:
        member_id = int(discord_id.strip())
    except ValueError:
        await interaction.response.send_message("❌ The Discord ID must be a numeric Discord user ID.", ephemeral=True)
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
        return

    try:
        member = guild.get_member(member_id)
        if member is None:
            member = await guild.fetch_member(member_id)
    except (discord.NotFound, discord.HTTPException):
        await interaction.response.send_message(f"❌ Could not find Discord member `{member_id}` in this server.", ephemeral=True)
        return

    set_linked_discord_id(player, member.id)
    await interaction.response.send_message(
        f"✅ Linked leaderboard player **{player}** to {member.mention} (`{member.id}`).",
        ephemeral=True,
    )


@lbadd_command.error
async def lbadd_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.errors.MissingPermissions):
        msg = "You need **Manage Server** permission to use this command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    else:
        print(f"lbadd command error: {error}")


@bot.tree.command(name="lbrole", description="Immediately give a player the Weekly Loot Winner role.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(player="The exact leaderboard player name")
async def lbrole_command(interaction: discord.Interaction, player: str):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("❌ This command can only be used in a server.", ephemeral=True)
        return

    success, message = await grant_weekly_loot_role(player, guild)
    await interaction.followup.send(("✅ " if success else "❌ ") + message, ephemeral=True)


@lbrole_command.error
async def lbrole_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.errors.MissingPermissions):
        msg = "You need **Manage Server** permission to use this command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    else:
        print(f"lbrole command error: {error}")


@bot.tree.command(name="leaderboard", description="Show the current clan leaderboard.")
async def leaderboard_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await update_leaderboard()
    await interaction.followup.send(
        f"Leaderboard updated in <#{LEADERBOARD_CHANNEL_ID}>.",
        ephemeral=True,
    )


async def send_player_stats(interaction: discord.Interaction, player: str):
    row = get_player_stats(player)

    if not row or not row['player']:
        await interaction.response.send_message(
            f"No data found for **{player}**.",
            ephemeral=True,
        )
        return

    loot = row["loot_gp"] or 0
    drops = row["loot_drops"] or 0
    deaths = row["deaths"] or 0
    death_value = row["death_value_gp"] or 0
    completion_rows = get_player_completions(row["player"])
    completions = sum(r["completions"] or 0 for r in completion_rows)

    embed = discord.Embed(
        title=f"📊 {row['player']}",
        description="Personal Dink statistics",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="💰 Total Loot", value=f"**{format_gp(loot)} GP**", inline=True)
    embed.add_field(name="🎁 Loot Drops", value=f"**{drops:,}**", inline=True)
    embed.add_field(name="💀 Deaths", value=f"**{deaths:,}**", inline=True)
    embed.add_field(name="💸 PvP GP Lost", value=f"**{format_gp(death_value)} GP**", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)



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


@bot.tree.command(name="stats", description="Show stats for a player.")
@app_commands.describe(player="The exact player name")
async def stats_command(interaction: discord.Interaction, player: str):
    await send_player_stats(interaction, player)


@bot.tree.command(name="player", description="Show detailed stats for a player.")
@app_commands.describe(player="The exact player name")
async def player_command(interaction: discord.Interaction, player: str):
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
        f"💰 Imported {drops} new loot events.\n"
        f"💀 Imported {deaths} new death events.\n"
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
