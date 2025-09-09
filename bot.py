"""
Emoji Pair Finder Game (Aiogram v2 + MongoDB)

Features:
- /start, /new, /play, /profile, /chat, /privacy, /leaderboard, /language, /broadcast (admin)
- Grid sizes: 5x5 | 5x8 | 5x12 (5 rows x N cols)
- Cooldown anti-spam: 1 second per user between guesses
- MongoDB collections: users, chats, games
- Inline keyboard game UI with callback_data
- Uses pymongo for DB
"""

import logging
import os
import time
import random
from functools import wraps
from typing import List, Dict, Any

from aiogram import Bot, Dispatcher, types, executor
from aiogram.utils.callback_data import CallbackData
from pymongo import MongoClient
from dotenv import load_dotenv
import emoji

load_dotenv()

# Config
BOT_TOKEN = os.getenv("BOT_TOKEN")  # Telegram bot token
MONGO_URI = os.getenv("MONGO_URI")  # MongoDB URI
OWNER_ID = int(os.getenv("OWNER_ID") or 0)  # Admin for broadcast
COOLDOWN_SECONDS = 1.0

if not BOT_TOKEN or not MONGO_URI:
    raise RuntimeError("Please set BOT_TOKEN and MONGO_URI environment variables (and optionally OWNER_ID).")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# Mongo
mongo = MongoClient(MONGO_URI)
db = mongo.get_database("emoji_pair_finder")
users_coll = db.users
chats_coll = db.chats
games_coll = db.games  # active and past games

# Callback data factories
game_cb = CallbackData("game", "game_id", "action", "index")  # game:<id>:action:<index>
size_cb = CallbackData("size", "game_id", "size")  # size:<id>:size
lang_cb = CallbackData("lang", "lang")  # lang:<code>

# Emoji pool (all emojis from emoji lib)
ALL_EMOJIS = list(emoji.EMOJI_DATA.keys())
EMOJI_POOL = [e for e in ALL_EMOJIS if len(e) <= 2]

# Default language
DEFAULT_LANG = "en"
LANG_FLAGS = {
    "en": "🇺🇸",
    "in": "🇮🇳",
    "ru": "🇷🇺",
    "tr": "🇹🇷",
    "id": "🇮🇩",
    "br": "🇧🇷",
    "mx": "🇲🇽",
    "ua": "🇺🇦",
}

# Utility functions
def get_user_doc(user: types.User) -> Dict[str, Any]:
    doc = users_coll.find_one({"user_id": user.id})
    if not doc:
        doc = {
            "user_id": user.id,
            "username": user.username or "",
            "games_played": 0,
            "pairs_found": 0,
            "total_points": 0,
            "best_score": 0,
            "language": DEFAULT_LANG,
            "cooldown_until": 0.0,
            "chats": []
        }
        users_coll.insert_one(doc)
    return doc

def update_user_stats(user_id: int, **fields):
    users_coll.update_one({"user_id": user_id}, {"$inc": {k: v for k, v in fields.items() if isinstance(v, (int,float))}}, upsert=True)

def upsert_user_profile(user: types.User):
    users_coll.update_one(
        {"user_id": user.id},
        {"$set": {"username": user.username or ""}, "$setOnInsert": {"language": DEFAULT_LANG}},
        upsert=True
    )

def ensure_chat_record(chat: types.Chat):
    chats_coll.update_one({"chat_id": chat.id}, {"$set": {"title": chat.title or chat.first_name or ""}}, upsert=True)

def require_owner(func):
    @wraps(func)
    async def wrapper(message: types.Message, *args, **kwargs):
        if message.from_user.id != OWNER_ID:
            await message.reply("You are not authorized to use this command.")
            return
        return await func(message, *args, **kwargs)
    return wrapper

def generate_game_grid(rows: int, cols: int):
    """
    Returns:
    - grid: list of dicts {emoji, matched(bool)}
    """
    total = rows * cols
    pair_count = total // 2
    emojis = random.sample(EMOJI_POOL, k=pair_count)
    deck = []
    for e in emojis:
        deck.append(e)
        deck.append(e)
    if total % 2 == 1:
        filler = "⬜"
        deck.append(filler)
    else:
        filler = None
    random.shuffle(deck)
    grid = [{"emoji": deck[i], "matched": False} for i in range(total)]
    return grid, filler

