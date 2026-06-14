import os
import logging
import asyncio
import random
from datetime import time, datetime

import pytz
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

from database import (
    Database, _now, CEFR_LEVELS, CEFR_TARGET, CEFR_ADVANCE_AT, DAILY_HINTS
)
from claude_api import (
    get_word_info, generate_cefr_word, FALLBACK_DISTRACTORS,
    _validate_distractors, _fill_replacements, regenerate_distractors,
    get_word_info_fixed
)

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

TZ = pytz.timezone('Asia/Ho_Chi_Minh')
db = Database()

DIV = '━━━━━━━━━━━━━━━━'
LETTERS = 'ABCDEFGH'

# Рівні складності: розмір партії, к-сть кнопок, режим тесту
LEVELS = {
    1: {'name': 'Легкий',   'emoji': '🟢', 'batch': 4,  'buttons': 3, 'mode': 'choice', 'desc': '4 слова • вибір з 3'},
    2: {'name': 'Середній', 'emoji': '🔵', 'batch': 7,  'buttons': 5, 'mode': 'choice', 'desc': '7 слів • вибір з 5'},
    3: {'name': 'Складний', 'emoji': '🟠', 'batch': 10, 'buttons': 8, 'mode': 'choice', 'desc': '10 слів • вибір з 8'},
    4: {'name': 'Експерт',  'emoji': '🔴', 'batch': 15, 'buttons': 0, 'mode': 'type',   'desc': '15 слів • писати вручну'},
}

TIMEZONES = {
    'Asia/Ho_Chi_Minh': '🇻🇳 Хошимін (UTC+7)',
    'Europe/Kiev': '🇺🇦 Київ (UTC+2/+3)',
}

CEFR_DESC = {
    'A1': 'Початковий',
    'A2': 'Базовий',
    'B1': 'Середній',
    'B2': 'Вище середнього',
    'C1': 'Просунутий',
    'C2': 'Досконалий',
}
LEVELUP_TEST_SIZE = 50
LEVELUP_MAX_MISTAKES = 3

REMINDERS = [
    (9, 0, 'morning'),
    (10, 30, 'mini'),
    (12, 0, 'training'),
    (14, 30, 'gap'),
    (18, 0, 'training'),
    (21, 0, 'official'),
]


# ============ LEVENSHTEIN (для режиму «Експерт») ============
def levenshtein(a, b):
    a, b = a or '', b or ''
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[lb]


def _norm(s):
    return (s or '').strip().lower()


def type_is_correct(answer, target):
    """1 невірна літера в слові допускається (Левенштейн <= 1)."""
    return levenshtein(_norm(answer), _norm(target)) <= 1


def translation_variants(translation):
    """Розбиває переклад на варіанти (кома/слеш/крапка з комою)."""
    raw = (translation or '').replace('/', ',').replace(';', ',')
    return [v.strip() for v in raw.split(',') if v.strip()]


def _next_cefr(cefr):
    """Наступний рівень CEFR або None якщо вже C2."""
    try:
        i = CEFR_LEVELS.index(cefr)
        return CEFR_LEVELS[i + 1] if i + 1 < len(CEFR_LEVELS) else None
    except (ValueError, IndexError):
        return None


# ============ KEYBOARDS ============
def persistent_kb():
    return ReplyKeyboardMarkup(
        [['📚 Меню', '🎯 Тест']],
        resize_keyboard=True, is_persistent=True
    )


def main_menu_kb(user_id=None):
    rows = [
        [InlineKeyboardButton('📚 Мої слова', callback_data='m:words:0'),
         InlineKeyboardButton('➕ Додати', callback_data='m:add')],
        [InlineKeyboardButton('📋 Слова на сьогодні', callback_data='m:list')],
        [InlineKeyboardButton('📊 Статистика', callback_data='m:stats:today'),
         InlineKeyboardButton('🎯 Тест', callback_data='m:test')],
    ]
    rows.append([InlineKeyboardButton('⚙️ Налаштування', callback_data='m:settings'),
                 InlineKeyboardButton('ℹ️ Інструкція', callback_data='m:help')])
    return InlineKeyboardMarkup(rows)


def back_kb(target='m:home'):
    return InlineKeyboardMarkup([[InlineKeyboardButton('◀️ Назад', callback_data=target)]])


def test_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('🎯 Тренування', callback_data='test:training')],
        [InlineKeyboardButton('🏆 Офіційний тест', callback_data='test:official')],
        [InlineKeyboardButton('◀️ Меню', callback_data='m:home')],
    ])


# ============ ONBOARDING ============
# Крок 1 — джерело слів
def onboarding_source_text():
    return (
        '👋 <b>З чого почнемо?</b>\n' + DIV + '\n'
        'Обери звідки брати слова для вивчення:\n\n'
        '🎓 <b>За рівнем CEFR</b> — бот сам підбирає реальні вживані '
        'слова твого рівня (A1–C2)\n\n'
        '✍️ <b>Свої слова</b> — додаєш власні слова вручну'
    )


def onboarding_source_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('🎓 За рівнем CEFR', callback_data='src:cefr')],
        [InlineKeyboardButton('✍️ Свої слова', callback_data='src:own')],
        [InlineKeyboardButton('ℹ️ Як це працює', callback_data='m:help')],
    ])


# Крок 1.5 — вибір CEFR рівня
def onboarding_cefr_text():
    return (
        '🎓 <b>Який твій рівень англійської?</b>\n' + DIV + '\n'
        'Бот підбиратиме слова саме цього рівня:\n\n'
        '🟢 <b>A1</b> — Початковий\n'
        '🟢 <b>A2</b> — Базовий\n'
        '🔵 <b>B1</b> — Середній\n'
        '🔵 <b>B2</b> — Вище середнього\n'
        '🟠 <b>C1</b> — Просунутий\n'
        '🔴 <b>C2</b> — Досконалий\n\n'
        'Не знаєш свій рівень? Почни з <b>A2</b> або <b>B1</b>.'
    )


def onboarding_cefr_kb():
    emoji = {'A1': '🟢', 'A2': '🟢', 'B1': '🔵', 'B2': '🔵', 'C1': '🟠', 'C2': '🔴'}
    rows = []
    pair = []
    for lvl in CEFR_LEVELS:
        pair.append(InlineKeyboardButton(f'{emoji[lvl]} {lvl} — {CEFR_DESC[lvl]}',
                                         callback_data=f'cefr:{lvl}'))
        if len(pair) == 1:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    return InlineKeyboardMarkup(rows)


# Крок 2 — рівень складності тесту
def onboarding_text():
    return (
        '🎚 <b>Обери рівень складності тесту</b>\n' + DIV + '\n'
        'Від нього залежить розмір партії та тип тесту:\n\n'
        '🟢 <b>Легкий</b> — 4 слова, вибір з 3 кнопок\n'
        '🔵 <b>Середній</b> — 7 слів, вибір з 5 кнопок\n'
        '🟠 <b>Складний</b> — 10 слів, вибір з 8 кнопок\n'
        '🔴 <b>Експерт</b> — 15 слів, писати вручну\n\n'
        'Змінити можна будь-коли в ⚙️ Налаштуваннях.'
    )


def onboarding_kb():
    rows = [[InlineKeyboardButton(f"{c['emoji']} {c['name']} — {c['desc']}",
                                  callback_data=f'lvl:{n}')]
            for n, c in LEVELS.items()]
    return InlineKeyboardMarkup(rows)


async def needs_onboarding(user_id):
    """Онбординг не завершено, якщо не обрано джерело, рівень CEFR (для cefr) або складність."""
    src = db.get_word_source(user_id)
    if src is None:
        return True
    if src == 'cefr' and db.get_cefr_level(user_id) is None:
        return True
    if db.get_level(user_id) is None:
        return True
    return False


async def send_onboarding_step(message_or_chat, user_id, context=None, edit_query=None):
    """Показує поточний потрібний крок онбордингу."""
    src = db.get_word_source(user_id)
    if src is None:
        text, kb = onboarding_source_text(), onboarding_source_kb()
    elif src == 'cefr' and db.get_cefr_level(user_id) is None:
        text, kb = onboarding_cefr_text(), onboarding_cefr_kb()
    else:
        text, kb = onboarding_text(), onboarding_kb()
    if edit_query is not None:
        return await safe_edit(edit_query, text, kb)
    await message_or_chat.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


