#!/usr/bin/env python3
"""
bot.py (Version 14.0 - The Stable Merge)
------------------------------------------
- MERGE: Combines the stable sequential quiz logic of v12 with the robust database backend of v13.
- FIX: Re-implements sequential question serving (question_index tracking). Questions are no longer random.
- FEATURE: All data (users, scores, questions) is now managed through the SQLite database.
- STABILITY: All known bugs from v13's quiz logic have been resolved.
"""
import os
import sys
import json
import math
import logging
import sqlite3
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from apscheduler.jobstores.base import JobLookupError

# ---------------------
# Config & constants
# ---------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("CRITICAL: BOT_TOKEN missing. Set it in .env.")
    sys.exit(1)

DB_FILE = "quiz_bot.db"
PAGE_SIZE = 5
QUESTION_TIMER_SECONDS = 30

BTN_CATEGORIES = "📚 Quiz Categories"
BTN_LEADERBOARD = "🏆 Leaderboard"
BTN_MY_SCORE = "📊 My Score"

# ---------------------
# Logging
# ---------------------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------
# DB helpers (From v13)
# ---------------------
def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def db_query(query, params=()):
    with get_db_connection() as conn:
        cur = conn.execute(query, params)
        return cur.fetchall()

def db_execute(query, params=()):
    with get_db_connection() as conn:
        conn.execute(query, params)
        conn.commit()

def add_or_update_user(user_id: int, name: str):
    db_execute("INSERT INTO users (user_id, name) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name", (user_id, name))

def update_user_score(user_id: int, points_to_add: int = 1):
    db_execute("UPDATE users SET score = score + ? WHERE user_id = ?", (points_to_add, user_id))

def get_user_score(user_id: int) -> int:
    row = db_query("SELECT score FROM users WHERE user_id = ?", (user_id,))
    return row[0]["score"] if row else 0

def get_leaderboard_data():
    rows = db_query("SELECT user_id, name, score FROM users WHERE score > 0 ORDER BY score DESC, name ASC")
    return [(str(r["user_id"]), {"name": r["name"], "score": r["score"]}) for r in rows]

# v14 CHANGE: Renamed from get_all_questions_for_category to be clearer
def get_questions_by_category(category: str):
    rows = db_query("SELECT id, category, question_text, options, answer FROM questions WHERE category = ? ORDER BY id ASC", (category,))
    questions = []
    for r in rows:
        questions.append({
            "id": r["id"],
            "question": r["question_text"],
            "options": json.loads(r["options"]),
            "answer": r["answer"]
        })
    return questions

# ---------------------
# Bot helper UI builders
# ---------------------
def get_user_display_name(update: Update) -> str:
    user = update.effective_user
    if not user: return "Player"
    return user.username or user.first_name or "Player"

def main_menu_keyboard():
    return ReplyKeyboardMarkup([[BTN_CATEGORIES, BTN_LEADERBOARD], [BTN_MY_SCORE]], resize_keyboard=True)

def build_category_inline_markup():
    rows = db_query("SELECT DISTINCT category FROM questions ORDER BY category ASC")
    buttons = [[InlineKeyboardButton(r["category"].capitalize(), callback_data=f"category_{r['category']}")] for r in rows]
    return InlineKeyboardMarkup(buttons)

def build_leaderboard_page(sorted_scores, page: int, requester_user_id: str):
    total_pages = max(1, math.ceil(len(sorted_scores) / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start, end = page * PAGE_SIZE, (page + 1) * PAGE_SIZE
    slice_ = sorted_scores[start:end]
    header = f"🏆 Leaderboard (Page {page+1} of {total_pages}) 🏆\n\n"
    lines = [f'{i}. {data.get("name", f"User {uid}")} — {data.get("score", 0)} points{" (You)" if str(requester_user_id) == uid else ""}' for i, (uid, data) in enumerate(slice_, start=start + 1)]
    text = header + ("\n".join(lines) if lines else "No players yet.")
    buttons = []
    if page > 0: buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"leaderboard_page_{page-1}"))
    if page < total_pages - 1: buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"leaderboard_page_{page+1}"))
    return text, InlineKeyboardMarkup([buttons]) if buttons else None

# ---------------------
# Timer callback (Stable version)
# ---------------------
async def timer_expired_callback(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    chat_id, message_id, correct_answer, user_id = job_data.get("chat_id"), job_data.get("message_id"), job_data.get("correct_answer"), job_data.get("user_id")
    user_data = context.application.user_data.get(user_id, {})
    if user_data.get("answered_question", False): return
    user_data["answered_question"] = True
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=f"⏰ Time's up!\nThe correct answer was: {correct_answer}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Next Question ➡️", callback_data="next_question")]])
        )
    except Exception as e:
        logger.warning(f"Failed to edit message on timer expiry: {e}")

