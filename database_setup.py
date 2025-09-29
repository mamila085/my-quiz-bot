import sqlite3
DB_FILE = "quiz_bot.db"
def main():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, name TEXT NOT NULL, score INTEGER DEFAULT 0
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL,
        question_text TEXT NOT NULL UNIQUE, options TEXT NOT NULL, answer TEXT NOT NULL
    );
    """)
    conn.commit()
    conn.close()
    print(f"Database '{DB_FILE}' initialized successfully.")
if __name__ == "__main__":
    main()