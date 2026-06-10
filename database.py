import sqlite3
import json
from datetime import datetime, timedelta
import pytz

KYIV_TZ = pytz.timezone('Europe/Kiev')

class Database:
    def __init__(self, db_path='wordflow.db'):
        self.db_path = db_path
        self.init_db()

    def get_conn(self):
        return sqlite3.connect(self.db_path)

    def init_db(self):
        with self.get_conn() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    name TEXT,
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS words (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    word TEXT,
                    transcription TEXT,
                    translation TEXT,
                    example1 TEXT,
                    example2 TEXT,
                    repetitions INTEGER DEFAULT 0,
                    correct_answers INTEGER DEFAULT 0,
                    total_answers INTEGER DEFAULT 0,
                    next_review TEXT,
                    learned INTEGER DEFAULT 0,
                    added_date TEXT,
                    UNIQUE(user_id, word)
                );

                CREATE TABLE IF NOT EXISTS quiz_state (
                    user_id INTEGER PRIMARY KEY,
                    type TEXT,
                    remaining TEXT,
                    current_word TEXT,
                    correct_index TEXT,
                    question_type TEXT
                );
            ''')

    def add_user(self, user_id, name):
        with self.get_conn() as conn:
            conn.execute(
                'INSERT OR IGNORE INTO users (user_id, name, created_at) VALUES (?, ?, ?)',
                (user_id, name, datetime.now(KYIV_TZ).isoformat())
            )

    def get_all_users(self):
        with self.get_conn() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute('SELECT * FROM users').fetchall()]

    def add_word(self, user_id, word, transcription, translation, example1, example2):
        today = datetime.now(KYIV_TZ).strftime('%Y-%m-%d')
        with self.get_conn() as conn:
            conn.execute('''
                INSERT OR IGNORE INTO words
                (user_id, word, transcription, translation, example1, example2, next_review, added_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, word, transcription, translation, example1, example2, today, today))

    def word_exists(self, user_id, word):
        with self.get_conn() as conn:
            r = conn.execute(
                'SELECT id FROM words WHERE user_id=? AND word=?', (user_id, word)
            ).fetchone()
            return r is not None

    def get_active_words(self, user_id):
        with self.get_conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT * FROM words WHERE user_id=? AND learned=0 ORDER BY added_date DESC',
                (user_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_todays_words(self, user_id):
        today = datetime.now(KYIV_TZ).strftime('%Y-%m-%d')
        with self.get_conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT * FROM words WHERE user_id=? AND learned=0 AND next_review<=?',
                (user_id, today)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_word(self, user_id, word):
        with self.get_conn() as conn:
            conn.row_factory = sqlite3.Row
            r = conn.execute(
                'SELECT * FROM words WHERE user_id=? AND word=?', (user_id, word)
            ).fetchone()
            return dict(r) if r else None

    def get_all_translations(self, user_id):
        with self.get_conn() as conn:
            rows = conn.execute(
                'SELECT translation FROM words WHERE user_id=? AND learned=0', (user_id,)
            ).fetchall()
            return [r[0] for r in rows]

    def get_all_words(self, user_id):
        with self.get_conn() as conn:
            rows = conn.execute(
                'SELECT word FROM words WHERE user_id=? AND learned=0', (user_id,)
            ).fetchall()
            return [r[0] for r in rows]

    def update_repetition(self, user_id, word, correct: bool):
        word_data = self.get_word(user_id, word)
        if not word_data:
            return

        reps = word_data['repetitions'] + (1 if correct else 0)
        total = word_data['total_answers'] + 1
        correct_count = word_data['correct_answers'] + (1 if correct else 0)

        if reps <= 1:
            days = 1
        elif reps <= 3:
            days = 3
        elif reps <= 5:
            days = 7
        else:
            days = 30

        next_review = (datetime.now(KYIV_TZ) + timedelta(days=days)).strftime('%Y-%m-%d')

        with self.get_conn() as conn:
            conn.execute('''
                UPDATE words SET repetitions=?, correct_answers=?, total_answers=?, next_review=?
                WHERE user_id=? AND word=?
            ''', (reps, correct_count, total, next_review, user_id, word))

    def mark_learned(self, user_id, word):
        with self.get_conn() as conn:
            conn.execute(
                'UPDATE words SET learned=1 WHERE user_id=? AND word=?', (user_id, word)
            )

    def get_stats(self, user_id):
        with self.get_conn() as conn:
            active = conn.execute(
                'SELECT COUNT(*) FROM words WHERE user_id=? AND learned=0', (user_id,)
            ).fetchone()[0]
            learned = conn.execute(
                'SELECT COUNT(*) FROM words WHERE user_id=? AND learned=1', (user_id,)
            ).fetchone()[0]
            reps = conn.execute(
                'SELECT SUM(total_answers), SUM(correct_answers) FROM words WHERE user_id=?', (user_id,)
            ).fetchone()
            total_ans = reps[0] or 0
            correct_ans = reps[1] or 0
            pct = int(correct_ans / total_ans * 100) if total_ans > 0 else 0
            return {'active': active, 'learned': learned, 'total_reps': total_ans, 'correct': pct}

    def set_quiz_state(self, user_id, quiz_type, remaining):
        with self.get_conn() as conn:
            conn.execute('''
                INSERT OR REPLACE INTO quiz_state (user_id, type, remaining)
                VALUES (?, ?, ?)
            ''', (user_id, quiz_type, json.dumps(remaining or [])))

    def get_quiz_state(self, user_id):
        with self.get_conn() as conn:
            r = conn.execute(
                'SELECT * FROM quiz_state WHERE user_id=?', (user_id,)
            ).fetchone()
            if not r:
                return None
            return {
                'type': r[1],
                'remaining': json.loads(r[2]) if r[2] else [],
                'current_word': r[3],
                'correct_index': r[4],
                'question_type': r[5]
            }

    def set_current_question(self, user_id, word, correct_index, question_type):
        with self.get_conn() as conn:
            conn.execute('''
                UPDATE quiz_state SET current_word=?, correct_index=?, question_type=?
                WHERE user_id=?
            ''', (str(word), str(correct_index), question_type, user_id))

    def get_current_question(self, user_id):
        with self.get_conn() as conn:
            r = conn.execute(
                'SELECT current_word, correct_index, question_type FROM quiz_state WHERE user_id=?',
                (user_id,)
            ).fetchone()
            if not r or not r[0]:
                return None
            return {'current_word': r[0], 'correct_index': r[1], 'question_type': r[2]}

    def remove_from_remaining(self, user_id, word):
        state = self.get_quiz_state(user_id)
        if not state:
            return
        remaining = [w for w in state['remaining'] if w != word]
        with self.get_conn() as conn:
            conn.execute(
                'UPDATE quiz_state SET remaining=?, current_word=NULL WHERE user_id=?',
                (json.dumps(remaining), user_id)
            )

    def clear_quiz_state(self, user_id):
        with self.get_conn() as conn:
            conn.execute('DELETE FROM quiz_state WHERE user_id=?', (user_id,))