# ============ RENDER: HOME ============
def render_home(user_id):
    u = db.get_user(user_id)
    name = u['name'] if u else ''
    lvl = (u['level'] if u else None) or 3
    conf = LEVELS.get(lvl, LEVELS[3])
    hour = _now().hour
    if 5 <= hour < 12:
        greet = f'🌅 Доброго ранку, {name}!'
    elif 12 <= hour < 18:
        greet = f'☀️ Привіт, {name}!'
    else:
        greet = f'🌙 Добрий вечір, {name}!'

    active = db.get_active_batch_words(user_id)
    batch_no = db.get_active_batch_number(user_id)
    today = db.get_stats_today(user_id)
    streak = u['current_streak'] if u else 0
    best = u['best_streak'] if u else 0

    src = db.get_word_source(user_id)
    cefr = db.get_cefr_level(user_id)

    lines = [greet, '', DIV]
    lines.append(f'{conf["emoji"]} Складність: {conf["name"]} ({conf["desc"]})')

    if src == 'cefr' and cefr:
        mastered = db.cefr_mastered_count(user_id, cefr)
        lines.append(f'🎓 Рівень CEFR: {cefr} ({CEFR_DESC.get(cefr, "")})')
        lines.append(f'📈 Вивчено: {mastered}/{CEFR_TARGET} слів рівня {cefr}')

    if batch_no:
        lines.append(f'📚 Партія #{batch_no} · {len(active)} слів')
    elif src == 'cefr':
        lines.append('🎓 Натисни «Згенерувати нові слова» щоб почати')
    else:
        lines.append('📭 Поки немає слів — натисни ➕ Додати')

    if today['answered']:
        lines.append(f'✅ Сьогодні: {today["correct"]}/{today["answered"]} · {today["pct"]}%')
    else:
        lines.append('✅ Сьогодні: ще не займався')

    lines.append(f'🔥 Серія: {streak} дн.' + (f' (рекорд {best})' if best else ''))

    weak = db.get_weak_words_active_batch(user_id, limit=3)
    if weak:
        lines.append(DIV)
        lines.append('😅 Повтори:')
        lines.append(', '.join(w['word'].upper() for w in weak))

    kb_rows = list(main_menu_kb(user_id).inline_keyboard)
    # Пропозиція тесту переходу, якщо вивчено достатньо
    if src == 'cefr' and cefr:
        mastered = db.cefr_mastered_count(user_id, cefr)
        nxt = _next_cefr(cefr)
        if mastered >= CEFR_ADVANCE_AT and nxt:
            lines.append(DIV)
            lines.append(f'🎉 Ти вивчив {mastered} слів рівня {cefr}!')
            lines.append(f'Готовий перевірити себе для переходу на {nxt}?')
            kb_rows.insert(0, [InlineKeyboardButton(
                f'🎯 Тест переходу на {nxt}', callback_data='levelup:start')])

    return '\n'.join(lines), InlineKeyboardMarkup(kb_rows)


# ============ RENDER: WORD CARD ============
def render_word_card(user_id, index):
    words = db.get_all_active_words(user_id)
    if not words:
        return '📭 У тебе поки немає активних слів.\n\nНатисни ➕ Додати щоб почати.', back_kb()

    index = max(0, min(index, len(words) - 1))
    w = words[index]
    total = w['total_answers']
    acc = round(w['correct_answers'] / total * 100) if total else 0

    if total == 0:
        level = '🆕 Нове'
    elif acc >= 90:
        level = '💪 Чудово знаю'
    elif acc >= 70:
        level = '👍 Добре знаю'
    elif acc >= 40:
        level = '😐 Середньо'
    else:
        level = '😅 Слабко'

    stars = '⭐' * min(5, w['correct_answers']) if w['correct_answers'] else '▫️'

    text = (
        f'📚 {index + 1} з {len(words)}  ·  Партія #{w["batch_number"]}\n'
        f'{DIV}\n'
        f'<b>{w["word"].upper()}</b>\n'
        f'{w["translation"]} · 🔊 {w["transcription"]}\n\n'
        f'▸ <i>{w["example1"]}</i>\n'
        f'▸ <i>{w["example2"]}</i>\n'
        f'{DIV}\n'
        f'📊 {level}  {stars}\n'
        f'✅ Правильно: {w["correct_answers"]}/{total}\n'
    )
    if w['last_wrong']:
        text += f'⚠️ Остання помилка: «{w["last_wrong"]}»\n'

    nav = []
    if index > 0:
        nav.append(InlineKeyboardButton('◀️', callback_data=f'm:words:{index-1}'))
    nav.append(InlineKeyboardButton(f'{index+1}/{len(words)}', callback_data='noop'))
    if index < len(words) - 1:
        nav.append(InlineKeyboardButton('▶️', callback_data=f'm:words:{index+1}'))

    kb = InlineKeyboardMarkup([
        nav,
        [InlineKeyboardButton('✅ Вивчив', callback_data=f'w:learn:{w["id"]}:{index}'),
         InlineKeyboardButton('🗑 Видалити', callback_data=f'w:del:{w["id"]}:{index}')],
        [InlineKeyboardButton('◀️ Меню', callback_data='m:home')],
    ])
    return text, kb


# ============ RENDER: ACTIVE WORDS LIST ============
def render_active_list(user_id):
    words = db.get_active_batch_words(user_id)
    batch_no = db.get_active_batch_number(user_id)
    if not words:
        return '📭 Немає активних слів у поточній партії.', back_kb()

    lines = [f'📋 <b>Партія #{batch_no} · {len(words)} слів</b>', DIV]
    for i, w in enumerate(words, 1):
        lines.append(f'{i}. <b>{w["word"].upper()}</b> — {w["translation"]}\n'
                     f'   🔊 {w["transcription"]}')
    return '\n'.join(lines), back_kb()


# ============ RENDER: STATS ============
def stats_tabs_kb(active):
    def lbl(key, name):
        return ('· ' + name + ' ·') if key == active else name
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(lbl('today', '📅 Сьогодні'), callback_data='m:stats:today'),
         InlineKeyboardButton(lbl('week', '📈 Тиждень'), callback_data='m:stats:week'),
         InlineKeyboardButton(lbl('all', '🏆 Всі'), callback_data='m:stats:all')],
        [InlineKeyboardButton('◀️ Меню', callback_data='m:home')],
    ])


def render_stats_today(user_id):
    u = db.get_user(user_id)
    t = db.get_stats_today(user_id)
    text = ['📅 <b>Сьогодні</b>', DIV]
    if t['answered']:
        text.append(f'✅ Відповідей: {t["answered"]}')
        text.append(f'🎯 Правильних: {t["correct"]} ({t["pct"]}%)')
    else:
        text.append('Сьогодні ще не займався 💤')
    text.append(DIV)
    text.append(f'🔥 Серія: {u["current_streak"]} дн.')
    text.append(f'🏆 Рекорд: {u["best_streak"]} дн.')

    weak = db.get_weak_words_active_batch(user_id, limit=5)
    if weak:
        text.append(DIV)
        text.append('😅 Повтори ці слова:')
        for w in weak:
            total = w['total_answers']
            acc = round(w['correct_answers'] / total * 100) if total else 0
            mark = '⚠️' if total else '🆕'
            text.append(f'{mark} {w["word"].upper()} — {w["translation"]} ({acc}%)')
    return '\n'.join(text), stats_tabs_kb('today')


def render_stats_week(user_id):
    week = db.get_stats_week(user_id)
    text = ['📈 <b>Тиждень</b>', DIV]
    names = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Нд']
    maxv = max((d['answered'] for d in week), default=0) or 1
    total_ans = 0
    for d in week:
        weekday = names[_weekday(d['date'])]
        filled = round(d['answered'] / maxv * 10)
        bar = '█' * filled + '░' * (10 - filled)
        text.append(f'{weekday} {bar} {d["answered"]}')
        total_ans += d['answered']
    text.append(DIV)
    text.append(f'Всього за тиждень: {total_ans} відповідей')
    return '\n'.join(text), stats_tabs_kb('week')


def _weekday(date_str):
    from datetime import datetime as _dt
    return _dt.strptime(date_str, '%Y-%m-%d').weekday()


def render_stats_all(user_id):
    strong, weak, learned = db.categorize_words(user_id)
    text = ['🏆 <b>Всі слова</b>', DIV]
    text.append(f'💪 Добре знаю: {len(strong)}')
    for w in strong[:8]:
        text.append(f'  ✅ {w["word"].upper()} · {w["accuracy"]}%')
    text.append('')
    text.append(f'😅 Треба повторити: {len(weak)}')
    for w in weak[:8]:
        total = w['total_answers']
        acc = round(w['correct_answers'] / total * 100) if total else 0
        mark = '⚠️' if total else '🆕'
        text.append(f'  {mark} {w["word"].upper()} · {acc}%')
    text.append('')
    text.append(f'🏆 Вивчено (в архіві): {len(learned)}')

    batches = db.get_batches(user_id)
    if batches:
        text.append(DIV)
        text.append('📦 Партії:')
        for b in batches:
            icon = {'active': '🟢', 'completed': '✅', 'locked': '🔒'}.get(b['status'], '•')
            text.append(f'  {icon} Партія #{b["batch_number"]} — {b["total"]} слів')
    return '\n'.join(text), stats_tabs_kb('all')


