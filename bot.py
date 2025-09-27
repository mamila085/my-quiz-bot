"""
bot.py - Quiz Bot (Version 12.4)
------------------------------
- CHANGE: Questions are now served sequentially in the order they appear in questions.json.
- CHANGE: The bot tracks the user's progress (question index) per category.
- FIX: The random repetition bug is now resolved by using sequential logic.
- All other features (Timer, Paginated Leaderboard, etc.) are stable.
"""
import os
import sys
import json
import math
import logging
import random # We keep it for potential future use, but don't use it for question picking
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

# --------------------
# Configuration
# --------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("CRITICAL: BOT_TOKEN not found in environment (.env). Exiting.")
    sys.exit(1)

SCORES_FILE = "scores.json"
QUESTIONS_FILE = "questions.json"
PAGE_SIZE = 5
QUESTION_TIMER_SECONDS = 30

# --------------------
# Logging & Global State
# --------------------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
quiz_data = {}
scores = {}

# --------------------
# Utilities: load/save
# --------------------
def load_questions():
    if not os.path.exists(QUESTIONS_FILE): logger.critical(f"{QUESTIONS_FILE} not found. Exiting."); sys.exit(1)
    try:
        with open(QUESTIONS_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except json.JSONDecodeError: logger.critical(f"{QUESTIONS_FILE} contains invalid JSON. Exiting."); sys.exit(1)

def load_scores():
    if not os.path.exists(SCORES_FILE): return {}
    try:
        with open(SCORES_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except json.JSONDecodeError: logger.warning("scores.json corrupted; starting fresh."); return {}

def save_scores_atomic(data):
    tmp = SCORES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SCORES_FILE)

# --------------------
# Helper functions
# --------------------
def get_user_display_name(update: Update) -> str:
    user = update.effective_user
    if not user: return "Player"
    if user.username: return f"@{user.username}"
    if user.first_name: return user.first_name
    return "Player"

def build_main_menu_markup() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["📚 Quiz Categories", "🏆 Leaderboard"], ["📊 My Score"]], resize_keyboard=True)

def build_category_inline_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(name.capitalize(), callback_data=f"category_{name}")] for name in quiz_data.keys()])

def build_leaderboard_page(sorted_scores, page: int, requester_user_id: str):
    total_pages = max(1, math.ceil(len(sorted_scores) / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start, end = page * PAGE_SIZE, (page + 1) * PAGE_SIZE
    players_on_page = sorted_scores[start:end]
    header = f"🏆 Leaderboard (Page {page+1} of {total_pages}) 🏆\n\n"
    lines = []
    for i, (uid, data) in enumerate(players_on_page, start=start + 1):
        display, score_val = data.get("name", f"User {uid}"), data.get("score", 0)
        marker = " (You)" if str(requester_user_id) == uid else ""
        lines.append(f"{i}. {display} — {score_val} points{marker}")
    text = header + "\n".join(lines)
    buttons = []
    if page > 0: buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"leaderboard_page_{page-1}"))
    if page < total_pages - 1: buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"leaderboard_page_{page+1}"))
    return text, InlineKeyboardMarkup([buttons]) if buttons else None