# Command handlers
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    upsert_user_profile(message.from_user)
    ensure_chat_record(message.chat)
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("▶️ Start Game", callback_data="start_new"))
    kb.add(types.InlineKeyboardButton("🎲 How to Play", callback_data="how_play"))
    kb.add(types.InlineKeyboardButton("👤 Owner & Support", url=f"https://t.me/{(os.getenv('OWNER_USERNAME') or 'your_owner_username')}"))
    kb.add(types.InlineKeyboardButton("🌐 Change Language", callback_data="open_lang"))
    kb.add(types.InlineKeyboardButton("➕ Add Me to Chat", url=f"https://t.me/{(os.getenv('BOT_USERNAME') or 'YourBot')}?startgroup=true"))
    text = ("🎮 Welcome to Emoji Pair Finder Game!\n"
            "Match the emoji pairs and score points.\n"
            "Play alone in DM or invite friends in your groups.")
    await message.answer(text, reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data == "how_play")
async def cb_how_play(callback: types.CallbackQuery):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    text = ("📖 *How to Play*\n\n"
            "1. Start a new game with /new or the Start Game button.\n"
            "2. Choose a grid size: 5x5, 5x8, or 5x12.\n"
            "3. The grid shows random emojis.\n"
            "4. Tap emojis to match pairs:\n"
            "   ✅ Matching pair = +10 points.\n"
            "   ❌ Wrong pair = no penalty.\n"
            "5. First to finish all pairs wins!\n"
            "6. Anti-spam: 1 second cooldown between taps.")
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data == "back_main")
async def cb_back_main(callback: types.CallbackQuery):
    await cmd_start(callback.message)

@dp.callback_query_handler(lambda c: c.data == "start_new")
async def cb_start_new(callback: types.CallbackQuery):
    await callback.answer()
    await start_new_game_flow(callback.message, callback.from_user)

@dp.message_handler(commands=["new", "play"])
async def cmd_new(message: types.Message):
    await start_new_game_flow(message, message.from_user)

async def start_new_game_flow(message_or: types.Message, user: types.User):
    game_id = str(int(time.time() * 1000)) + "_" + str(user.id)
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(types.InlineKeyboardButton("5x5", callback_data=size_cb.new(game_id=game_id, size="5x5")))
    kb.add(types.InlineKeyboardButton("5x8", callback_data=size_cb.new(game_id=game_id, size="5x8")))
    kb.add(types.InlineKeyboardButton("5x12", callback_data=size_cb.new(game_id=game_id, size="5x12")))
    await (message_or.reply if isinstance(message_or, types.Message) else message_or.message.reply)("Select field size:", reply_markup=kb)

@dp.callback_query_handler(size_cb.filter())
async def cb_size_select(callback: types.CallbackQuery, callback_data: dict):
    await callback.answer()
    game_id = callback_data["game_id"]
    size = callback_data["size"]
    rows, cols = map(int, size.split("x"))
    grid, filler = generate_game_grid(rows, cols)
    game_doc = {
        "game_id": game_id,
        "owner_id": callback.from_user.id,
        "chat_id": callback.message.chat.id,
        "rows": rows,
        "cols": cols,
        "grid": grid,
        "revealed": [],
        "score": 0,
        "pairs_found": 0,
        "started_at": time.time(),
        "filler": filler,
        "players": {str(callback.from_user.id): {"pairs_found":0, "score":0}},
        "is_finished": False
    }
    games_coll.insert_one(game_doc)
    await send_game_grid(callback.message.chat.id, game_doc)

async def send_game_grid(chat_id: int, game_doc: Dict[str,Any]):
    rows = game_doc["rows"]
    cols = game_doc["cols"]
    grid = game_doc["grid"]
    kb = types.InlineKeyboardMarkup(row_width=cols)
    total = rows * cols
    for idx in range(total):
        cell = grid[idx]
        if cell["matched"]:
            btn_text = "✅"
            cb = types.InlineKeyboardButton(btn_text, callback_data="noop")
        else:
            btn_text = cell["emoji"]
            cb = types.InlineKeyboardButton(btn_text, callback_data=game_cb.new(game_id=game_doc["game_id"], action="pick", index=str(idx)))
        kb.insert(cb)
    kb.add(types.InlineKeyboardButton("🔁 Restart", callback_data="start_new"))
    kb.add(types.InlineKeyboardButton("🏁 End Game", callback_data=game_cb.new(game_id=game_doc["game_id"], action="end", index="0")))
    text = f"Emoji Pair Finder — {game_doc['rows']}x{game_doc['cols']}\nPairs found: {game_doc['pairs_found']} • Score: {game_doc['score']}"
    await bot.send_message(chat_id, text, reply_markup=kb)