# ============ RENDER: SETTINGS ============
def render_settings(user_id):
    u = db.get_user(user_id)
    lvl = (u['level'] if u else None) or 3
    conf = LEVELS.get(lvl, LEVELS[3])
    paused = db.is_paused(user_id)
    src = db.get_word_source(user_id)
    cefr = db.get_cefr_level(user_id)

    text = ['⚙️ <b>Налаштування</b>', DIV]
    if src == 'cefr' and cefr:
        mastered = db.cefr_mastered_count(user_id, cefr)
        text.append(f'🎓 Джерело: рівень CEFR <b>{cefr}</b> ({CEFR_DESC.get(cefr, "")})')
        text.append(f'📈 Вивчено: {mastered}/{CEFR_TARGET} слів рівня {cefr}')
    else:
        text.append('✍️ Джерело: <b>Свої слова</b>')
    text.append(f'{conf["emoji"]} Складність: <b>{conf["name"]}</b> ({conf["desc"]})')
    text.append(DIV)

    tz = db.get_timezone(user_id)
    tz_label = TIMEZONES.get(tz, tz)
    text.append(f'🕐 Час нагадувань: {tz_label}')
    text.append('9:00 · 10:30 · 12:00 · 14:30 · 18:00 · 21:00')
    text.append(DIV)
    text.append('⏸ Пауза: ' + ('так, на паузі' if paused else 'вимкнено'))

    rows = []
    if src == 'cefr':
        rows.append([InlineKeyboardButton('🎓 Обрати рівень A1-C2', callback_data='s:cefr')])
    rows.append([InlineKeyboardButton('🎚 Змінити складність', callback_data='s:level')])
    if src == 'cefr':
        rows.append([InlineKeyboardButton('✍️ Перейти на свої слова', callback_data='s:src:own')])
    else:
        rows.append([InlineKeyboardButton('🎓 Перейти на рівні CEFR', callback_data='s:src:cefr')])
    rows.append([InlineKeyboardButton('🕐 Змінити часовий пояс', callback_data='s:tz')])
    if paused:
        rows.append([InlineKeyboardButton('▶️ Зняти паузу', callback_data='s:unpause')])
    else:
        rows.append([
            InlineKeyboardButton('⏸ 1 день', callback_data='s:pause:1'),
            InlineKeyboardButton('⏸ 3 дні', callback_data='s:pause:3'),
            InlineKeyboardButton('⏸ 7 днів', callback_data='s:pause:7'),
        ])
    rows.append([InlineKeyboardButton('📤 Експорт слів', callback_data='s:export')])
    rows.append([InlineKeyboardButton('ℹ️ Як це працює', callback_data='m:help')])
    if src == 'cefr':
        rows.append([InlineKeyboardButton('🎓 Отримати слова', callback_data='cefr:gen')])
    rows.append([InlineKeyboardButton('◀️ Меню', callback_data='m:home')])
    return '\n'.join(text), InlineKeyboardMarkup(rows)


def render_level_picker(user_id):
    u = db.get_user(user_id)
    cur = (u['level'] if u else None) or 3
    text = ['🎚 <b>Рівень складності</b>', DIV,
            'Нові слова будуть формуватись у партії за обраним рівнем.', '']
    rows = []
    for n, c in LEVELS.items():
        mark = ' ✅' if n == cur else ''
        rows.append([InlineKeyboardButton(f'{c["emoji"]} {c["name"]} — {c["desc"]}{mark}',
                                          callback_data=f'setlvl:{n}')])
    rows.append([InlineKeyboardButton('◀️ Назад', callback_data='m:settings')])
    return '\n'.join(text), InlineKeyboardMarkup(rows)


def render_tz_picker(user_id):
    cur = db.get_timezone(user_id)
    text = ['🕐 <b>Часовий пояс нагадувань</b>', DIV, '']
    rows = []
    for tz, label in TIMEZONES.items():
        mark = ' ✅' if tz == cur else ''
        rows.append([InlineKeyboardButton(f'{label}{mark}', callback_data=f'settz:{tz}')])
    rows.append([InlineKeyboardButton('◀️ Назад', callback_data='m:settings')])
    return '\n'.join(text), InlineKeyboardMarkup(rows)


def render_cefr_setting_picker(user_id):
    cur = db.get_cefr_level(user_id)
    emoji = {'A1': '🟢', 'A2': '🟢', 'B1': '🔵', 'B2': '🔵', 'C1': '🟠', 'C2': '🔴'}
    text = ['🎓 <b>Рівень CEFR</b>', DIV,
            'Нові слова підбиратимуться за обраним рівнем.', '']
    rows = []
    for lvl in CEFR_LEVELS:
        mark = ' ✅' if lvl == cur else ''
        rows.append([InlineKeyboardButton(f'{emoji[lvl]} {lvl} — {CEFR_DESC[lvl]}{mark}',
                                          callback_data=f'cefr:{lvl}')])
    rows.append([InlineKeyboardButton('◀️ Назад', callback_data='m:settings')])
    return '\n'.join(text), InlineKeyboardMarkup(rows)


# ============ COMMANDS ============
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.add_user(user.id, user.first_name)

    if await needs_onboarding(user.id):
        await update.message.reply_text(
            f'👋 Привіт, {user.first_name}! Я допоможу вивчати англійські слова '
            f'методом інтервального повторення.',
            reply_markup=persistent_kb()
        )
        await send_onboarding_step(update.message, user.id, context)
        return

    await update.message.reply_text(
        f'👋 Привіт, {user.first_name}!', reply_markup=persistent_kb()
    )
    text, kb = render_home(user.id)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.add_user(update.effective_user.id, update.effective_user.first_name)
    if await needs_onboarding(update.effective_user.id):
        await send_onboarding_step(update.message, update.effective_user.id, context)
        return
    if context.args:
        await process_new_words(update.effective_user.id, ' '.join(context.args),
                                update.message, context)
    else:
        context.user_data['state'] = 'adding'
        await update.message.reply_text(
            '➕ Надішли слова англійською через кому:\n\n'
            '<code>struggle, effort, empty, uncertain</code>',
            parse_mode=ParseMode.HTML
        )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await needs_onboarding(update.effective_user.id):
        await send_onboarding_step(update.message, update.effective_user.id, context)
        return
    text, kb = render_home(update.effective_user.id)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


