import sqlite3
import json
import os
DB_FILE = "quiz_bot.db"
QUESTIONS_JSON = "questions.json"
SCORES_JSON = "scores.json"
def migrate_questions(conn):
    if not os.path.exists(QUESTIONS_JSON): return 0
    with open(QUESTIONS_JSON, "r", encoding="utf-8") as f: data = json.load(f)
    inserted = 0
    cur = conn.cursor()
    for category, qlist in data.items():
        for q in qlist:
            options_json = json.dumps(q.get("options", []), ensure_ascii=False)
            try:
                cur.execute(
                    "INSERT OR IGNORE INTO questions (category, question_text, options, answer) VALUES (?, ?, ?, ?)",
                    (category, q.get("question"), options_json, q.get("answer"))
                )
                if cur.rowcount > 0: inserted += 1
            except Exception as e: print(f"Failed to insert question: {e}")
    conn.commit()
    return inserted
def migrate_scores(conn):
    if not os.path.exists(SCORES_JSON): return 0
    with open(SCORES_JSON, "r", encoding="utf-8") as f: data = json.load(f)
    inserted = 0
    cur = conn.cursor()
    for user_id, value in data.items():
        try:
            cur.execute(
                "INSERT OR REPLACE INTO users (user_id, name, score) VALUES (?, ?, ?)",
                (int(user_id), value.get("name", f"user{user_id}"), int(value.get("score", 0)))
            )
            inserted += 1
        except Exception as e: print(f"Failed to insert user {user_id}: {e}")
    conn.commit()
    return inserted
def main():
    if not os.path.exists(DB_FILE):
        print(f"DB file '{DB_FILE}' not found. Run database_setup.py first.")
        return
    conn = sqlite3.connect(DB_FILE)
    q_count = migrate_questions(conn)
    u_count = migrate_scores(conn)
    conn.close()
    print(f"Migration complete. Questions migrated: {q_count}. Users migrated/updated: {u_count}.")
if __name__ == "__main__":
    main()