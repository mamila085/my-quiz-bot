"""
Quiz Bot - Version 10
---------------------
Enhancements over V9:
1) Externalized quiz questions into questions.json (loaded at startup).
2) Persistent main menu (ReplyKeyboardMarkup) for quick access.
3) Retains production features: env vars, logging, atomic persistence, paginated leaderboard.

SETUP:
- Create a virtual env and install requirements (python-telegram-bot, python-dotenv).
- Put your token in .env: BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"
- Create questions.json (see provided file content).
- Run: python bot.py
"""

import os
import json
import math
import logging
import random
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ------------------------------------------------------------------------------
# Environment & Logging
# ------------------------------------------------------------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("CRITICAL: BOT_TOKEN not found. Set it in your .env file.")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("quiz-bot")

# ------------------------------------------------------------------------------
# Constants & Files
# ------------------------------------------------------------------------------
SCORES_FILE = "scores.json"
QUESTIONS_FILE = "questions.json"
PAGE_SIZE = 5

# Main Menu Button Labels
BTN_CATEGORIES = "?? Quiz Categories"
BTN_LEADERBOARD = "?? Leaderboard"
BTN_MY_SCORE = "?? My Score"

# ------------------------------------------------------------------------------
# Global State
# ------------------------------------------------------------------------------
scores: dict[str, dict] = {}  # { user_id: {"name": str, "score": int} }
quiz_data: dict[str, list] = {}  # Loaded from questions.json

# ------------------------------------------------------------------------------
# Persistence Helpers
# ------------------------------------------------------------------------------
def load_scores() -> dict:
    """Load scores with JSON error handling."""
    if not os.path.exists(SCORES_FILE):
        logger.info("scores.json not found. Starting with empty scores.")
        return {}
    try:
        with open(SCORES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        logger.warning("Warning: scores.json is corrupted or empty. Starting fresh.")
        return {}
    except Exception as e:
        logger.exception("Unexpected error while loading scores: %s", e)
        return {}

def save_scores(data: dict) -> None:
    """Atomically write scores to disk to prevent corruption."""
    tmp = SCORES_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, SCORES_FILE)
    except Exception as e:
        logger.exception("Failed to save scores: %s", e)

def load_questions() -> dict:
    """
    Load quiz questions from QUESTIONS_FILE.
    Critical: if missing or invalid, log and exit (bot cannot function without questions).
    """
    if not os.path.exists(QUESTIONS_FILE):
        logger.critical("CRITICAL: questions.json not found. The bot cannot start.")
        raise SystemExit(1)
    try:
        with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict) or not data:
                logger.critical("CRITICAL: questions.json is empty or malformed.")
                raise SystemExit(1)
            return data
    except json.JSONDecodeError:
        logger.critical("CRITICAL: questions.json contains invalid JSON.")
        raise SystemExit(1)
    except Exception as e:
        logger.critical("CRITICAL: Failed to load questions.json: %s", e)
        raise SystemExit(1)

# Load global data at import time (fail fast if broken)
scores = load_scores()
quiz_data = load_questions()

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------
def get_user_name(update: Update) -> str:
    """Best-effort user display name: username > first_name > 'Player'."""
    user = update.effective_user
    if not user:
        return "Player"
    if user.username:
        return f"@{user.username}"
    if user.first_name:
        return user.first_name
    return "Player"

def get_new_question(category: str) -> dict | None:
    """Return a random question from the chosen category."""
    if category not in quiz_data or not quiz_data[category]:
        return None
    return random.choice(quiz_data[category])

def build_main_menu() -> ReplyKeyboardMarkup:
    """Persistent reply keyboard with main actions."""
    keyboard = [
        [KeyboardButton(BTN_CATEGORIES), KeyboardButton(BTN_LEADERBOARD)],
        [KeyboardButton(BTN_MY_SCORE)],
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        is_persistent=True,     # keeps the keyboard visible
        one_time_keyboard=False # do not hide after one use
    )

def build_category_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard with dynamic category buttons from quiz_data keys."""
    buttons = [[InlineKeyboardButton(cat.capitalize(), callback_data=f"category_{cat}")]
               for cat in quiz_data.keys()]
    return InlineKeyboardMarkup(buttons)

def build_leaderboard_page(sorted_scores, page: int, requester_user_id: str):
    """Create the text and nav buttons for a specific leaderboard page."""
    total_players = len(sorted_scores)
    total_pages = max(1, math.ceil(total_players / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    players_on_page = sorted_scores[start:end]

    header = f"?? Leaderboard (Page {page+1} of {total_pages}) ??\n\n"
    lines = []
    for i, (uid, data) in enumerate(players_on_page, start=start + 1):
        name = data.get("name", f"User {uid}")
        score_value = data.get("score", 0)
        if str(requester_user_id) == uid:
            lines.append(f"{i}. {name} (You) - {score_value} points ??")
        else:
            lines.append(f"{i}. {name} - {score_value} points")

    leaderboard_text = header + ("\n".join(lines) if lines else "No players to show yet.")

    buttons_row = []
    if page > 0:
        buttons_row.append(
            InlineKeyboardButton("?? Previous", callback_data=f"leaderboard_page_{page-1}")
        )
    if page < total_pages - 1:
        buttons_row.append(
            InlineKeyboardButton("Next ??", callback_data=f"leaderboard_page_{page+1}")
        )

    reply_markup = InlineKeyboardMarkup([buttons_row]) if buttons_row else None
    return leaderboard_text, reply_markup

# ------------------------------------------------------------------------------
# Command & Callback Handlers
# ------------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initialize user record, send welcome, and show persistent main menu."""
    user = update.effective_user
    if not user:
        return
    user_id = str(user.id)
    user_name = get_user_name(update)

    if user_id not in scores:
        scores[user_id] = {"name": user_name, "score": 0}
    else:
        scores[user_id]["name"] = user_name
    save_scores(scores)

    await update.message.reply_text(
        "Welcome to the Quiz Bot! ??\n"
        "Use the menu below or commands:\n"
        "* /quiz - choose a category\n"
        "* /score - your score\n"
        "* /leaderboard - leaderboard (paginated)",
        reply_markup=build_main_menu(),
    )