def help_text():
    return (
        '📖 <b>Як користуватись WordFlow</b>\n' + DIV + '\n\n'
        '<b>1. Звідки слова?</b>\n'
        '🎓 <b>За рівнем CEFR (A1-C2)</b> — бот сам підбирає реальні слова '
        'твого рівня та генерує партії.\n'
        '✍️ <b>Свої слова</b> — додаєш будь-які слова через ➕ Додати, '
        'бот робить переклад, транскрипцію та приклади.\n\n'
        '<b>2. Рівень складності</b>\n'
        '🟢 Легкий (4 слова, вибір з 3)\n'
        '🔵 Середній (7 слів, вибір з 5)\n'
        '🟠 Складний (10 слів, вибір з 8)\n'
        '🔴 Експерт (15 слів, писати вручну, є підказки)\n\n'
        '<b>3. Як вчити</b>\n'
        '📋 <b>Слова на сьогодні</b> — список активної партії з перекладом.\n'
        '🎯 <b>Тренування</b> — будь-коли, не впливає на прогрес.\n'
        '🏆 <b>Офіційний тест</b> — щодня 21:00–00:00. 100% правильно — '
        'відкриває нову партію!\n\n'
        '<b>4. Нагадування (за твоїм часовим поясом)</b>\n'
        '⏰ 9:00 — слова на день\n'
        '⏰ 10:30 — міні-тест\n'
        '⏰ 12:00 — адаптивне тренування\n'
        '⏰ 14:30 — речення з пропуском\n'
        '⏰ 18:00 — адаптивне тренування\n'
        '⏰ 21:00 — офіційний тест\n\n'
        '<b>5. CEFR-прогрес</b>\n'
        'Вивчиш 80/100 слів рівня — бот запропонує тест переходу '
        'на наступний рівень.\n\n'
        '⚙️ Все можна змінити в Налаштуваннях: складність, рівень CEFR, '
        'джерело слів, часовий пояс, пауза.\n\n'
        'Команди: /menu — головне меню, /add — додати слова, /help — ця довідка.'
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(help_text(), parse_mode=ParseMode.HTML,
                                     reply_markup=back_kb())


# ============ ADMIN: ONE-TIME DISTRACTOR REVALIDATION ============
async def cmd_fixdistractors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Одноразова перевалідація дистракторів обох напрямків (видали команду після використання).

    Для кожного слова перевіряє обидві сторони (uk і en). Якщо знаходить дистрактори,
    близькі за значенням/темою до правильної відповіді — РЕГЕНЕРУЄ весь набір з інших тем.
    """
    msg = await update.message.reply_text('⏳ Перевіряю дистрактори (обидва напрямки)...')
    all_words = db.get_all_words_full()
    checked = 0
    fixed = 0
    for w in all_words:
        d_en = list(w.get('distractors_en') or [])
        d_uk = list(w.get('distractors_uk') or [])
        if not d_en or not d_uk or len(d_en) != len(d_uk):
            continue

        distractors = [{'en': en, 'uk': uk} for en, uk in zip(d_en, d_uk)]
        word = w['word'].strip().lower()
        translation = w['translation']

        # 1) UK-сторона: чи uk-варіант близький до перекладу слова (питання en2uk)
        uk_pairs = [{'en': d['uk'], 'uk': d['uk']} for d in distractors]
        bad_uk = await _validate_distractors(word, translation, uk_pairs)
        await asyncio.sleep(0.4)

        # 2) EN-сторона: чи en-варіант близький до значення слова (питання ua2en)
        bad_en = await _validate_distractors(translation, word, distractors)
        await asyncio.sleep(0.4)
        checked += 1

        if any(a or b for a, b in zip(bad_uk, bad_en)):
            fresh = await regenerate_distractors(w['word'], translation)
            await asyncio.sleep(0.4)
            if fresh:
                db.update_word_distractors(
                    w['id'], fresh['distractors_en'], fresh['distractors_uk'])
                fixed += 1

    await msg.edit_text(
        f'✅ Готово!\n'
        f'Слів перевірено: {checked}\n'
        f'Регенеровано наборів: {fixed} з {len(all_words)}'
    )


async def cmd_setword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручне перевизначення значення слова (для багатозначних слів).
    Формат:  /setword waste = марнувати, витрачати
    Бот згенерує під це значення нову транскрипцію, приклади та дистрактори,
    оновить слово і скине його статистику.
    """
    user_id = update.effective_user.id
    raw = update.message.text or ''
    # прибираємо саму команду
    body = raw.split(None, 1)[1] if len(raw.split(None, 1)) > 1 else ''
    if '=' not in body:
        await update.message.reply_text(
            'Формат:\n<code>/setword waste = марнувати, витрачати</code>',
            parse_mode=ParseMode.HTML)
        return

    word_part, translation = body.split('=', 1)
    word = word_part.strip().lower()
    translation = translation.strip()
    if not word or not translation:
        await update.message.reply_text(
            'Формат:\n<code>/setword waste = марнувати, витрачати</code>',
            parse_mode=ParseMode.HTML)
        return

    status = await update.message.reply_text(
        f'⏳ Перегенеровую «{word}» зі значенням «{translation}»...')
    wd = await get_word_info_fixed(word, translation)
    rows = db.update_word_content(user_id, word, wd)

    if rows == 0:
        await status.edit_text(
            f'😕 Слова «{word}» немає у твоєму списку. '
            f'Спочатку додай його через ➕ Додати.')
        return

    lines = [f'✅ Оновлено «{word.upper()}» ({rows} зап.):', DIV,
             f'<b>{word.upper()}</b> — {wd["translation"]} · 🔊 {wd["transcription"]}',
             f'▸ <i>{wd["example1"]}</i>',
             f'▸ <i>{wd["example2"]}</i>', '',
             'Статистику цього слова скинуто (вчиш нове значення з нуля).']
    await status.edit_text('\n'.join(lines), parse_mode=ParseMode.HTML)


# ============ ADD WORDS ============
async def process_new_words(user_id, raw_text, message, context):
    parts = []
    for chunk in raw_text.replace('\n', ',').replace(';', ',').split(','):
        c = chunk.strip().lower()
        if c:
            parts.append(c)
    parts = [p for p in dict.fromkeys(parts)]

    if not parts:
        await message.reply_text('Не знайшов слів. Спробуй: struggle, effort, empty')
        return

    status = await message.reply_text(f'⏳ Обробляю {len(parts)} слів через AI...')

    words_data = []
    for word in parts:
        if db.word_exists(user_id, word):
            continue
        info = await get_word_info(word)
        words_data.append(info)

    if not words_data:
        await status.edit_text('Усі ці слова вже є у твоєму списку 👍')
        return

    added = db.add_words(user_id, words_data)
    bsize = db.batch_size_for(user_id)

    lines = [f'✅ Додано {added} слів:', DIV]
    for wd in words_data:
        lines.append(f'• <b>{wd["word"].upper()}</b> {wd["translation"]} — 🔊 {wd["transcription"]}')
    lines.append('')
    lines.append(f'Слова розбито на партії по {bsize}. Перша партія активна! 🚀')

    await status.edit_text('\n'.join(lines), parse_mode=ParseMode.HTML,
                           reply_markup=main_menu_kb(user_id))


# ============ CEFR GENERATION ============
async def generate_cefr_batch(user_id, context, chat_id):
    """Генерує партію слів поточного CEFR-рівня користувача."""
    cefr = db.get_cefr_level(user_id)
    if not cefr:
        await context.bot.send_message(chat_id, 'Спочатку обери рівень CEFR в ⚙️ Налаштуваннях.')
        return False

    size = db.batch_size_for(user_id)
    status = await context.bot.send_message(
        chat_id, f'🎓 Генерую {size} слів рівня {cefr}... Це займе трохи часу ⏳')

    exclude = [w.lower() for w in db.cefr_recent_words(user_id, cefr, 40)]
    seen = set(exclude)
    words_data = []
    attempts = 0
    max_attempts = size * 4

    while len(words_data) < size and attempts < max_attempts:
        attempts += 1
        try:
            info = await generate_cefr_word(cefr, list(seen))
        except Exception as e:
            logger.error(f'generate_cefr_word error: {e}')
            info = None
        if not info:
            continue
        w = info['word'].lower()
        if w in seen or db.word_exists(user_id, w):
            continue
        seen.add(w)
        info['cefr_level'] = cefr
        words_data.append(info)

    if not words_data:
        logger.error(f'generate_cefr_batch: 0 words after {attempts} attempts for cefr={cefr}')
        await status.edit_text(
            f'😕 Не вдалось згенерувати слова ({attempts} спроб). '
            f'Перевір CLAUDE_API_KEY або спробуй ще раз через хвилину.',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                '🔄 Спробувати ще раз', callback_data='cefr:gen')]])
        )
        return False

    db.add_words(user_id, words_data, batch_size=size)

    lines = [f'🎓 <b>Нова партія рівня {cefr}</b>', DIV]
    for wd in words_data:
        lines.append(f'• <b>{wd["word"].upper()}</b> {wd["translation"]} — 🔊 {wd["transcription"]}')
    lines.append('')
    lines.append('Партія активна! Починай тренуватись 🚀')
    await status.edit_text('\n'.join(lines), parse_mode=ParseMode.HTML,
                           reply_markup=main_menu_kb(user_id))
    return True


# ============ QUIZ ENGINE ============
def _make_gap(sentence, word):
    """Замінює слово (або його форму) на ___ в реченні."""
    import re
    pattern = re.compile(re.escape(word), re.IGNORECASE)
    result = pattern.sub('___', sentence, count=1)
    if result == sentence:
        result = sentence + ' (___)'
    return result


def build_question(user_id, w, level, gap=False, forced_direction=None):
    conf = LEVELS[level]
    payload = {'word_id': w['id']}

    if gap:
        # Gap-режим: вгадуємо слово за реченням з пропуском,
        # без транскрипції, кнопки за рівнем складності
        sentence = w.get('example1') or w.get('example2') or w['word']
        gapped = _make_gap(sentence, w['word'])
        payload['sentence'] = gapped

        if conf['mode'] == 'type':
            payload['mode'] = 'gap_type'
            return payload

        nbtn = conf['buttons']
        correct = w['word']
        pool = list(w.get('distractors_en', []) or [])
        fallback = [d['en'] for d in FALLBACK_DISTRACTORS]

        seen = {_norm(correct)}
        uniq = []
        for d in pool:
            if _norm(d) and _norm(d) not in seen:
                seen.add(_norm(d))
                uniq.append(d)
        random.shuffle(uniq)

        need = nbtn - 1
        chosen = uniq[:need]
        if len(chosen) < need:
            fb = [d for d in fallback if _norm(d) not in seen]
            random.shuffle(fb)
            for d in fb:
                chosen.append(d)
                seen.add(_norm(d))
                if len(chosen) >= need:
                    break

        options = chosen + [correct]
        random.shuffle(options)
        correct_index = options.index(correct)
        payload.update({'mode': 'gap_choice', 'options': options,
                        'correct_index': correct_index})
        return payload

    direction = forced_direction or random.choice(['ua2en', 'en2uk'])
    payload['direction'] = direction
    payload['mode'] = conf['mode']

    if conf['mode'] == 'type':
        return payload

    nbtn = conf['buttons']
    if direction == 'ua2en':
        correct = w['word']
        pool = list(w.get('distractors_en', []) or [])
        fallback = [d['en'] for d in FALLBACK_DISTRACTORS]
        prompt = w['translation']
        flag = '🇺🇦'
    else:
        correct = w['translation']
        pool = list(w.get('distractors_uk', []) or [])
        fallback = [d['uk'] for d in FALLBACK_DISTRACTORS]
        prompt = w['word']
        flag = '🇬🇧'

    seen = {_norm(correct)}
    uniq = []
    for d in pool:
        if _norm(d) and _norm(d) not in seen:
            seen.add(_norm(d))
            uniq.append(d)
    random.shuffle(uniq)

    need = nbtn - 1
    chosen = uniq[:need]
    if len(chosen) < need:
        fb = [d for d in fallback if _norm(d) not in seen]
        random.shuffle(fb)
        for d in fb:
            chosen.append(d)
            seen.add(_norm(d))
            if len(chosen) >= need:
                break

    options = chosen + [correct]
    random.shuffle(options)
    correct_index = options.index(correct)
    payload.update({'options': options, 'correct_index': correct_index,
                    'prompt': prompt, 'flag': flag})
    return payload


