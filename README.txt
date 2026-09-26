Dink Discord Leaderboard Bot v3

Features:
- Separate 💰 Loot Leaderboard
- Separate 💀 Death Leaderboard
- Separate 🎯 PvM Completions Leaderboard
- Separate 💎 Biggest Drops leaderboard (top 10 individual drops)
- /leaderboard refreshes all four messages
- /player <name> shows detailed player statistics
- /stats <name> remains available as an alias
- /backfill imports existing Dink messages
- SQLite database preserves history

IMPORTANT: Keep your existing leaderboard.db when updating from an older version.
Keep your .env file and do not share the bot token.

Install/update:
1. Stop the old bot with Ctrl+C.
2. Replace bot.py with this version (or extract the ZIP over the old folder).
3. Keep your existing .env and leaderboard.db.
4. Run: pip install -r requirements.txt
5. Run: python bot.py

You do NOT need to run /backfill again if the existing database is intact.
The completion leaderboard uses the highest recorded Dink completion count per player/activity instead of summing cumulative counts.