@dp.callback_query_handler(game_cb.filter())
async def cb_game_actions(callback: types.CallbackQuery, callback_data: dict):
    await callback.answer()
    game_id = callback_data["game_id"]
    action = callback_data["action"]
    index = int(callback_data["index"])

    game_doc = games_coll.find_one({"game_id": game_id})
    if not game_doc:
        await callback.message.answer("Game not found or expired.")
        return

    # cooldown
    user_doc = users_coll.find_one({"user_id": callback.from_user.id}) or {"cooldown_until":0}
    now = time.time()
    if now < user_doc.get("cooldown_until", 0):
        await callback.answer("Too fast! Wait a second.", show_alert=False)
        return
    users_coll.update_one({"user_id": callback.from_user.id}, {"$set": {"cooldown_until": now + COOLDOWN_SECONDS}}, upsert=True)

    if action == "pick":
        await handle_pick(callback, game_doc, index)
    elif action == "end":
        await handle_end_game(callback, game_doc)

async def handle_pick(callback: types.CallbackQuery, game_doc: dict, index: int):
    grid = game_doc["grid"]
    total = game_doc["rows"] * game_doc["cols"]
    if index < 0 or index >= total:
        return
    if grid[index]["matched"]:
        return

    revealed = game_doc.get("revealed", [])
    if index in revealed:
        return

    revealed.append(index)
    games_coll.update_one({"game_id": game_doc["game_id"]}, {"$set": {"revealed": revealed}})

    if len(revealed) == 2:
        i1, i2 = revealed
        e1, e2 = grid[i1]["emoji"], grid[i2]["emoji"]
        owner_id = callback.from_user.id
        filler = game_doc.get("filler")

        if e1 == e2:
            grid[i1]["matched"] = True
            grid[i2]["matched"] = True
            points = 10
            games_coll.update_one({"game_id": game_doc["game_id"]}, {"$set": {"grid": grid}, "$inc": {"pairs_found": 1, "score": points}})
            games_coll.update_one({"game_id": game_doc["game_id"]}, {"$inc": {f"players.{owner_id}.pairs_found": 1, f"players.{owner_id}.score": points}})
            users_coll.update_one({"user_id": owner_id}, {"$inc": {"pairs_found": 1, "total_points": points}})
            games_coll.update_one({"game_id": game_doc["game_id"]}, {"$set": {"revealed": []}})
            await callback.answer("Match! +10 points 🎉")
        elif filler and (e1 == filler or e2 == filler):
            if e1 == filler: grid[i1]["matched"] = True
            if e2 == filler: grid[i2]["matched"] = True
            games_coll.update_one({"game_id": game_doc["game_id"]}, {"$set": {"grid": grid, "revealed": []}})
            await callback.answer("Filler matched (no points).")
        else:
            games_coll.update_one({"game_id": game_doc["game_id"]}, {"$set": {"revealed": []}})
            await callback.answer("Not a match.")

        game_doc = games_coll.find_one({"game_id": game_doc["game_id"]})
        if all(c["matched"] for c in game_doc["grid"]):
            await finalize_game(callback.message.chat.id, game_doc)
            return
        try:
            await bot.delete_message(callback.message.chat.id, callback.message.message_id)
        except: pass
        await send_game_grid(callback.message.chat.id, game_doc)

async def finalize_game(chat_id: int, game_doc: dict):
    players = game_doc.get("players", {})
    for uid_str, pdata in players.items():
        uid = int(uid_str)
        score = pdata.get("score",0)
        users_coll.update_one({"user_id": uid}, {"$inc": {"games_played": 1}})
        user = users_coll.find_one({"user_id": uid}) or {}
        if score and score > user.get("best_score", 0):
            users_coll.update_one({"user_id": uid}, {"$set": {"best_score": score}})
    chats_coll.update_one({"chat_id": chat_id}, {"$inc": {"games_played": 1, "total_activity": 1}}, upsert=True)
    games_coll.update_one({"game_id": game_doc["game_id"]}, {"$set": {"is_finished": True, "finished_at": time.time()}})
    lines = ["🏁 Game Over! Results:"]
    for uid_str, pdata in players.items():
        uid = int(uid_str)
        uname = (users_coll.find_one({"user_id": uid}) or {}).get("username") or f"User {uid}"
        lines.append(f"{uname}: {pdata.get('score',0)} pts • {pdata.get('pairs_found',0)} pairs")
    await bot.send_message(chat_id, "\n".join(lines))