async def start_quiz(user_id, kind, context, chat_id):
    words = db.get_active_batch_words(user_id)
    if not words:
        await context.bot.send_message(chat_id, '📭 Немає активних слів для тесту.')
        return False

    lvl = db.get_level(user_id) or 3
    conf = LEVELS[lvl]

    if kind == 'official':
        # Екзамен: кожне слово ДВІЧІ — раз ua2en, раз en2uk, перемішано (рівно навпіл)
        tasks = []
        for w in words:
            tasks.append([w['id'], 'ua2en'])
            tasks.append([w['id'], 'en2uk'])
        random.shuffle(tasks)
        db.start_quiz(user_id, kind, tasks)
        total = len(tasks)
        await context.bot.send_message(
            chat_id,
            f'🏆 <b>ОФІЦІЙНИЙ ТЕСТ</b>\n{DIV}\n'
            f'{total} питань ({len(words)} слів × 2: 🇬🇧→🇺🇦 та 🇺🇦→🇬🇧) · {conf["emoji"]} {conf["name"]}\n'
            f'Дай усе правильно — і відкриється наступна партія!\n'
            f'Помилка = доведеться повторити.',
            parse_mode=ParseMode.HTML
        )
    else:
        ids = [w['id'] for w in words]
        random.shuffle(ids)
        db.start_quiz(user_id, kind, ids)
        mode_hint = ('обирай правильний варіант кнопкою'
                     if conf['mode'] == 'choice' else 'пиши відповідь вручну')
        await context.bot.send_message(
            chat_id,
            f'🎯 <b>Тренування</b>\n{DIV}\n{len(ids)} слів · {mode_hint} 💪',
            parse_mode=ParseMode.HTML
        )
    await present_question(user_id, context, chat_id)
    return True


async def start_levelup_test(user_id, context, chat_id):
    """Тест переходу на наступний CEFR-рівень (до 50 слів, допуск 3 помилки)."""
    cefr = db.get_cefr_level(user_id)
    nxt = _next_cefr(cefr) if cefr else None
    if not cefr or not nxt:
        await context.bot.send_message(chat_id, 'Тест переходу зараз недоступний.')
        return False
    words = db.cefr_words_for_test(user_id, cefr, LEVELUP_TEST_SIZE)
    if len(words) < 10:
        await context.bot.send_message(
            chat_id, f'Замало слів рівня {cefr} для тесту переходу. Повчись ще трохи 🎓')
        return False
    ids = [w['id'] for w in words]
    random.shuffle(ids)
    db.start_quiz(user_id, 'levelup', ids)
    await context.bot.send_message(
        chat_id,
        f'🎯 <b>ТЕСТ ПЕРЕХОДУ {cefr} → {nxt}</b>\n{DIV}\n'
        f'{len(ids)} слів · допускається до {LEVELUP_MAX_MISTAKES} помилок\n'
        f'Готовий? Поїхали! 💪',
        parse_mode=ParseMode.HTML
    )
    await present_question(user_id, context, chat_id)
    return True


def _render_choice_question(payload, done, total):
    """Текст і клавіатура для choice/gap_choice питання."""
    rows = [[InlineKeyboardButton(opt, callback_data=f'qa:{i}')]
            for i, opt in enumerate(payload['options'])]
    if payload['mode'] == 'gap_choice':
        text = (f'❓ <b>{done}/{total}</b>\n{DIV}\n'
                f'🇬🇧 <i>{payload["sentence"]}</i>\n\n'
                f'Обери слово, що пропущене 👇')
    else:
        text = (f'❓ <b>{done}/{total}</b>\n{DIV}\n'
                f'{payload["flag"]} <b>{payload["prompt"].upper()}</b>\n\n'
                f'Обери правильний варіант 👇')
    return text, InlineKeyboardMarkup(rows)


async def present_question(user_id, context, chat_id):
    nxt = db.quiz_next(user_id)
    if nxt is None:
        await finish_quiz(user_id, context, chat_id)
        return
    wid, forced_dir = nxt
    w = db.get_word_by_id(user_id, wid)
    if not w:
        await present_question(user_id, context, chat_id)
        return

    q = db.get_quiz(user_id)
    lvl = db.get_level(user_id) or 3
    gap = (q is not None and q['kind'] == 'gap')
    payload = build_question(user_id, w, lvl, gap=gap, forced_direction=forced_dir)
    db.set_current_question(user_id, payload)

    q = db.get_quiz(user_id)
    done = q['total'] - len(q['remaining'])
    total = q['total']

    if payload['mode'] in ('choice', 'gap_choice'):
        text, kb = _render_choice_question(payload, done, total)
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                       reply_markup=kb)
    elif payload['mode'] == 'gap_type':
        await context.bot.send_message(
            chat_id,
            f'✍️ <b>{done}/{total}</b>\n{DIV}\n'
            f'🇬🇧 <i>{payload["sentence"]}</i>\n\n'
            f'Напиши пропущене слово англійською 👇',
            parse_mode=ParseMode.HTML
        )
    else:
        direction = payload['direction']
        if direction == 'ua2en':
            flag, prompt, instr = '🇺🇦', w['translation'], 'Напиши англійською 👇'
        else:
            flag, prompt, instr = '🇬🇧', w['word'], 'Напиши переклад українською 👇'
        kb = None
        hints_left = db.get_hints_left(user_id)
        if hints_left > 0:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                f'💡 Підказка ({hints_left}/{DAILY_HINTS})', callback_data='hint')]])
        await context.bot.send_message(
            chat_id,
            f'✍️ <b>{done}/{total}</b>\n{DIV}\n'
            f'{flag} <b>{prompt.upper()}</b>\n\n{instr}',
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )


def _choice_feedback(payload, w, correct, chosen_text):
    """Короткий рядок-фідбек для choice/gap_choice (1 повідомлення, редагується на місці)."""
    if payload.get('mode') == 'gap_choice':
        if correct:
            return (f'✅ Правильно!  <b>{w["word"].upper()}</b> '
                    f'{w["translation"]} · 🔊 {w["transcription"]}')
        return (f'❌ Не вгадав. Ти обрав: <s>{chosen_text}</s>\n'
                f'✅ <b>{w["word"].upper()}</b> — {w["translation"]} · 🔊 {w["transcription"]}')
    # звичайний choice
    if correct:
        return (f'✅ Правильно!  <b>{w["word"].upper()}</b> '
                f'{w["translation"]} · 🔊 {w["transcription"]}')
    return (f'❌ Не вгадав. Ти обрав: <s>{chosen_text}</s>\n'
            f'✅ <b>{w["word"].upper()}</b> — {w["translation"]} · 🔊 {w["transcription"]}')


async def handle_choice_answer(user_id, selected, context, chat_id, query):
    q = db.get_quiz(user_id)
    if not q or not q['current_q'] or q['current_word_id'] is None:
        return
    payload = q['current_q']
    if payload.get('mode') not in ('choice', 'gap_choice'):
        return
    w = db.get_word_by_id(user_id, q['current_word_id'])
    if not w:
        return

    options = payload['options']
    correct_index = payload['correct_index']
    if selected < 0 or selected >= len(options):
        return
    correct = (selected == correct_index)
    chosen_text = options[selected]
    db.record_answer(user_id, w['id'], correct,
                     user_typed=None if correct else chosen_text)
    db.quiz_record(user_id, correct)

    feedback = _choice_feedback(payload, w, correct, chosen_text)

    # Беремо наступне питання
    nxt = db.quiz_next(user_id)
    if nxt is None:
        # Останнє — показуємо фідбек у тому ж повідомленні, тоді підсумок
        try:
            await query.edit_message_text(feedback, parse_mode=ParseMode.HTML)
        except Exception:
            await context.bot.send_message(chat_id, feedback, parse_mode=ParseMode.HTML)
        await finish_quiz(user_id, context, chat_id)
        return

    wid, forced_dir = nxt
    w2 = db.get_word_by_id(user_id, wid)
    if not w2:
        # слово зникло — рекурсивно далі (нове повідомлення)
        try:
            await query.edit_message_text(feedback, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await present_question(user_id, context, chat_id)
        return

    lvl = db.get_level(user_id) or 3
    gap = (q['kind'] == 'gap')
    payload2 = build_question(user_id, w2, lvl, gap=gap, forced_direction=forced_dir)
    db.set_current_question(user_id, payload2)

    q2 = db.get_quiz(user_id)
    done = q2['total'] - len(q2['remaining'])
    total = q2['total']

    if payload2['mode'] in ('choice', 'gap_choice'):
        # ЕДИН-В-ОДНОМУ: фідбек + наступне питання в тому ж повідомленні (без гортання)
        qtext, kb = _render_choice_question(payload2, done, total)
        combined = f'{feedback}\n{DIV}\n{qtext}'
        try:
            await query.edit_message_text(combined, parse_mode=ParseMode.HTML,
                                          reply_markup=kb)
        except Exception:
            await context.bot.send_message(chat_id, combined, parse_mode=ParseMode.HTML,
                                           reply_markup=kb)
    else:
        # наступне питання — письмове (Експерт): фідбек окремо, тоді питання
        try:
            await query.edit_message_text(feedback, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        if payload2['mode'] == 'gap_type':
            await context.bot.send_message(
                chat_id,
                f'✍️ <b>{done}/{total}</b>\n{DIV}\n'
                f'🇬🇧 <i>{payload2["sentence"]}</i>\n\n'
                f'Напиши пропущене слово англійською 👇',
                parse_mode=ParseMode.HTML
            )
        else:
            direction = payload2['direction']
            if direction == 'ua2en':
                flag, prompt, instr = '🇺🇦', w2['translation'], 'Напиши англійською 👇'
            else:
                flag, prompt, instr = '🇬🇧', w2['word'], 'Напиши переклад українською 👇'
            kb = None
            hints_left = db.get_hints_left(user_id)
            if hints_left > 0:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                    f'💡 Підказка ({hints_left}/{DAILY_HINTS})', callback_data='hint')]])
            await context.bot.send_message(
                chat_id,
                f'✍️ <b>{done}/{total}</b>\n{DIV}\n'
                f'{flag} <b>{prompt.upper()}</b>\n\n{instr}',
                parse_mode=ParseMode.HTML,
                reply_markup=kb
            )


