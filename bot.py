"""
Quiz Bot - Version 7 (Production-Ready)
---------------------------------------
What's new versus V6:
1) SECURITY: Reads BOT_TOKEN from environment variables via python-dotenv (.env file).
2) ERROR HANDLING: Robust JSON loading with JSONDecodeError fallback; atomic writes for scores.
3) DEPENDENCY MGMT: requirements.txt provided; versions pinned.
4) CLEANUP: Logging added; clear comments; paginated leaderboard retained.

-------------------------------------------------------------------------------
SETUP INSTRUCTIONS
-------------------------------------------------------------------------------
1) Create and activate a virtual environment (recommended):
   python -m venv .venv
   # Windows: .venv/Scripts/activate
   # macOS/Linux: source .venv/bin/activate

2) Install dependencies:
   pip install -r requirements.txt

3) Create a .env file in the project root (copy from .env.example) and set:
   BOT_TOKEN="YOUR_BOT_TOKEN_HERE"

4) Run the bot:
   python bot.py
   (replace "bot.py" with this file name)

Note: Never commit your real .env file to version control.
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import random
import json
import os
import math
import logging
from tempfile import NamedTemporaryFile
from dotenv import load_dotenv

# ------------------------------------------------------------------------------
# Logging configuration (use INFO in production; DEBUG during development)
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger("quiz-bot")

# ------------------------------------------------------------------------------
# Security: Environment variable loading for BOT_TOKEN
# ------------------------------------------------------------------------------
# Loads variables from .env into environment (if present).
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")  # DO NOT hardcode tokens in code!

if not BOT_TOKEN:
    # Fail fast with a clear message to avoid starting a bot without a token.
    logger.critical(
        "BOT_TOKEN not found. Create a .env file with BOT_TOKEN or set it in the environment."
    )
    raise SystemExit(1)

# ------------------------------------------------------------------------------
# Constants / Files
# ------------------------------------------------------------------------------
SCORES_FILE = "scores.json"
PAGE_SIZE = 5  # Number of players per leaderboard page

# ------------------------------------------------------------------------------
# Quiz Data
# ------------------------------------------------------------------------------
quiz_data = [
    {
        "question": "What is the capital city of Ethiopia?",
        "options": ["Hawassa", "Addis Ababa", "Mekelle", "Bahir Dar"],
        "answer": "Addis Ababa",
    },
    {
        "question": "Which planet is known as the Red Planet?",
        "options": ["Earth", "Venus", "Mars", "Jupiter"],
        "answer": "Mars",
    },
    {
        "question": "Who developed the theory of relativity?",
        "options": ["Isaac Newton", "Albert Einstein", "Nikola Tesla", "Galileo Galilei"],
        "answer": "Albert Einstein",
    },
]

# ------------------------------------------------------------------------------
# Persistence Helpers
# ------------------------------------------------------------------------------
def load_scores():
    """
    Load scores from SCORES_FILE (JSON).
    - Returns {} if file does not exist.
    - If file is empty/corrupted (JSONDecodeError), logs a warning and returns {}.
    """
    if not os.path.exists(SCORES_FILE):
        logger.info("scores.json not found. Starting with empty scores.")
        return {}

    try:
        with open(SCORES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        logger.warning(
            "Warning: scores.json is corrupted or empty. Starting with a fresh slate."
        )
        return {}
    except Exception as e:
        logger.exception("Unexpected error while loading scores: %s", e)
        return {}

def _atomic_write_json(file_path: str, data: dict):
    """
    Write JSON atomically:
    - Write to a temporary file first, then replace the target.
    - Prevents partial/corrupt writes during crashes or interruptions.
    """
    dir_name = os.path.dirname(os.path.abspath(file_path)) or "."
    with NamedTemporaryFile("w", delete=False, dir=dir_name, encoding="utf-8") as tmp:
        json.dump(data, tmp, ensure_ascii=False, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, file_path)

def save_scores(scores: dict):
    """Persist scores to SCORES_FILE using atomic write."""
    try:
        _atomic_write_json(SCORES_FILE, scores)
    except Exception as e:
        logger.exception("Failed to save scores: %s", e)

# Global in-memory cache (loaded at startup)
scores = load_scores()

# ------------------------------------------------------------------------------
# Utility Helpers
# ------------------------------------------------------------------------------
def get_new_question():
    return random.choice(quiz_data)

def get_user_name(update: Update):
    """Return the best available name (username > first_name > 'Player')."""
    user = update.effective_user
    if not user:
        return "Player"
    if user.username:
        return f"@{user.username}"
    if user.first_name:
        return user.first_name
    return "Player"

def build_leaderboard_page(sorted_scores, page: int, requester_user_id: str):
    """
    Create text + navigation buttons for the requested leaderboard page.
    - sorted_scores: list[(user_id, {"name": str, "score": int})] sorted desc by score
    - page: 0-based page index
    - requester_user_id: to mark "(You)" inline in the display
    """
    total_players = len(sorted_scores)
    total_pages = max(1, math.ceil(total_players / PAGE_SIZE))
    # Clamp page into valid range to avoid index errors.
    page = max(0, min(page, total_pages - 1))

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    players_on_page = sorted_scores[start:end]

    header = f"?? Leaderboard (Page {page+1} of {total_pages}) ??\n\n"
    lines = []
    for i, (uid, data) in enumerate(players_on_page, start=start + 1):
        name = data.get("name", "Player")
        score_value = data.get("score", 0)
        if uid == requester_user_id:
            lines.append(f"{i}. {name} (You) - {score_value} points ??")
        else:
            lines.append(f"{i}. {name} - {score_value} points")

    leaderboard_text = header + ("\n".join(lines) if lines else "No players to show yet.")

    # Navigation buttons based on position
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
# Command Handlers
# ------------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initialize user record (name+score), then show help text."""
    user = update.effective_user
    if not user:
        return

    user_id = str(user.id)
    user_name = get_user_name(update)

    # Initialize or refresh user's name
    if user_id not in scores:
        scores[user_id] = {"name": user_name, "score": 0}
    else:
        scores[user_id]["name"] = user_name

    save_scores(scores)

    await update.message.reply_text(
        "Welcome to the Quiz Bot! ??\n"
        "Send /quiz to get your first question.\n"
        "Send /score to check your score.\n"
        "Send /leaderboard to see the leaderboard (paginated)."
    )