# --------------------
# Timer callback
# --------------------
async def timer_expired_callback(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    chat_id, message_id, correct_answer = job_data.get("chat_id"), job_data.get("message_id"), job_data.get("correct_answer")
    user_id = job_data.get("user_id")
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
        logger.warning("Failed to edit message on timer expiry: %s", e)

# --------------------
# Core handlers
# --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id, display_name = str(update.effective_user.id), get_user_display_name(update)
    if user_id not in scores or scores[user_id].get("name") != display_name:
        scores[user_id] = {"name": display_name, "score": scores.get(user_id, {}).get("score", 0)}
        save_scores_atomic(scores)
    await update.message.reply_text("Welcome! Use the menu below.", reply_markup=build_main_menu_markup())

async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📚 Choose a category:", reply_markup=build_category_inline_markup())

# --- CHANGE: This function now sends questions sequentially ---
async def send_question(destination, context: ContextTypes.DEFAULT_TYPE, category: str, question_index: int, edit: bool = False):
    user_id = destination.from_user.id
    
    questions_in_category = quiz_data.get(category, [])
    if not questions_in_category or question_index >= len(questions_in_category):
        msg_text = "🎉 You've completed all questions in this category!"
        if edit: await destination.edit_message_text(text=msg_text)
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

    job = context.job_queue.run_once(
        timer_expired_callback,
        when=QUESTION_TIMER_SECONDS,
        data={"chat_id": sent_message.chat.id, "message_id": sent_message.message_id, "correct_answer": question_data["answer"], "user_id": user_id},
    )
    context.user_data['timer_job'] = job

# --- CHANGE: This handler now starts the quiz from the first question (index 0) ---
async def select_category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.replace("category_", "", 1)
    # Start quiz from the beginning (index 0)
    await send_question(query, context, category, question_index=0, edit=True)

# --- CHANGE: This handler now increments the index to get the next question ---
async def next_question_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = context.user_data.get('current_category')
    current_index = context.user_data.get('current_question_index', -1)
    
    if not category:
        await query.edit_message_text("⚠️ Please pick a category first.")
        return
    
    # Move to the next question index
    next_index = current_index + 1
    await send_question(query, context, category, question_index=next_index, edit=True)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user, user_id, display_name = query.from_user, str(query.from_user.id), get_user_display_name(update)

    job = context.user_data.get('timer_job')
    if job:
        try: job.schedule_removal()
        except JobLookupError: pass

    if context.user_data.get('answered_question', False): return
    context.user_data['answered_question'] = True

    selected = query.data
    current_q = context.user_data.get('current_question_data')
    if not current_q:
        await query.edit_message_text("⚠️ No active question. Start a quiz with /quiz.")
        return

    correct_answer = current_q.get('answer')
    if selected == correct_answer:
        if user_id not in scores: scores[user_id] = {"name": display_name, "score": 0}
        else: scores[user_id]["name"] = display_name
        scores[user_id]["score"] += 1
        save_scores_atomic(scores)
        response_text = f"✅ Correct! 🎉\nYour score: {scores[user_id]['score']}"
    else:
        if user_id not in scores:
            scores[user_id] = {"name": display_name, "score": 0}
            save_scores_atomic(scores)
        response_text = f"❌ Wrong! The correct answer was: {correct_answer}\nYour score: {scores[user_id]['score']}"

    await query.edit_message_text(response_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Next Question ➡️", callback_data="next_question")]]))

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not scores: await update.message.reply_text("📊 Leaderboard is empty!"); return
    sorted_scores = sorted(scores.items(), key=lambda x: x[1].get("score", 0), reverse=True)
    text, markup = build_leaderboard_page(sorted_scores, 0, str(update.effective_user.id))
    await update.message.reply_text(text, reply_markup=markup)

async def leaderboard_page_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    sorted_scores = sorted(scores.items(), key=lambda x: x[1].get("score", 0), reverse=True)
    text, markup = build_leaderboard_page(sorted_scores, page, str(query.from_user.id))
    await query.edit_message_text(text, reply_markup=markup)

async def show_score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id not in scores: scores[user_id] = {"name": get_user_display_name(update), "score": 0}
    await update.message.reply_text(f"🏆 Your current score is: {scores[user_id]['score']}")

# --------------------
# Main entry
# --------------------
def main():
    global quiz_data, scores
    quiz_data = load_questions()
    scores = load_scores()

    app = Application.builder().token(BOT_TOKEN).build()
    # Add all handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quiz", quiz))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("score", show_score))
    app.add_handler(MessageHandler(filters.Text("📚 Quiz Categories"), quiz))
    app.add_handler(MessageHandler(filters.Text("🏆 Leaderboard"), leaderboard))
    app.add_handler(MessageHandler(filters.Text("📊 My Score"), show_score))
    app.add_handler(CallbackQueryHandler(select_category_handler, pattern=r"^category_.*$"))
    app.add_handler(CallbackQueryHandler(next_question_handler, pattern=r"^next_question$"))
    app.add_handler(CallbackQueryHandler(leaderboard_page_handler, pattern=r"^leaderboard_page_\d+$"))
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^(?!next_question|leaderboard_page_|category_).*$"))

    logger.info("Bot is starting (Version 12.4 - Sequential)...")
    app.run_polling()

if __name__ == "__main__":
    main()