async def handle_end_game(callback: types.CallbackQuery, game_doc: dict):
    await callback.answer("Ending game...")
    await finalize_game(callback.message.chat.id, game_doc)

# Profile, leaderboard, etc
@dp.message_handler(commands=["profile"])
async def cmd_profile(message: types.Message):
    upsert_user_profile(message.from_user)
    doc = users_coll.find_one({"user_id": message.from_user.id}) or {}
    text = (f"👤 Profile — @{message.from_user.username or message.from_user.first_name}\n"
            f"Total games: {doc.get('games_played',0)}\n"
            f"Pairs found: {doc.get('pairs_found',0)}\n"
            f"Total points: {doc.get('total_points',0)}\n"
            f"Best score: {doc.get('best_score',0)}\n")
    rank = users_coll.count_documents({"total_points": {"$gt": doc.get("total_points",0)}}) + 1
    text += f"Global rank: #{rank}"
    await message.reply(text)

@dp.message_handler(commands=["chat"])
async def cmd_chatlist(message: types.Message):
    doc = users_coll.find_one({"user_id": message.from_user.id}) or {}
    chat_ids = doc.get("chats", [])
    if not chat_ids:
        await message.reply("No chats recorded.")
        return
    lines = ["Chats:"]
    for cid in chat_ids:
        chat = chats_coll.find_one({"chat_id": cid}) or {}
        lines.append(f"- {chat.get('title','Chat')} (id: {cid})")
    await message.reply("\n".join(lines))

@dp.message_handler(commands=["privacy"])
async def cmd_privacy(message: types.Message):
    await message.reply("Privacy Policy:\nThis bot stores only minimal game stats. No personal data is shared.")

@dp.message_handler(commands=["leaderboard"])
async def cmd_leaderboard(message: types.Message):
    top_users = list(users_coll.find({}).sort("total_points",-1).limit(10))
    top_chats = list(chats_coll.find({}).sort("total_activity",-1).limit(10))
    lines = ["🌍 Top 10 Users:"]
    rank = 1
    for u in top_users:
        name = u.get("username") or f"User {u.get('user_id')}"
        lines.append(f"{rank}. {name} — {u.get('total_points',0)} pts")
        rank += 1
    lines.append("\n💬 Top Chats:")
    rank = 1
    for c in top_chats:
        lines.append(f"{rank}. {c.get('title','Chat')} — activity {c.get('total_activity',0)}")
        rank += 1
    await message.reply("\n".join(lines))

@dp.message_handler(commands=["language"])
async def cmd_language(message: types.Message):
    kb = types.InlineKeyboardMarkup(row_width=4)
    for code, flag in LANG_FLAGS.items():
        kb.add(types.InlineKeyboardButton(f"{flag} {code.upper()}", callback_data=lang_cb.new(lang=code)))
    await message.reply("Choose language:", reply_markup=kb)

@dp.callback_query_handler(lang_cb.filter())
async def cb_set_language(callback: types.CallbackQuery, callback_data: dict):
    lang = callback_data["lang"]
    users_coll.update_one({"user_id": callback.from_user.id}, {"$set": {"language": lang}})
    await callback.answer(f"Language set to {lang.upper()}")
    await callback.message.edit_text(f"Language updated to {LANG_FLAGS.get(lang, lang)}")

@dp.message_handler(commands=["broadcast"])
@require_owner
async def cmd_broadcast(message: types.Message):
    if not message.reply_to_message:
        await message.reply("Reply to a message to broadcast it.")
        return
    count = 0
    for u in users_coll.find({}, {"user_id":1}):
        try:
            await message.reply_to_message.copy_to(u["user_id"])
            count += 1
        except: continue
    await message.reply(f"Broadcast sent to {count} users.")

# Main
if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
