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
import math
from functools import wraps
from typing import List, Dict, Any

from aiogram import Bot, Dispatcher, types, executor
from aiogram.utils.callback_data import CallbackData
from pymongo import MongoClient
from dotenv import load_dotenv

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

# Constants
EMOJI_POOL = [
    "😀","😅","😂","🤣","😊","😍","😎","😉","🤩","🤓",
    "😇","🥳","🤠","😺","🐶","🐱","🦊","🐻","🐼","🦁",
    "🍎","🍌","🍇","🍓","🍒","🍉","🍩","🍪","🍰","☕",
    "⚽","🏀","🏈","🎮","🎲","🎯","🎹","🎸","🚗","✈️"
]
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
    users_coll.update_one({"user_id": user_id}, {"$inc": {k: v for k, v in fields.items() if isinstance(v, (int,float))}},
                          upsert=True)

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
    - We create pairs for floor(rows*cols/2) and if odd cell exists, put a filler that auto-matches (no points).
    """
    total = rows * cols
    pair_count = total // 2
    # choose random emojis
    emojis = random.sample(EMOJI_POOL, k=pair_count)
    deck = []
    for e in emojis:
        deck.append(e)
        deck.append(e)
    # if odd, add a filler (unique emoji) that'll be auto-matched but gives 0 points
    filler = None
    if total % 2 == 1:
        filler = "⬜"  # neutral filler; when revealed it will be turned into check and give 0 points
        deck.append(filler)
    random.shuffle(deck)
    grid = [{"emoji": deck[i], "matched": False} for i in range(total)]
    return grid, filler

def format_scoreboard_row(rank, user_doc):
    name = user_doc.get("username") or f"User {user_doc.get('user_id')}"
    return f"{rank}. {name} — {user_doc.get('total_points',0)} pts"

# Command handlers

@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    upsert_user_profile(message.from_user)
    ensure_chat_record(message.chat)
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("▶️ Start Game", callback_data="start_new"))
    kb.add(types.InlineKeyboardButton("🎲 How to Play", callback_data="how_play"))
    kb.add(types.InlineKeyboardButton("👤 Owner & Support", url=f"https://t.me/{(os.getenv('OWNER_USERNAME') or 'your_owner_username')}"))
    # Language button opens inline language menu
    kb.add(types.InlineKeyboardButton("🌐 Change Language", callback_data="open_lang"))
    kb.add(types.InlineKeyboardButton("➕ Add Me to Chat", url=f"https://t.me/{(os.getenv('BOT_USERNAME') or 'YourBot') }?startgroup=true"))
    text = ("🎮 Welcome to Emoji Pair Finder Game!\n"
            "Match the emoji pairs and score points.\n"
            "Play alone in DM or invite friends in your groups.")
    await message.answer(text, reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data == "start_new")
async def cb_start_new(callback: types.CallbackQuery):
    # same as /new
    await callback.answer()
    await start_new_game_flow(callback.message, callback.from_user)

@dp.message_handler(commands=["new", "play"])
async def cmd_new(message: types.Message):
    await start_new_game_flow(message, message.from_user)

async def start_new_game_flow(message_or: types.Message, user: types.User):
    # ask size selection
    # create game_id
    game_id = str(int(time.time() * 1000)) + "_" + str(user.id)  # simple unique id
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
    # parse size format "5x8"
    rows, cols = map(int, size.split("x"))
    grid, filler = generate_game_grid(rows, cols)
    # initial game doc
    game_doc = {
        "game_id": game_id,
        "owner_id": callback.from_user.id,
        "chat_id": callback.message.chat.id,
        "rows": rows,
        "cols": cols,
        "grid": grid,  # list of {"emoji", "matched"}
        "revealed": [],  # indices currently revealed but not matched
        "score": 0,
        "pairs_found": 0,
        "started_at": time.time(),
        "filler": filler,
        "players": {str(callback.from_user.id): {"pairs_found":0, "score":0}},
        "is_finished": False
    }
    games_coll.insert_one(game_doc)
    # send grid message
    await send_game_grid(callback.message.chat.id, game_doc, callback.message.message_id)

async def send_game_grid(chat_id: int, game_doc: Dict[str,Any], reply_to_message_id: int = None):
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
            btn_text = str(idx+1)  # show numbers to pick
            # callback includes game id, action "pick", index
            cb = types.InlineKeyboardButton(btn_text, callback_data=game_cb.new(game_id=game_doc["game_id"], action="pick", index=str(idx)))
        kb.insert(cb)
    # additional controls
    kb.add(types.InlineKeyboardButton("🔁 Restart", callback_data="start_new"))
    kb.add(types.InlineKeyboardButton("🏁 End Game", callback_data=game_cb.new(game_id=game_doc["game_id"], action="end", index="0")))
    text = f"Emoji Pair Finder — {game_doc['rows']}x{game_doc['cols']}\nPairs found: {game_doc['pairs_found']} • Score: {game_doc['score']}"
    await bot.send_message(chat_id, text, reply_markup=kb, reply_to_message_id=reply_to_message_id)

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

    # Anti-spam cooldown (per-user)
    user_doc = users_coll.find_one({"user_id": callback.from_user.id}) or {"cooldown_until":0}
    now = time.time()
    if now < user_doc.get("cooldown_until", 0):
        # ignore click
        await callback.answer("You're clicking too fast — wait a moment.", show_alert=False)
        return
    # update cooldown
    users_coll.update_one({"user_id": callback.from_user.id}, {"$set": {"cooldown_until": now + COOLDOWN_SECONDS}}, upsert=True)

    if action == "pick":
        await handle_pick(callback, game_doc, index)
    elif action == "end":
        await handle_end_game(callback, game_doc)
    else:
        await callback.answer()

async def handle_pick(callback: types.CallbackQuery, game_doc: dict, index: int):
    grid = game_doc["grid"]
    total = game_doc["rows"] * game_doc["cols"]
    if index < 0 or index >= total:
        await callback.answer("Invalid cell.")
        return

    if grid[index]["matched"]:
        await callback.answer("Already matched.")
        return

    # reveal current pick
    revealed = game_doc.get("revealed", [])
    # If the same index clicked twice, ignore
    if index in revealed:
        await callback.answer("Already revealed.")
        return

    revealed.append(index)
    # Save revealed in DB
    games_coll.update_one({"game_id": game_doc["game_id"]}, {"$set": {"revealed": revealed}})

    # If two revealed, check pair
    if len(revealed) == 2:
        i1, i2 = revealed
        e1 = grid[i1]["emoji"]
        e2 = grid[i2]["emoji"]
        # If filler present and one is filler: auto-match filler but no points
        filler = game_doc.get("filler")
        owner_id = callback.from_user.id
        if e1 == e2:
            # correct pair
            grid[i1]["matched"] = True
            grid[i2]["matched"] = True
            # points: +10 per correct pair
            points = 10
            # update scores in both game doc and user profile
            games_coll.update_one({"game_id": game_doc["game_id"]}, {"$set": {"grid": grid}, "$inc": {"pairs_found": 1, "score": points}})
            # per-player stats in game
            player_key = str(owner_id)
            games_coll.update_one({"game_id": game_doc["game_id"]}, {"$inc": {f"players.{player_key}.pairs_found": 1, f"players.{player_key}.score": points}})
            # update global user stats
            users_coll.update_one({"user_id": owner_id}, {"$inc": {"pairs_found": 1, "total_points": points}})
            # update best score if needed at game end (we'll set best_score there)
            # reset revealed
            games_coll.update_one({"game_id": game_doc["game_id"]}, {"$set": {"revealed": []}})
            await callback.answer("Match! +10 points 🎉")
        elif filler and (e1 == filler or e2 == filler):
            # if filler clicked as part of pair -> treat filler as auto-match but give 0 points
            # mark the filler index as matched
            if e1 == filler:
                grid[i1]["matched"] = True
            if e2 == filler:
                grid[i2]["matched"] = True
            games_coll.update_one({"game_id": game_doc["game_id"]}, {"$set": {"grid": grid, "revealed": []}})
            await callback.answer("You found a filler tile — it auto-matched (no points).")
        else:
            # wrong pair -> simply reset revealed (they stay hidden)
            games_coll.update_one({"game_id": game_doc["game_id"]}, {"$set": {"revealed": []}})
            await callback.answer("Not a match — try again.")
        # reload game doc
        game_doc = games_coll.find_one({"game_id": game_doc["game_id"]})
        # check if finished (all matched)
        all_matched = all(c.get("matched", False) for c in game_doc["grid"])
        if all_matched:
            await finalize_game(callback.message.chat.id, game_doc)
            return
        # update grid display (send a fresh message)
        # for simplicity, delete the original message and send a new one
        try:
            await bot.delete_message(callback.message.chat.id, callback.message.message_id)
        except Exception:
            pass
        await send_game_grid(callback.message.chat.id, game_doc)
    else:
        # only one revealed — we can show a small alert with the emoji
        await callback.answer(f"Revealed: {grid[index]['emoji']}")

async def finalize_game(chat_id: int, game_doc: dict):
    # compute final per-player winner & update global users with game played, best score
    players = game_doc.get("players", {})
    # update global users: games_played++, best_score
    for uid_str, pdata in players.items():
        uid = int(uid_str)
        score = pdata.get("score",0)
        users_coll.update_one({"user_id": uid}, {"$inc": {"games_played": 1}})
        # update best score if higher
        user = users_coll.find_one({"user_id": uid}) or {}
        if score and score > user.get("best_score", 0):
            users_coll.update_one({"user_id": uid}, {"$set": {"best_score": score}})
    # update chat stats
    chats_coll.update_one({"chat_id": chat_id}, {"$inc": {"games_played": 1, "total_activity": 1}}, upsert=True)
    # mark game finished
    games_coll.update_one({"game_id": game_doc["game_id"]}, {"$set": {"is_finished": True, "finished_at": time.time()}})
    # announce final scores
    lines = ["🏁 Game Over! Results:"]
    for uid_str, pdata in players.items():
        uid = int(uid_str)
        uname = (users_coll.find_one({"user_id": uid}) or {}).get("username") or f"User {uid}"
        lines.append(f"{uname}: {pdata.get('score',0)} pts • {pdata.get('pairs_found',0)} pairs")
    text = "\n".join(lines)
    await bot.send_message(chat_id, text)

async def handle_end_game(callback: types.CallbackQuery, game_doc: dict):
    # allow anyone to end; finalize game
    await callback.answer("Ending game...")
    await finalize_game(callback.message.chat.id, game_doc)

# profile, chat, privacy, leaderboard, language, broadcast

@dp.message_handler(commands=["profile"])
async def cmd_profile(message: types.Message):
    upsert_user_profile(message.from_user)
    doc = users_coll.find_one({"user_id": message.from_user.id}) or {}
    text = (f"👤 Profile — @{message.from_user.username or message.from_user.first_name}\n"
            f"Total games: {doc.get('games_played',0)}\n"
            f"Pairs found: {doc.get('pairs_found',0)}\n"
            f"Total points: {doc.get('total_points',0)}\n"
            f"Best score: {doc.get('best_score',0)}\n")
    # compute global rank by total_points
    rank = users_coll.count_documents({"total_points": {"$gt": doc.get("total_points",0)}}) + 1
    text += f"Global rank: #{rank}"
    await message.reply(text)

@dp.message_handler(commands=["chat"])
async def cmd_chatlist(message: types.Message):
    # show chats where user added the bot (based on 'chats' array in user doc)
    doc = users_coll.find_one({"user_id": message.from_user.id}) or {}
    chat_ids = doc.get("chats", [])
    if not chat_ids:
        await message.reply("I don't have a record of chats you've added me to.")
        return
    lines = ["Chats where you've added the bot:"]
    for cid in chat_ids:
        chat = chats_coll.find_one({"chat_id": cid}) or {}
        lines.append(f"- {chat.get('title','Chat')} (id: {cid})")
    await message.reply("\n".join(lines))

@dp.message_handler(commands=["privacy"])
async def cmd_privacy(message: types.Message):
    await message.reply("Privacy Policy:\nThis bot stores minimal data (user id, username, game stats) to provide game features. No personal data is sold or shared.")

@dp.message_handler(commands=["leaderboard"])
async def cmd_leaderboard(message: types.Message):
    # top 10 users by total_points
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

@dp.callback_query_handler(lambda c: c.data == "open_lang")
async def cb_open_lang(callback: types.CallbackQuery):
    await callback.answer()
    await cmd_language(callback.message)

@dp.callback_query_handler(lang_cb.filter())
async def cb_lang_change(callback: types.CallbackQuery, callback_data: dict):
    code = callback_data["lang"]
    users_coll.update_one({"user_id": callback.from_user.id}, {"$set": {"language": code}}, upsert=True)
    await callback.answer(f"Language set to {code.upper()}")
    await callback.message.edit_reply_markup()  # close the menu

# Admin broadcast
@dp.message_handler(commands=["broadcast"])
@require_owner
async def cmd_broadcast(message: types.Message):
    # Usage: reply to a message you want broadcast, or send /broadcast followed by text
    text = ""
    target_msg = None
    if message.reply_to_message:
        target_msg = message.reply_to_message
    else:
        # take text after command
        text = message.get_args()
        if not text:
            await message.reply("Usage: reply to a message to broadcast it, or send /broadcast <text>")
            return
    # get all user ids and chat ids
    user_ids = [u["user_id"] for u in users_coll.find({}, {"user_id":1})]
    chat_ids = [c["chat_id"] for c in chats_coll.find({}, {"chat_id":1})]

    # broadcast to users
    sent_users = 0
    sent_chats = 0
    errors = 0
    for uid in user_ids:
        try:
            if target_msg:
                await bot.copy_message(uid, target_msg.chat.id, target_msg.message_id)
            else:
                await bot.send_message(uid, text)
            sent_users += 1
        except Exception as e:
            errors += 1
    for cid in chat_ids:
        try:
            if target_msg:
                await bot.copy_message(cid, target_msg.chat.id, target_msg.message_id)
            else:
                await bot.send_message(cid, text)
            sent_chats += 1
        except Exception:
            errors += 1
    await message.reply(f"Broadcast done: users {sent_users}, chats {sent_chats}, errors {errors}")

# Helper: when bot added to chat, record chat in DB and add chat id to user doc of the adder if we can
@dp.my_chat_member_handler()
async def on_chat_member_update(my_chat_member: types.ChatMemberUpdated):
    # when bot is added to group or removed
    chat = my_chat_member.chat
    new_status = my_chat_member.new_chat_member.status
    if new_status in ("member", "administrator"):
        ensure_chat_record(chat)
        # try to attribute to the inviter (if exists)
        from_user = my_chat_member.from_user
        if from_user:
            users_coll.update_one({"user_id": from_user.id}, {"$addToSet": {"chats": chat.id}})
    elif new_status == "left":
        # maybe mark chat as inactive
        chats_coll.update_one({"chat_id": chat.id}, {"$set": {"left_at": time.time()}})

# No-op handler for unused buttons
@dp.callback_query_handler(lambda c: c.data == "noop")
async def cb_noop(callback: types.CallbackQuery):
    await callback.answer()

# Startup / shutdown
if __name__ == "__main__":
    logging.info("Starting Emoji Pair Finder bot...")
    executor.start_polling(dp, skip_updates=True)
