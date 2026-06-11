import os
import sqlite3
import json
from datetime import datetime, timedelta
import pytz

TZ = pytz.timezone('Asia/Ho_Chi_Minh')
DB_PATH = os.environ.get('DB_PATH', 'wordflow.db')

# Розмір партії за рівнем (рівень також визначає режим тесту в bot.py)
LEVEL_BATCH = {1: 4, 2: 8, 3: 12, 4: 15}
DEFAULT_BATCH = 10


def _now():
    return datetime.now(TZ)


def _today():
    return _now().strftime('%Y-%m-%d')


class Database:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        with self._conn() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    name TEXT,
                    created_at TEXT,
                    level INTEGER,
                    current_streak INTEGER DEFAULT 0,
                    best_streak INTEGER DEFAULT 0,
                    last_active_date TEXT,
                    paused_until TEXT,
                    official_cooldown TEXT,
                    timezone TEXT DEFAULT 'Asia/Ho_Chi_Minh'
                );

                CREATE TABLE IF NOT EXISTS words (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    word TEXT,
                    transcription TEXT,
                    translation TEXT,
                    example1 TEXT,
                    example2 TEXT,
                    distractors_en TEXT,
                    distractors_uk TEXT,
                    batch_number INTEGER,
                    correct_answers INTEGER DEFAULT 0,
                    total_answers INTEGER DEFAULT 0,
                    last_wrong TEXT,
                    learned INTEGER DEFAULT 0,
                    added_date TEXT
                );

                CREATE TABLE IF NOT EXISTS batches (
                    user_id INTEGER,
                    batch_number INTEGER,
                    status TEXT DEFAULT 'locked',
                    size INTEGER DEFAULT 10,
                    PRIMARY KEY (user_id, batch_number)
                );

                CREATE TABLE IF NOT EXISTS daily_stats (
                    user_id INTEGER,
                    date TEXT,
                    answered INTEGER DEFAULT 0,
                    correct INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, date)
                );

                CREATE TABLE IF NOT EXISTS quiz_session (
                    user_id INTEGER PRIMARY KEY,
                    kind TEXT,
                    remaining TEXT,
                    total INTEGER,
                    current_word_id INTEGER,
                    current_q TEXT,
                    results TEXT
                );
            ''')
        self._migrate()

    def _migrate(self):
        """Безпечно додає нові колонки до вже існуючої бази (v1 -> v2)."""
        with self._conn() as conn:
            def cols(table):
                return {r['name'] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}

            ucols = cols('users')
            if 'level' not in ucols:
                conn.execute('ALTER TABLE users ADD COLUMN level INTEGER')
            if 'timezone' not in ucols:
                conn.execute("ALTER TABLE users ADD COLUMN timezone TEXT DEFAULT 'Asia/Ho_Chi_Minh'")

            wcols = cols('words')
            if 'distractors_en' not in wcols:
                conn.execute('ALTER TABLE words ADD COLUMN distractors_en TEXT')
            if 'distractors_uk' not in wcols:
                conn.execute('ALTER TABLE words ADD COLUMN distractors_uk TEXT')

            bcols = cols('batches')
            if 'size' not in bcols:
                conn.execute('ALTER TABLE batches ADD COLUMN size INTEGER DEFAULT 10')

            qcols = cols('quiz_session')
            if 'current_q' not in qcols:
                conn.execute('ALTER TABLE quiz_session ADD COLUMN current_q TEXT')

    # ---------------- USERS ----------------
    def add_user(self, user_id, name):
        with self._conn() as conn:
            conn.execute(
                'INSERT OR IGNORE INTO users (user_id, name, created_at) VALUES (?,?,?)',
                (user_id, name, _now().isoformat())
            )

    def get_user(self, user_id):
        with self._conn() as conn:
            r = conn.execute('SELECT * FROM users WHERE user_id=?', (user_id,)).fetchone()
            return dict(r) if r else None

    def get_all_users(self):
        with self._conn() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM users').fetchall()]

    def get_level(self, user_id):
        u = self.get_user(user_id)
        if not u:
            return None
        return u['level']

    def set_level(self, user_id, level):
        with self._conn() as conn:
            conn.execute('UPDATE users SET level=? WHERE user_id=?', (int(level), user_id))

    def get_timezone(self, user_id):
        u = self.get_user(user_id)
        if not u:
            return 'Asia/Ho_Chi_Minh'
        return u.get('timezone') or 'Asia/Ho_Chi_Minh'

    def set_timezone(self, user_id, tz):
        with self._conn() as conn:
            conn.execute('UPDATE users SET timezone=? WHERE user_id=?', (tz, user_id))

    def batch_size_for(self, user_id):
        lvl = self.get_level(user_id) or 3
        return LEVEL_BATCH.get(lvl, DEFAULT_BATCH)

    # ---------------- WORDS / BATCHES ----------------
    def word_exists(self, user_id, word):
        with self._conn() as conn:
            r = conn.execute(
                'SELECT id FROM words WHERE user_id=? AND lower(word)=lower(?) AND learned=0',
                (user_id, word)
            ).fetchone()
            return r is not None

    def _last_open_batch(self, conn, user_id):
        """Останя незавершена партія, у якій ще є вільні місця (cnt < size)."""
        rows = conn.execute('''
            SELECT b.batch_number, b.size,
                   (SELECT COUNT(*) FROM words w
                    WHERE w.user_id=b.user_id AND w.batch_number=b.batch_number AND w.learned=0) AS cnt
            FROM batches b
            WHERE b.user_id=? AND b.status!='completed'
            ORDER BY b.batch_number DESC
        ''', (user_id,)).fetchall()
        for r in rows:
            size = r['size'] or DEFAULT_BATCH
            if r['cnt'] < size:
                return r['batch_number'], r['cnt'], size
        return None, 0, None

    def _has_active_batch(self, conn, user_id):
        r = conn.execute(
            "SELECT 1 FROM batches WHERE user_id=? AND status='active' LIMIT 1", (user_id,)
        ).fetchone()
        return r is not None

    def add_words(self, user_id, words_data, batch_size=None):
        if batch_size is None:
            batch_size = self.batch_size_for(user_id)
        today = _today()
        added = 0
        with self._conn() as conn:
            batch_num, count, cur_size = self._last_open_batch(conn, user_id)
            if batch_num is None:
                maxb = conn.execute(
                    'SELECT COALESCE(MAX(batch_number),0) AS m FROM batches WHERE user_id=?',
                    (user_id,)
                ).fetchone()['m']
                batch_num = maxb + 1
                count = 0
                cur_size = batch_size
                status = 'active' if not self._has_active_batch(conn, user_id) else 'locked'
                conn.execute(
                    'INSERT OR IGNORE INTO batches (user_id, batch_number, status, size) VALUES (?,?,?,?)',
                    (user_id, batch_num, status, cur_size)
                )

            for wd in words_data:
                if self.word_exists(user_id, wd['word']):
                    continue
                if count >= cur_size:
                    batch_num += 1
                    count = 0
                    cur_size = batch_size
                    status = 'active' if not self._has_active_batch(conn, user_id) else 'locked'
                    conn.execute(
                        'INSERT OR IGNORE INTO batches (user_id, batch_number, status, size) VALUES (?,?,?,?)',
                        (user_id, batch_num, status, cur_size)
                    )
                conn.execute('''
                    INSERT INTO words
                    (user_id, word, transcription, translation, example1, example2,
                     distractors_en, distractors_uk, batch_number, added_date)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                ''', (user_id, wd['word'], wd['transcription'], wd['translation'],
                      wd['example1'], wd['example2'],
                      json.dumps(wd.get('distractors_en', []), ensure_ascii=False),
                      json.dumps(wd.get('distractors_uk', []), ensure_ascii=False),
                      batch_num, today))
                count += 1
                added += 1
        return added

    def get_active_batch_number(self, user_id):
        with self._conn() as conn:
            r = conn.execute(
                "SELECT batch_number FROM batches WHERE user_id=? AND status='active' "
                "ORDER BY batch_number LIMIT 1", (user_id,)
            ).fetchone()
            return r['batch_number'] if r else None

    def _row_to_word(self, r):
        w = dict(r)
        try:
            w['distractors_en'] = json.loads(w['distractors_en']) if w.get('distractors_en') else []
        except Exception:
            w['distractors_en'] = []
        try:
            w['distractors_uk'] = json.loads(w['distractors_uk']) if w.get('distractors_uk') else []
        except Exception:
            w['distractors_uk'] = []
        return w

    def get_active_batch_words(self, user_id):
        bn = self.get_active_batch_number(user_id)
        if bn is None:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT * FROM words WHERE user_id=? AND batch_number=? AND learned=0 ORDER BY id',
                (user_id, bn)
            ).fetchall()
            return [self._row_to_word(r) for r in rows]

    def get_all_active_words(self, user_id):
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT * FROM words WHERE user_id=? AND learned=0 ORDER BY batch_number, id',
                (user_id,)
            ).fetchall()
            return [self._row_to_word(r) for r in rows]

    def get_word_by_id(self, user_id, word_id):
        with self._conn() as conn:
            r = conn.execute(
                'SELECT * FROM words WHERE user_id=? AND id=?', (user_id, word_id)
            ).fetchone()
            return self._row_to_word(r) if r else None

    def mark_learned(self, user_id, word_id):
        with self._conn() as conn:
            conn.execute('UPDATE words SET learned=1 WHERE user_id=? AND id=?', (user_id, word_id))

    def delete_word(self, user_id, word_id):
        with self._conn() as conn:
            conn.execute('DELETE FROM words WHERE user_id=? AND id=?', (user_id, word_id))

    def get_batches(self, user_id):
        with self._conn() as conn:
            rows = conn.execute('''
                SELECT b.batch_number, b.status, b.size,
                   (SELECT COUNT(*) FROM words w WHERE w.user_id=b.user_id
                    AND w.batch_number=b.batch_number) AS total,
                   (SELECT COUNT(*) FROM words w WHERE w.user_id=b.user_id
                    AND w.batch_number=b.batch_number AND w.learned=1) AS learned
                FROM batches b WHERE b.user_id=? ORDER BY b.batch_number
            ''', (user_id,)).fetchall()
            return [dict(r) for r in rows]

    def unlock_next_batch(self, user_id):
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT batch_number FROM batches WHERE user_id=? AND status='active' "
                "ORDER BY batch_number LIMIT 1", (user_id,)
            ).fetchone()
            if cur:
                conn.execute(
                    "UPDATE batches SET status='completed' WHERE user_id=? AND batch_number=?",
                    (user_id, cur['batch_number'])
                )
            nxt = conn.execute(
                "SELECT batch_number FROM batches WHERE user_id=? AND status='locked' "
                "ORDER BY batch_number LIMIT 1", (user_id,)
            ).fetchone()
            if nxt:
                conn.execute(
                    "UPDATE batches SET status='active' WHERE user_id=? AND batch_number=?",
                    (user_id, nxt['batch_number'])
                )
                return nxt['batch_number']
            return None

    # ---------------- ANSWERS / STREAK / DAILY ----------------
    def record_answer(self, user_id, word_id, correct, user_typed=None):
        today = _today()
        with self._conn() as conn:
            conn.execute(
                'UPDATE words SET total_answers=total_answers+1, correct_answers=correct_answers+? '
                'WHERE user_id=? AND id=?',
                (1 if correct else 0, user_id, word_id)
            )
            if not correct and user_typed:
                conn.execute('UPDATE words SET last_wrong=? WHERE user_id=? AND id=?',
                             (user_typed, user_id, word_id))
            conn.execute('INSERT OR IGNORE INTO daily_stats (user_id, date) VALUES (?,?)',
                         (user_id, today))
            conn.execute(
                'UPDATE daily_stats SET answered=answered+1, correct=correct+? '
                'WHERE user_id=? AND date=?',
                (1 if correct else 0, user_id, today)
            )
            u = conn.execute('SELECT * FROM users WHERE user_id=?', (user_id,)).fetchone()
            last = u['last_active_date']
            cur = u['current_streak'] or 0
            best = u['best_streak'] or 0
            if last != today:
                yesterday = (_now() - timedelta(days=1)).strftime('%Y-%m-%d')
                cur = cur + 1 if last == yesterday else 1
                best = max(best, cur)
                conn.execute(
                    'UPDATE users SET current_streak=?, best_streak=?, last_active_date=? WHERE user_id=?',
                    (cur, best, today, user_id)
                )

    def get_stats_today(self, user_id):
        today = _today()
        with self._conn() as conn:
            r = conn.execute(
                'SELECT answered, correct FROM daily_stats WHERE user_id=? AND date=?',
                (user_id, today)
            ).fetchone()
            if not r or r['answered'] == 0:
                return {'answered': 0, 'correct': 0, 'pct': 0}
            return {'answered': r['answered'], 'correct': r['correct'],
                    'pct': round(r['correct'] / r['answered'] * 100)}

    def get_stats_week(self, user_id):
        result = []
        with self._conn() as conn:
            for i in range(6, -1, -1):
                d = (_now() - timedelta(days=i)).strftime('%Y-%m-%d')
                r = conn.execute(
                    'SELECT answered, correct FROM daily_stats WHERE user_id=? AND date=?',
                    (user_id, d)
                ).fetchone()
                result.append({
                    'date': d,
                    'answered': r['answered'] if r else 0,
                    'correct': r['correct'] if r else 0
                })
        return result

    def categorize_words(self, user_id):
        words = self.get_all_active_words(user_id)
        learned = self.get_learned_words(user_id)
        strong, weak = [], []
        for w in words:
            total = w['total_answers']
            acc = round(w['correct_answers'] / total * 100) if total else 0
            w['accuracy'] = acc
            if total >= 3 and acc >= 70:
                strong.append(w)
            else:
                weak.append(w)
        strong.sort(key=lambda x: -x['accuracy'])
        weak.sort(key=lambda x: x['accuracy'])
        return strong, weak, learned

    def get_learned_words(self, user_id):
        with self._conn() as conn:
            rows = conn.execute(
                'SELECT * FROM words WHERE user_id=? AND learned=1 ORDER BY id', (user_id,)
            ).fetchall()
            return [self._row_to_word(r) for r in rows]

    def get_weak_words_active_batch(self, user_id, limit=3):
        words = self.get_active_batch_words(user_id)
        scored = []
        for w in words:
            total = w['total_answers']
            acc = (w['correct_answers'] / total) if total else 0
            scored.append((acc, total, w))
        scored.sort(key=lambda x: (x[0], -x[1]))
        return [w for acc, total, w in scored if total == 0 or acc < 0.7][:limit]

    # ---------------- PAUSE / COOLDOWN ----------------
    def set_pause(self, user_id, days):
        until = (_now() + timedelta(days=days)).isoformat()
        with self._conn() as conn:
            conn.execute('UPDATE users SET paused_until=? WHERE user_id=?', (until, user_id))

    def clear_pause(self, user_id):
        with self._conn() as conn:
            conn.execute('UPDATE users SET paused_until=NULL WHERE user_id=?', (user_id,))

    def is_paused(self, user_id):
        u = self.get_user(user_id)
        if not u or not u['paused_until']:
            return False
        try:
            return datetime.fromisoformat(u['paused_until']) > _now()
        except Exception:
            return False

    def set_official_cooldown(self, user_id, minutes):
        until = (_now() + timedelta(minutes=minutes)).isoformat()
        with self._conn() as conn:
            conn.execute('UPDATE users SET official_cooldown=? WHERE user_id=?', (until, user_id))

    def official_cooldown_remaining(self, user_id):
        u = self.get_user(user_id)
        if not u or not u['official_cooldown']:
            return 0
        try:
            until = datetime.fromisoformat(u['official_cooldown'])
            return max(0, int((until - _now()).total_seconds()))
        except Exception:
            return 0

    # ---------------- QUIZ SESSION ----------------
    def start_quiz(self, user_id, kind, word_ids):
        with self._conn() as conn:
            conn.execute('''
                INSERT OR REPLACE INTO quiz_session
                (user_id, kind, remaining, total, current_word_id, current_q, results)
                VALUES (?,?,?,?,?,?,?)
            ''', (user_id, kind, json.dumps(word_ids), len(word_ids), None, None, json.dumps({})))

    def get_quiz(self, user_id):
        with self._conn() as conn:
            r = conn.execute('SELECT * FROM quiz_session WHERE user_id=?', (user_id,)).fetchone()
            if not r:
                return None
            return {
                'kind': r['kind'],
                'remaining': json.loads(r['remaining']) if r['remaining'] else [],
                'total': r['total'],
                'current_word_id': r['current_word_id'],
                'current_q': json.loads(r['current_q']) if r['current_q'] else None,
                'results': json.loads(r['results']) if r['results'] else {},
            }

    def quiz_next(self, user_id):
        q = self.get_quiz(user_id)
        if not q or not q['remaining']:
            return None
        nxt = q['remaining'][0]
        rest = q['remaining'][1:]
        with self._conn() as conn:
            conn.execute(
                'UPDATE quiz_session SET remaining=?, current_word_id=?, current_q=NULL WHERE user_id=?',
                (json.dumps(rest), nxt, user_id)
            )
        return nxt

    def set_current_question(self, user_id, payload):
        with self._conn() as conn:
            conn.execute('UPDATE quiz_session SET current_q=? WHERE user_id=?',
                         (json.dumps(payload, ensure_ascii=False), user_id))

    def quiz_record(self, user_id, correct):
        q = self.get_quiz(user_id)
        if not q or q['current_word_id'] is None:
            return
        results = q['results']
        results[str(q['current_word_id'])] = bool(correct)
        with self._conn() as conn:
            conn.execute('UPDATE quiz_session SET results=? WHERE user_id=?',
                         (json.dumps(results), user_id))

    def quiz_all_correct(self, user_id):
        q = self.get_quiz(user_id)
        if not q:
            return False
        return len(q['results']) == q['total'] and all(q['results'].values())

    def clear_quiz(self, user_id):
        with self._conn() as conn:
            conn.execute('DELETE FROM quiz_session WHERE user_id=?', (user_id,))