async def handle_hint(user_id, context, chat_id, query):
    """Показує перші 2 літери відповіді (режим Експерт), списує одну підказку."""
    q = db.get_quiz(user_id)
    if not q or not q['current_q'] or q['current_word_id'] is None:
        await query.answer('Підказка зараз недоступна')
        return
    payload = q['current_q']
    if payload.get('mode') != 'type':
        await query.answer('Підказка лише для письмових питань')
        return
    if db.get_hints_left(user_id) <= 0:
        await query.answer('Підказки на сьогодні закінчились 😔', show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    w = db.get_word_by_id(user_id, q['current_word_id'])
    if not w:
        return
    direction = payload['direction']
    target = w['word'] if direction == 'ua2en' else (translation_variants(w['translation']) or [w['translation']])[0]
    target = (target or '').strip()
    reveal = target[:2].upper() if len(target) >= 2 else target.upper()

    db.use_hint(user_id)
    left = db.get_hints_left(user_id)

    await context.bot.send_message(
        chat_id, f'💡 Починається на: <b>{reveal}...</b>', parse_mode=ParseMode.HTML)

    # оновити кнопку лічильника або прибрати
    try:
        if left > 0:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                    f'💡 Підказка ({left}/{DAILY_HINTS})', callback_data='hint')]]))
        else:
            await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


async def handle_type_answer(user_id, text, context, chat_id):
    q = db.get_quiz(user_id)
    if not q or not q['current_q'] or q['current_word_id'] is None:
        return False
    payload = q['current_q']
    if payload.get('mode') not in ('type', 'gap_type'):
        return False
    w = db.get_word_by_id(user_id, q['current_word_id'])
    if not w:
        return False

    answer = text.strip()

    if payload.get('mode') == 'gap_type':
        correct = type_is_correct(answer, w['word'])
        db.record_answer(user_id, w['id'], correct,
                         user_typed=None if correct else answer)
        db.quiz_record(user_id, correct)
        if correct:
            await context.bot.send_message(
                chat_id,
                f'✅ Правильно!\n'
                f'<b>{w["word"].upper()}</b> {w["translation"]} · 🔊 {w["transcription"]}',
                parse_mode=ParseMode.HTML
            )
        else:
            await context.bot.send_message(
                chat_id,
                f'❌ Майже!\n'
                f'Ти написав: <s>{answer}</s>\n'
                f'✅ Правильно: <b>{w["word"].upper()}</b>\n'
                f'{w["translation"]} · 🔊 {w["transcription"]}',
                parse_mode=ParseMode.HTML
            )
        await asyncio.sleep(0.4)
        await present_question(user_id, context, chat_id)
        return True

    direction = payload['direction']
    if direction == 'ua2en':
        correct = type_is_correct(answer, w['word'])
        right_text = w['word'].upper()
    else:
        variants = translation_variants(w['translation']) or [w['translation']]
        correct = any(type_is_correct(answer, v) for v in variants)
        right_text = w['translation']

    db.record_answer(user_id, w['id'], correct,
                     user_typed=None if correct else answer)
    db.quiz_record(user_id, correct)

    if correct:
        await context.bot.send_message(
            chat_id,
            f'✅ Правильно!\n'
            f'<b>{w["word"].upper()}</b> {w["translation"]} · 🔊 {w["transcription"]}\n\n'
            f'▸ <i>{w["example1"]}</i>',
            parse_mode=ParseMode.HTML
        )
    else:
        await context.bot.send_message(
            chat_id,
            f'❌ Майже!\n'
            f'Ти написав: <s>{answer}</s>\n'
            f'✅ Правильно: <b>{right_text}</b>\n'
            f'🔊 {w["transcription"]}\n\n'
            f'▸ <i>{w["example1"]}</i>\n'
            f'▸ <i>{w["example2"]}</i>',
            parse_mode=ParseMode.HTML
        )
    await asyncio.sleep(0.4)
    await present_question(user_id, context, chat_id)
    return True


async def finish_quiz(user_id, context, chat_id):
    q = db.get_quiz(user_id)
    if not q:
        return
    kind = q['kind']
    results = q['results']  # список [word_id, ok]
    correct = sum(1 for _, ok in results if ok)
    total = q['total']
    all_ok = db.quiz_all_correct(user_id)

    wrong_words = []
    seen_wrong = set()
    for wid, ok in results:
        if not ok and wid not in seen_wrong:
            seen_wrong.add(wid)
            w = db.get_word_by_id(user_id, int(wid))
            if w:
                wrong_words.append(w['word'].upper())

    db.clear_quiz(user_id)

    if kind == 'levelup':
        cefr = db.get_cefr_level(user_id)
        nxt = _next_cefr(cefr) if cefr else None
        mistakes = total - correct
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f'✅ Перейти на {nxt}', callback_data='levelup:advance')],
            [InlineKeyboardButton(f'↩️ Залишитись на {cefr}', callback_data='levelup:stay')],
        ])
        if mistakes <= LEVELUP_MAX_MISTAKES:
            msg = (f'🎉 <b>Вітаю! {correct}/{total}</b>\n{DIV}\n'
                   f'Помилок: {mistakes} — чудовий результат!\n'
                   f'Ти готовий перейти на рівень <b>{nxt}</b> 🚀')
        else:
            msg = (f'📊 <b>{correct}/{total}</b>\n{DIV}\n'
                   f'Помилок: {mistakes}. Рекомендую ще трохи підучити {cefr} '
                   f'перед переходом 📚\n'
                   f'Але якщо хочеш — можеш перейти на {nxt} вже зараз.')
        await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if kind == 'official':
        if all_ok:
            nxt = db.unlock_next_batch(user_id)
            msg = (f'🎉 <b>ВІТАЮ! {correct}/{total}</b>\n{DIV}\n'
                   f'Ти пройшов офіційний тест на 100%! 💪\n')
            if nxt:
                msg += f'🔓 Розблоковано Партію #{nxt}!'
                await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML,
                                               reply_markup=main_menu_kb(user_id))
            elif db.get_word_source(user_id) == 'cefr':
                msg += '🎓 Генерую наступну партію слів...'
                await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML)
                await generate_cefr_batch(user_id, context, chat_id)
            else:
                msg += '🏆 Це була остання партія — ти красавчик! 🎉\n\nЩо далі?'
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton('✍️ Додати свої слова', callback_data='m:add')],
                    [InlineKeyboardButton('🎓 Перейти на CEFR (обрати рівень)', callback_data='s:src:cefr')],
                ])
                await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML,
                                               reply_markup=kb)
        else:
            db.set_official_cooldown(user_id, 10)
            msg = (f'😅 <b>{correct}/{total}</b>\n{DIV}\n'
                   f'Майже! Помилки в: {", ".join(wrong_words)}\n\n'
                   f'⏳ Спробуй знову через 10 хвилин.')
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton('🔄 Спробувати знову', callback_data='test:official')],
                [InlineKeyboardButton('◀️ Меню', callback_data='m:home')],
            ])
            await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML, reply_markup=kb)
    elif kind == 'gap':
        pct = round(correct / total * 100) if total else 0
        msg = f'✏️ <b>Речення з пропуском — завершено</b>\n{DIV}\n✅ {correct}/{total} ({pct}%)\n'
        if wrong_words:
            msg += f'😅 Повтори: {", ".join(wrong_words)}'
        else:
            msg += '🔥 Жодної помилки!'
        await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML,
                                       reply_markup=main_menu_kb(user_id))
    else:
        pct = round(correct / total * 100) if total else 0
        msg = f'🎯 <b>Тренування завершено</b>\n{DIV}\n✅ {correct}/{total} ({pct}%)\n'
        if wrong_words:
            msg += f'😅 Повтори: {", ".join(wrong_words)}'
        else:
            msg += '🔥 Жодної помилки!'
        await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML,
                                       reply_markup=main_menu_kb(user_id))