async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a random multiple-choice question."""
    question_data = get_new_question()
    context.user_data["current_question"] = question_data

    keyboard = [
        [InlineKeyboardButton(opt, callback_data=opt)]
        for opt in question_data["options"]
    ]
    await update.message.reply_text(
        question_data["question"], reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle answer choice button clicks."""
    query = update.callback_query
    await query.answer()

    user = query.from_user
    user_id = str(user.id)
    user_name = get_user_name(update)

    question_data = context.user_data.get("current_question")
    if not question_data:
        await query.edit_message_text("Please start a new quiz with /quiz.")
        return

    selected = query.data
    correct = question_data["answer"]

    # Ensure user is tracked; always refresh name
    if user_id not in scores:
        scores[user_id] = {"name": user_name, "score": 0}
    scores[user_id]["name"] = user_name

    if selected == correct:
        scores[user_id]["score"] += 1
        response = f"? Correct! ?? Your score: {scores[user_id]['score']}"
    else:
        response = (
            f"? Wrong! The correct answer was: {correct}\n"
            f"Your score: {scores[user_id]['score']}"
        )

    save_scores(scores)

    keyboard = [[InlineKeyboardButton("Next Question ??", callback_data="next_question")]]
    await query.edit_message_text(response, reply_markup=InlineKeyboardMarkup(keyboard))

async def next_question_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Serve next quiz question."""
    query = update.callback_query
    await query.answer()

    if query.data != "next_question":
        return

    question_data = get_new_question()
    context.user_data["current_question"] = question_data

    keyboard = [
        [InlineKeyboardButton(opt, callback_data=opt)]
        for opt in question_data["options"]
    ]
    await query.edit_message_text(
        question_data["question"], reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's current score."""
    user = update.effective_user
    if not user:
        return

    user_id = str(user.id)
    if user_id not in scores:
        scores[user_id] = {"name": get_user_name(update), "score": 0}
        save_scores(scores)

    await update.message.reply_text(f"?? Your current score is: {scores[user_id]['score']}")

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the first page of the paginated leaderboard."""
    if not scores:
        await update.message.reply_text("?? Leaderboard is empty! Start playing with /quiz.")
        return

    # Sort by score (descending)
    sorted_scores = sorted(scores.items(), key=lambda x: x[1].get("score", 0), reverse=True)
    text, markup = build_leaderboard_page(sorted_scores, page=0, requester_user_id=str(update.effective_user.id))
    await update.message.reply_text(text, reply_markup=markup)

# ------------------------------------------------------------------------------
# Callback Handler for Paginated Leaderboard
# ------------------------------------------------------------------------------
async def leaderboard_page_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle pagination button presses for the leaderboard."""
    query = update.callback_query
    await query.answer()

    try:
        page = int(query.data.split("_")[-1])
    except ValueError:
        page = 0

    # Re-sort on each request in case scores changed in the meantime
    sorted_scores = sorted(scores.items(), key=lambda x: x[1].get("score", 0), reverse=True)
    text, markup = build_leaderboard_page(sorted_scores, page=page, requester_user_id=str(query.from_user.id))
    await query.edit_message_text(text, reply_markup=markup)

# ------------------------------------------------------------------------------
# Main Entrypoint
# ------------------------------------------------------------------------------
def main():
    try:
        app = Application.builder().token(BOT_TOKEN).build()

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("quiz", quiz))
        app.add_handler(CommandHandler("score", score))
        app.add_handler(CommandHandler("leaderboard", leaderboard))

        # Answer buttons (exclude leaderboard pagination & next_question)
        app.add_handler(CallbackQueryHandler(
            button_handler,
            pattern=r"^(?!next_question|leaderboard_page_).*$"
        ))

        # Next question
        app.add_handler(CallbackQueryHandler(
            next_question_handler,
            pattern=r"^next_question$"
        ))

        # Leaderboard pagination
        app.add_handler(CallbackQueryHandler(
            leaderboard_page_handler,
            pattern=r"^leaderboard_page_\d+$"
        ))

        logger.info("Bot is running... Press Ctrl+C to stop.")
        app.run_polling()
    except Exception as e:
        logger.exception("Fatal error while starting the bot: %s", e)
        raise

if __name__ == "__main__":
    main()