async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show category choices (inline buttons)."""
    await update.message.reply_text(
        "?? Please choose a category to start the quiz:",
        reply_markup=build_category_keyboard(),
    )

async def select_category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle category selection and send the first question of that category."""
    query = update.callback_query
    await query.answer()

    category = query.data.replace("category_", "")
    if category not in quiz_data:
        await query.edit_message_text("?? Unknown category. Please try /quiz again.")
        return

    context.user_data["current_category"] = category

    question_data = get_new_question(category)
    if not question_data:
        await query.edit_message_text("?? No questions available in this category right now.")
        return

    context.user_data["current_question"] = question_data

    keyboard = [[InlineKeyboardButton(opt, callback_data=opt)] for opt in question_data["options"]]
    await query.edit_message_text(
        f"?? Category: {category.capitalize()}\n\n{question_data['question']}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle answer button clicks."""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    # Keep names up-to-date
    name = query.from_user.username and f"@{query.from_user.username}" or query.from_user.first_name or "Player"

    question_data = context.user_data.get("current_question")
    if not question_data:
        await query.edit_message_text("Please start a new quiz with /quiz.")
        return

    selected = query.data
    correct = question_data["answer"]

    if user_id not in scores:
        scores[user_id] = {"name": name, "score": 0}
    scores[user_id]["name"] = name

    if selected == correct:
        scores[user_id]["score"] += 1
        response = f"? Correct! ?? Your score: {scores[user_id]['score']}"
    else:
        response = f"? Wrong! The correct answer was: {correct}\nYour score: {scores[user_id]['score']}"

    save_scores(scores)

    keyboard = [[InlineKeyboardButton("Next Question ??", callback_data="next_question")]]
    await query.edit_message_text(response, reply_markup=InlineKeyboardMarkup(keyboard))

async def next_question_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Serve the next question from the current category."""
    query = update.callback_query
    await query.answer()

    if query.data != "next_question":
        return

    category = context.user_data.get("current_category")
    if not category:
        await query.edit_message_text("?? Please select a category with /quiz first.")
        return

    question_data = get_new_question(category)
    if not question_data:
        await query.edit_message_text("?? No more questions available right now.")
        return

    context.user_data["current_question"] = question_data

    keyboard = [[InlineKeyboardButton(opt, callback_data=opt)] for opt in question_data["options"]]
    await query.edit_message_text(
        question_data["question"],
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the user's current score."""
    user_id = str(update.effective_user.id)
    if user_id not in scores:
        scores[user_id] = {"name": get_user_name(update), "score": 0}
        save_scores(scores)
    await update.message.reply_text(f"?? Your current score is: {scores[user_id]['score']}")

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the first page of the paginated leaderboard."""
    if not scores:
        await update.message.reply_text("?? Leaderboard is empty! Start playing with /quiz.")
        return
    sorted_scores = sorted(scores.items(), key=lambda x: x[1].get("score", 0), reverse=True)
    text, markup = build_leaderboard_page(sorted_scores, page=0, requester_user_id=str(update.effective_user.id))
    await update.message.reply_text(text, reply_markup=markup)

async def leaderboard_page_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle pagination for the leaderboard."""
    query = update.callback_query
    await query.answer()

    try:
        page = int(query.data.split("_")[-1])
    except ValueError:
        page = 0

    sorted_scores = sorted(scores.items(), key=lambda x: x[1].get("score", 0), reverse=True)
    text, markup = build_leaderboard_page(sorted_scores, page=page, requester_user_id=str(query.from_user.id))
    await query.edit_message_text(text, reply_markup=markup)

# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quiz", quiz))
    app.add_handler(CommandHandler("score", score))
    app.add_handler(CommandHandler("leaderboard", leaderboard))

    # Persistent Menu (text button) handlers
    app.add_handler(MessageHandler(filters.Text([BTN_CATEGORIES]), quiz))
    app.add_handler(MessageHandler(filters.Text([BTN_LEADERBOARD]), leaderboard))
    app.add_handler(MessageHandler(filters.Text([BTN_MY_SCORE]), score))

    # Callbacks
    app.add_handler(CallbackQueryHandler(select_category_handler, pattern=r"^category_.*$"))
    app.add_handler(CallbackQueryHandler(next_question_handler, pattern=r"^next_question$"))
    app.add_handler(CallbackQueryHandler(leaderboard_page_handler, pattern=r"^leaderboard_page_\d+$"))
    # Answer option buttons (exclude other special callbacks)
    app.add_handler(CallbackQueryHandler(
        button_handler,
        pattern=r"^(?!next_question|leaderboard_page_|category_).*$"
    ))

    logger.info("Bot is running... Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