# ============ CALLBACK ROUTER ============
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    chat_id = query.message.chat_id
    await query.answer()

    db.add_user(user_id, query.from_user.first_name)

    if data == 'noop':
        return

    # ----- ONBOARDING: джерело слів -----
    if data == 'src:cefr':
        db.set_word_source(user_id, 'cefr')
        return await safe_edit(query, onboarding_cefr_text(), onboarding_cefr_kb())
    if data == 'src:own':
        db.set_word_source(user_id, 'own')
        return await safe_edit(query, onboarding_text(), onboarding_kb())

    # ----- ONBOARDING: вибір CEFR -----
    if data.startswith('cefr:') and data != 'cefr:gen':
        cefr = data.split(':')[1]
        if cefr in CEFR_LEVELS:
            old_cefr = db.get_cefr_level(user_id)
            db.set_cefr_level(user_id, cefr)
            # якщо рівень складності ще не обрано — наступний крок
            if db.get_level(user_id) is None:
                return await safe_edit(query, onboarding_text(), onboarding_kb())
            # інакше це зміна рівня з налаштувань — завершуємо стару партію
            # і одразу генеруємо нову для нового рівня
            if old_cefr != cefr:
                db.archive_active_batch(user_id)
            await safe_edit(query, f'✅ Рівень CEFR: {cefr}', None)
            await generate_cefr_batch(user_id, context, chat_id)
            return
        return

    # ----- ONBOARDING: рівень складності -----
    if data.startswith('lvl:'):
        lvl = int(data.split(':')[1])
        db.set_level(user_id, lvl)
        conf = LEVELS[lvl]
        src = db.get_word_source(user_id)
        await safe_edit(query,
                        f'✅ Складність: {conf["emoji"]} <b>{conf["name"]}</b> ({conf["desc"]})',
                        None)
        if src == 'cefr':
            cefr = db.get_cefr_level(user_id)
            await context.bot.send_message(chat_id, 'Готово! 🎓', reply_markup=persistent_kb())
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                '🎓 Отримати перші слова', callback_data='cefr:gen')]])
            await context.bot.send_message(
                chat_id,
                f'Рівень {cefr} обрано. Натисни кнопку щоб згенерувати перші слова 👇',
                reply_markup=kb
            )
        else:
            await context.bot.send_message(
                chat_id, 'Готово! Натисни ➕ Додати, щоб завантажити перші слова.',
                reply_markup=persistent_kb())
            text, kb = render_home(user_id)
            await context.bot.send_message(chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    # m:help і onboard:back доступні навіть під час онбордингу
    if data == 'm:help':
        if await needs_onboarding(user_id):
            return await safe_edit(query, help_text(), back_kb('onboard:back'))
        return await safe_edit(query, help_text(), back_kb('m:settings'))
    if data == 'onboard:back':
        return await send_onboarding_step(None, user_id, context, edit_query=query)

    # якщо ще не пройшов онбординг — змусити
    if await needs_onboarding(user_id):
        return await send_onboarding_step(None, user_id, context, edit_query=query)

    # ----- CEFR: згенерувати нову партію -----
    if data == 'cefr:gen':
        try:
            await query.message.delete()
        except Exception:
            pass
        await generate_cefr_batch(user_id, context, chat_id)
        return

    # ----- LEVEL-UP TEST -----
    if data == 'levelup:start':
        try:
            await query.message.delete()
        except Exception:
            pass
        await start_levelup_test(user_id, context, chat_id)
        return
    if data == 'levelup:advance':
        cefr = db.get_cefr_level(user_id)
        nxt = _next_cefr(cefr) if cefr else None
        if nxt:
            db.set_cefr_level(user_id, nxt)
            db.archive_active_batch(user_id)
            await safe_edit(query,
                            f'🚀 <b>Вітаю з переходом на {nxt}!</b>\n{DIV}\n'
                            f'Генерую перші слова нового рівня...', None)
            await generate_cefr_batch(user_id, context, chat_id)
        else:
            await safe_edit(query, '🏆 Ти вже на максимальному рівні C2!', None)
        return
    if data == 'levelup:stay':
        cefr = db.get_cefr_level(user_id)
        await safe_edit(query,
                        f'👍 Залишаємось на {cefr}. Продовжуй вчитись!', None)
        text, kb = render_home(user_id)
        await context.bot.send_message(chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    # ----- HINT (режим Експерт) -----
    if data == 'hint':
        await handle_hint(user_id, context, chat_id, query)
        return

    # ----- QUIZ choice answer -----
    if data.startswith('qa:'):
        selected = int(data.split(':')[1])
        await handle_choice_answer(user_id, selected, context, chat_id, query)
        return

    # HOME
    if data == 'm:home':
        text, kb = render_home(user_id)
        return await safe_edit(query, text, kb)

    # ADD
    if data == 'm:add':
        context.user_data['state'] = 'adding'
        return await safe_edit(
            query,
            '➕ Надішли слова англійською через кому:\n\n<code>struggle, effort, empty</code>',
            back_kb()
        )

    # LIST (active batch, compact)
    if data == 'm:list':
        text, kb = render_active_list(user_id)
        return await safe_edit(query, text, kb)

    # WORDS
    if data.startswith('m:words:'):
        idx = int(data.split(':')[2])
        text, kb = render_word_card(user_id, idx)
        return await safe_edit(query, text, kb)

    if data.startswith('w:learn:'):
        _, _, wid, idx = data.split(':')
        db.mark_learned(user_id, int(wid))
        text, kb = render_word_card(user_id, int(idx))
        await query.answer('✅ Перенесено в архів')
        return await safe_edit(query, text, kb)

    if data.startswith('w:del:'):
        _, _, wid, idx = data.split(':')
        db.delete_word(user_id, int(wid))
        text, kb = render_word_card(user_id, max(0, int(idx) - 1))
        await query.answer('🗑 Видалено')
        return await safe_edit(query, text, kb)

    # STATS
    if data == 'm:stats:today':
        text, kb = render_stats_today(user_id)
        return await safe_edit(query, text, kb)
    if data == 'm:stats:week':
        text, kb = render_stats_week(user_id)
        return await safe_edit(query, text, kb)
    if data == 'm:stats:all':
        text, kb = render_stats_all(user_id)
        return await safe_edit(query, text, kb)

    # TEST
    if data == 'm:test':
        return await safe_edit(
            query,
            f'🎯 <b>Тести</b>\n{DIV}\n'
            f'🎯 <b>Тренування</b> — будь-коли, не впливає на прогрес\n'
            f'🏆 <b>Офіційний</b> — лише 21:00–00:00, 100% відкриває нову партію',
            test_menu_kb()
        )

    if data == 'test:training':
        try:
            await query.message.delete()
        except Exception:
            pass
        await start_quiz(user_id, 'training', context, chat_id)
        return

    if data == 'test:official':
        hour = _now().hour
        if hour < 21:
            return await safe_edit(
                query,
                f'⏰ Офіційний тест доступний лише з <b>21:00 до 00:00</b> за Хошиміном.\n\n'
                f'Зараз {_now().strftime("%H:%M")}. Поки що тренуйся 🎯',
                back_kb('m:test')
            )
        rem = db.official_cooldown_remaining(user_id)
        if rem > 0:
            return await safe_edit(
                query,
                f'⏳ Зачекай ще <b>{rem // 60} хв {rem % 60} с</b> перед наступною спробою.',
                back_kb('m:test')
            )
        try:
            await query.message.delete()
        except Exception:
            pass
        await start_quiz(user_id, 'official', context, chat_id)
        return

    # SETTINGS
    if data == 'm:settings':
        text, kb = render_settings(user_id)
        return await safe_edit(query, text, kb)
    if data == 's:level':
        text, kb = render_level_picker(user_id)
        return await safe_edit(query, text, kb)
    if data.startswith('setlvl:'):
        lvl = int(data.split(':')[1])
        db.set_level(user_id, lvl)
        await query.answer(f'✅ Рівень: {LEVELS[lvl]["name"]}')
        text, kb = render_settings(user_id)
        return await safe_edit(query, text, kb)
    if data == 's:tz':
        text, kb = render_tz_picker(user_id)
        return await safe_edit(query, text, kb)
    if data == 's:cefr':
        text, kb = render_cefr_setting_picker(user_id)
        return await safe_edit(query, text, kb)
    if data == 's:src:cefr':
        db.set_word_source(user_id, 'cefr')
        if db.get_cefr_level(user_id) is None:
            text, kb = render_cefr_setting_picker(user_id)
            return await safe_edit(query, text, kb)
        await query.answer('✅ Режим CEFR')
        text, kb = render_settings(user_id)
        return await safe_edit(query, text, kb)
    if data == 's:src:own':
        db.set_word_source(user_id, 'own')
        await query.answer('✅ Свої слова')
        text, kb = render_settings(user_id)
        return await safe_edit(query, text, kb)
    if data.startswith('settz:'):
        tz = data.split(':', 1)[1]
        if tz in TIMEZONES:
            db.set_timezone(user_id, tz)
            await query.answer(f'✅ {TIMEZONES[tz]}')
        text, kb = render_settings(user_id)
        return await safe_edit(query, text, kb)
    if data.startswith('s:pause:'):
        days = int(data.split(':')[2])
        db.set_pause(user_id, days)
        text, kb = render_settings(user_id)
        await query.answer(f'⏸ Пауза на {days} дн.')
        return await safe_edit(query, text, kb)
    if data == 's:unpause':
        db.clear_pause(user_id)
        text, kb = render_settings(user_id)
        await query.answer('▶️ Пауза знята')
        return await safe_edit(query, text, kb)
    if data == 's:export':
        words = db.get_all_active_words(user_id)
        if not words:
            return await query.answer('Немає слів для експорту')
        lines = ['📤 <b>Твої слова</b>', DIV]
        for w in words:
            lines.append(f'{w["word"]} — {w["translation"]} [{w["transcription"]}]')
        return await safe_edit(query, '\n'.join(lines), back_kb('m:settings'))


async def safe_edit(query, text, kb):
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f'edit failed: {e}')
        try:
            await query.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass


# ============ TEXT HANDLER ============
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.add_user(user_id, update.effective_user.first_name)
    text = update.message.text or ''
    chat_id = update.message.chat_id

    # Постійні кнопки внизу
    if text == '📚 Меню':
        if await needs_onboarding(user_id):
            return await send_onboarding_step(update.message, user_id, context)
        home, kb = render_home(user_id)
        return await update.message.reply_text(home, reply_markup=kb, parse_mode=ParseMode.HTML)
    if text == '🎯 Тест':
        if await needs_onboarding(user_id):
            return await send_onboarding_step(update.message, user_id, context)
        return await update.message.reply_text(
            f'🎯 <b>Тести</b>\n{DIV}\n'
            f'🎯 <b>Тренування</b> — будь-коли\n'
            f'🏆 <b>Офіційний</b> — 21:00–00:00, 100% відкриває нову партію',
            reply_markup=test_menu_kb(), parse_mode=ParseMode.HTML)

    if await needs_onboarding(user_id):
        return await send_onboarding_step(update.message, user_id, context)

    # Додавання слів? (перевіряємо ПЕРШИМ — пріоритетніше за зависле питання тесту)
    if context.user_data.get('state') == 'adding':
        context.user_data['state'] = None
        db.clear_quiz(user_id)  # на випадок завислого питання
        await process_new_words(user_id, text, update.message, context)
        return

    # Активний тест?
    q = db.get_quiz(user_id)
    if q and q['current_q'] and q['current_word_id'] is not None:
        if q['current_q'].get('mode') in ('type', 'gap_type'):
            handled = await handle_type_answer(user_id, text, context, chat_id)
            if handled:
                return
        else:
            # питання з кнопками — підкажемо тиснути кнопку
            return await update.message.reply_text('👆 Обери відповідь кнопкою вище.')

    # За замовчуванням — меню
    home, kb = render_home(user_id)
    await update.message.reply_text(home, reply_markup=kb, parse_mode=ParseMode.HTML)


# ============ REMINDERS ============
async def _get_adaptive_words(user_id):
    """Слабкі та нові слова активної партії, відсортовані за точністю (гірші перші)."""
    words = db.get_active_batch_words(user_id)
    if not words:
        return []
    scored = []
    for w in words:
        total = w['total_answers']
        acc = (w['correct_answers'] / total) if total else -1  # нові (-1) йдуть першими
        scored.append((acc, w))
    scored.sort(key=lambda x: x[0])
    return [w for _, w in scored]


async def start_quiz_words(user_id, kind, word_list, context, chat_id):
    """Запускає тест по конкретному списку слів."""
    if not word_list:
        await context.bot.send_message(chat_id, '📭 Немає слів для тесту.')
        return False
    ids = [w['id'] for w in word_list]
    db.start_quiz(user_id, kind, ids)
    lvl = db.get_level(user_id) or 3
    conf = LEVELS[lvl]

    if kind == 'gap':
        mode_hint = 'обирай кнопкою' if conf['mode'] == 'choice' else 'пиши слово вручну'
        await context.bot.send_message(
            chat_id,
            f'✏️ <b>Речення з пропуском</b>\n{DIV}\n'
            f'{len(ids)} речень · {mode_hint} 👇',
            parse_mode=ParseMode.HTML
        )
    else:
        mode_hint = ('обирай кнопкою' if conf['mode'] == 'choice' else 'пиши вручну')
        await context.bot.send_message(
            chat_id,
            f'🎯 <b>Тренування</b>\n{DIV}\n{len(ids)} слів · {mode_hint} 💪',
            parse_mode=ParseMode.HTML
        )
    await present_question(user_id, context, chat_id)
    return True


async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    kind = job_data['kind']
    local_hour = job_data['local_hour']
    local_minute = job_data['local_minute']
    import pytz as _pytz
    for u in db.get_all_users():
        uid = u['user_id']
        if u.get('level') is None:
            continue
        if db.is_paused(uid):
            continue
        # Check: is it the right local time for this user?
        user_tz = _pytz.timezone(db.get_timezone(uid))
        user_now = datetime.now(user_tz)
        if user_now.hour != local_hour or user_now.minute // 30 != local_minute // 30:
            continue
        words = db.get_active_batch_words(uid)
        if not words:
            continue
        try:
            if kind == 'morning':
                # 9:00 — показати всі слова з транскрипцією і реченнями
                lines = ['📚 <b>Слова на сьогодні</b>', DIV]
                for i, w in enumerate(words, 1):
                    lines.append(
                        f'{i}. <b>{w["word"].upper()}</b> {w["translation"]} · 🔊 {w["transcription"]}\n'
                        f'   ▸ <i>{w["example1"]}</i>\n'
                        f'   ▸ <i>{w["example2"]}</i>'
                    )
                lines.append('')
                tz_label = TIMEZONES.get(db.get_timezone(uid), '')
                lines.append(f'О 10:30 — міні-тест по цих словах 🎯 ({tz_label})')
                await context.bot.send_message(uid, '\n'.join(lines), parse_mode=ParseMode.HTML)

            elif kind == 'mini':
                # 10:30 — легкий тест по всіх словах з ранку
                # Скидаємо недограну сесію, щоб нагадування завжди приходило
                db.clear_quiz(uid)
                await context.bot.send_message(
                    uid,
                    f'🎯 <b>Міні-тест</b>\n{DIV}\nПеревір слова з ранку 👇',
                    parse_mode=ParseMode.HTML
                )
                await start_quiz(uid, 'training', context, uid)

            elif kind == 'training':
                # 12:00 і 18:00 — адаптивне тренування: слабкі та нові першими
                db.clear_quiz(uid)  # скидаємо недограну сесію
                adaptive = await _get_adaptive_words(uid)
                await start_quiz_words(uid, 'training', adaptive, context, uid)

            elif kind == 'gap':
                # 14:30 — речення з пропуском по всіх словах активної партії, послідовно (квіз)
                db.clear_quiz(uid)
                gap_words = words.copy()
                random.shuffle(gap_words)
                await start_quiz_words(uid, 'gap', gap_words, context, uid)

            elif kind == 'official':
                await context.bot.send_message(
                    uid,
                    f'🏆 <b>Час офіційного тесту!</b>\n{DIV}\n'
                    f'Здай на 100% до 00:00 — і відкриється нова партія.',
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton('🏆 Почати тест', callback_data='test:official')]
                    ])
                )
        except Exception as e:
            logger.error(f'reminder {kind} for {uid}: {e}')