# ---------------------
# Core Quiz Logic (Brought back from v12 and adapted for DB)
# ---------------------
# v14 CHANGE: This is the sequential send_question logic from v12, adapted for the database.
async def send_question(destination, context: ContextTypes.DEFAULT_TYPE, category: str, question_index: int, edit: bool = False):
    user_id = destination.from_user.id
    
    questions_in_category = get_questions_by_category(category)
    
    if not questions_in_category or question_index >= len(questions_in_category):
        msg_text = "🎉 You've completed all questions in this category! Choose another from the menu."
        if edit: await destination.edit_message_text(text=msg_text, reply_markup=build_category_inline_markup())
        else: await destination.message.reply_text(text=msg_text)
        return

    question_data = questions_in_category[question_index]
    
    context.user_data['answered_question'] = False
    context.user_data['current_category'] = category
    context.user_data['current_question_index'] = question_index
    context.user_data['current_question_data'] = question_data

    keyboard = [[InlineKeyboardButton(opt, callback_data=opt)] for opt in question_data["options"]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    question_text = f"Q{question_index + 1}: {question_data['question']}"

    if edit:
        sent_message = await destination.edit_message_text(question_text, reply_markup=reply_markup)
    else:
        sent_message = await destination.message.reply_text(question_text, reply_markup=reply_markup)

    old_job = context.user_data.get("timer_job")
    if old_job:
        try: old_job.schedule_removal()
        except JobLookupError: pass

    job = context.job_queue.run_once(
        timer_expired_callback,
        when=QUESTION_TIMER_SECONDS,
        data={"chat_id": sent_message.chat.id, "message_id": sent_message.message_id, "correct_answer": question_data["answer"], "user_id": user_id},
        name=f"timer_{user_id}_{sent_message.chat.id}"
    )
    context.user_data['timer_job'] = job

# ---------------------
# Handlers
# ---------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_or_update_user(user.id, get_user_display_name(update))
    await update.message.reply_text("Welcome to the Quiz Bot! Use the menu below to start.", reply_markup=main_menu_keyboard())

async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📚 Please choose a category:", reply_markup=build_category_inline_markup())

# v14 CHANGE: This is the handler logic from v12. Starts quiz at index 0.
async def select_category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.split("_", 1)[1]
    await send_question(query, context, category, question_index=0, edit=True)

# v14 CHANGE: This is the handler logic from v12. Increments the index.
async def next_question_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = context.user_data.get('current_category')
    current_index = context.user_data.get('current_question_index', -1)
    if not category:
        await query.edit_message_text("⚠️ Please pick a category first using the main menu.", reply_markup=build_category_inline_markup())
        return
    next_index = current_index + 1
    await send_question(query, context, category, question_index=next_index, edit=True)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    
    job = context.user_data.get("timer_job")
    if job:
        try: job.schedule_removal()
        except JobLookupError: pass

    if context.user_data.get('answered_question', True): return

    context.user_data['answered_question'] = True
    
    selected = query.data
    current_q = context.user_data.get('current_question_data')
    if not current_q:
        await query.edit_message_text("⚠️ No active question. Please start a new quiz.")
        return

    correct_answer = current_q["answer"]
    add_or_update_user(user.id, get_user_display_name(update))

    if selected == correct_answer:
        update_user_score(user.id, 1)
        response = f"✅ Correct! 🎉\nYour score: {get_user_score(user.id)}"
    else:
        response = f"❌ Wrong! The correct answer was: {correct_answer}\nYour score: {get_user_score(user.id)}"

    await query.edit_message_text(response, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Next Question ➡️", callback_data="next_question")]]))

async def score_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_or_update_user(user.id, get_user_display_name(update))
    await update.message.reply_text(f"🏆 Your current score is: {get_user_score(user.id)}")

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = get_leaderboard_data()
    if not data:
        await update.message.reply_text("Leaderboard is empty.")
        return
    text, markup = build_leaderboard_page(data, 0, str(update.effective_user.id))
    await update.message.reply_text(text, reply_markup=markup)

async def leaderboard_page_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    data = get_leaderboard_data()
    text, markup = build_leaderboard_page(data, page, str(query.from_user.id))
    await query.edit_message_text(text, reply_markup=markup)

# ---------------------
# Main
# ---------------------
def main():
    if not os.path.exists(DB_FILE):
        logger.critical(f"Database '{DB_FILE}' not found. Run database_setup.py and migrate_data.py first.")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quiz", quiz))
    app.add_handler(CommandHandler("score", score_handler))
    app.add_handler(CommandHandler("leaderboard", leaderboard))

    app.add_handler(MessageHandler(filters.Text(BTN_CATEGORIES), quiz))
    app.add_handler(MessageHandler(filters.Text(BTN_LEADERBOARD), leaderboard))
    app.add_handler(MessageHandler(filters.Text(BTN_MY_SCORE), score_handler))
    
    app.add_handler(CallbackQueryHandler(select_category_handler, pattern=r"^category_.*$"))
    app.add_handler(CallbackQueryHandler(next_question_handler, pattern=r"^next_question$"))
    app.add_handler(CallbackQueryHandler(leaderboard_page_handler, pattern=r"^leaderboard_page_\d+$"))
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^(?!next_question|leaderboard_page_|category_).*$"))

    logger.info("Bot is running (Version 14.0 - Stable DB Merge)...")
    app.run_polling()

if __name__ == "__main__":
    main()