async def post_init(application: Application):
    jq = application.job_queue
    # Schedule jobs every 30 min — reminder_job checks each user's timezone
    # Also keep direct slots for all possible reminder times across both zones
    scheduled = set()
    import pytz as _pytz
    for tz_name in TIMEZONES.keys():
        tz_obj = _pytz.timezone(tz_name)
        for h, m, kind in REMINDERS:
            # Convert local time to UTC
            from datetime import datetime as _dt
            local_dt = tz_obj.localize(_dt(2000, 1, 1, h, m, 0))
            utc_dt = local_dt.astimezone(_pytz.utc)
            key = (utc_dt.hour, utc_dt.minute, kind)
            if key not in scheduled:
                scheduled.add(key)
                jq.run_daily(
                    reminder_job,
                    time=time(hour=utc_dt.hour, minute=utc_dt.minute, tzinfo=_pytz.utc),
                    data={'kind': kind, 'local_hour': h, 'local_minute': m},
                    name=f'rem_{utc_dt.hour}_{utc_dt.minute}_{kind}'
                )
    logger.info(f'Reminders scheduled: {len(scheduled)} slots for {list(TIMEZONES.keys())}')


def main():
    token = os.environ.get('TELEGRAM_TOKEN')
    if not token:
        raise ValueError('TELEGRAM_TOKEN not set')

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CommandHandler('add', cmd_add))
    app.add_handler(CommandHandler('menu', cmd_menu))
    app.add_handler(CommandHandler('words', cmd_menu))
    app.add_handler(CommandHandler('stats', cmd_menu))
    app.add_handler(CommandHandler('test', cmd_menu))
    app.add_handler(CommandHandler('help', cmd_help))
    app.add_handler(CommandHandler('instruction', cmd_help))
    app.add_handler(CommandHandler('fixdistractors', cmd_fixdistractors))
    app.add_handler(CommandHandler('setword', cmd_setword))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info('WordFlow bot v2 starting...')
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
