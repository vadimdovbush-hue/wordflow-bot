import os
import logging
import asyncio
import random
from datetime import time

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

from database import Database, _now
from claude_api import get_word_info, FALLBACK_DISTRACTORS

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
    2: {'name': 'Середній', 'emoji': '🔵', 'batch': 8,  'buttons': 5, 'mode': 'choice', 'desc': '8 слів • вибір з 5'},
    3: {'name': 'Складний', 'emoji': '🟠', 'batch': 12, 'buttons': 8, 'mode': 'choice', 'desc': '12 слів • вибір з 8'},
    4: {'name': 'Експерт',  'emoji': '🔴', 'batch': 15, 'buttons': 0, 'mode': 'type',   'desc': '15 слів • писати вручну'},
}

TIMEZONES = {
    'Asia/Ho_Chi_Minh': '🇻🇳 Хошимін (UTC+7)',
    'Europe/Kiev': '🇺🇦 Київ (UTC+2/+3)',
}

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


# ============ KEYBOARDS ============
def persistent_kb():
    return ReplyKeyboardMarkup(
        [['📚 Меню', '🎯 Тест']],
        resize_keyboard=True, is_persistent=True
    )


def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('📚 Мої слова', callback_data='m:words:0'),
         InlineKeyboardButton('➕ Додати', callback_data='m:add')],
        [InlineKeyboardButton('📊 Статистика', callback_data='m:stats:today'),
         InlineKeyboardButton('🎯 Тест', callback_data='m:test')],
        [InlineKeyboardButton('⚙️ Налаштування', callback_data='m:settings')],
    ])


def back_kb(target='m:home'):
    return InlineKeyboardMarkup([[InlineKeyboardButton('◀️ Назад', callback_data=target)]])


def test_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('🎯 Тренування', callback_data='test:training')],
        [InlineKeyboardButton('🏆 Офіційний тест', callback_data='test:official')],
        [InlineKeyboardButton('◀️ Меню', callback_data='m:home')],
    ])


# ============ ONBOARDING ============
def onboarding_text():
    return (
        '🎚 <b>Обери рівень складності</b>\n' + DIV + '\n'
        'Від рівня залежить розмір партії та тип тесту:\n\n'
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
    return db.get_level(user_id) is None


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

    lines = [greet, '', DIV]
    lines.append(f'{conf["emoji"]} Рівень: {conf["name"]} ({conf["desc"]})')
    if batch_no:
        lines.append(f'📚 Партія #{batch_no} · {len(active)} слів')
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

    return '\n'.join(lines), main_menu_kb()


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
        f'🔊 {w["transcription"]} · {w["translation"]}\n\n'
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
    text = ['⚙️ <b>Налаштування</b>', DIV]
    text.append(f'{conf["emoji"]} Рівень: <b>{conf["name"]}</b> ({conf["desc"]})')
    text.append(DIV)
    text.append('🕘 Нагадування (за Хошиміном):')
    text.append('9:00 · 12:00 · 15:00 · 18:00 · 21:00')
    text.append(DIV)
    text.append('⏸ Пауза: ' + ('так, на паузі' if paused else 'вимкнено'))

    tz = db.get_timezone(user_id)
    tz_label = TIMEZONES.get(tz, tz)
    text.append(f'🕐 Час нагадувань: {tz_label}')

    rows = [
        [InlineKeyboardButton('🎚 Змінити рівень', callback_data='s:level')],
        [InlineKeyboardButton('🕐 Змінити часовий пояс', callback_data='s:tz')],
    ]
    if paused:
        rows.append([InlineKeyboardButton('▶️ Зняти паузу', callback_data='s:unpause')])
    else:
        rows.append([
            InlineKeyboardButton('⏸ 1 день', callback_data='s:pause:1'),
            InlineKeyboardButton('⏸ 3 дні', callback_data='s:pause:3'),
            InlineKeyboardButton('⏸ 7 днів', callback_data='s:pause:7'),
        ])
    rows.append([InlineKeyboardButton('📤 Експорт слів', callback_data='s:export')])
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
        await update.message.reply_text(
            onboarding_text(), reply_markup=onboarding_kb(), parse_mode=ParseMode.HTML
        )
        return

    await update.message.reply_text(
        f'👋 Привіт, {user.first_name}!', reply_markup=persistent_kb()
    )
    text, kb = render_home(user.id)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.add_user(update.effective_user.id, update.effective_user.first_name)
    if await needs_onboarding(update.effective_user.id):
        await update.message.reply_text(
            onboarding_text(), reply_markup=onboarding_kb(), parse_mode=ParseMode.HTML)
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
        await update.message.reply_text(
            onboarding_text(), reply_markup=onboarding_kb(), parse_mode=ParseMode.HTML)
        return
    text, kb = render_home(update.effective_user.id)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


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
        lines.append(f'• <b>{wd["word"].upper()}</b> 🔊 {wd["transcription"]} — {wd["translation"]}')
    lines.append('')
    lines.append(f'Слова розбито на партії по {bsize}. Перша партія активна! 🚀')

    await status.edit_text('\n'.join(lines), parse_mode=ParseMode.HTML,
                           reply_markup=main_menu_kb())


# ============ QUIZ ENGINE ============
def build_question(user_id, w, level):
    conf = LEVELS[level]
    direction = random.choice(['ua2en', 'en2uk'])
    payload = {'word_id': w['id'], 'direction': direction, 'mode': conf['mode']}

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
    ids = [w['id'] for w in words]
    random.shuffle(ids)
    db.start_quiz(user_id, kind, ids)

    lvl = db.get_level(user_id) or 3
    conf = LEVELS[lvl]
    mode_hint = ('обирай правильний варіант кнопкою'
                 if conf['mode'] == 'choice' else 'пиши відповідь вручну')
    if kind == 'official':
        await context.bot.send_message(
            chat_id,
            f'🏆 <b>ОФІЦІЙНИЙ ТЕСТ</b>\n{DIV}\n'
            f'{len(ids)} слів · {conf["emoji"]} {conf["name"]}\n'
            f'Дай усе правильно — і відкриється наступна партія!\n'
            f'Помилка = доведеться повторити.',
            parse_mode=ParseMode.HTML
        )
    else:
        await context.bot.send_message(
            chat_id,
            f'🎯 <b>Тренування</b>\n{DIV}\n{len(ids)} слів · {mode_hint} 💪',
            parse_mode=ParseMode.HTML
        )
    await present_question(user_id, context, chat_id)
    return True


async def present_question(user_id, context, chat_id):
    wid = db.quiz_next(user_id)
    if wid is None:
        await finish_quiz(user_id, context, chat_id)
        return
    w = db.get_word_by_id(user_id, wid)
    if not w:
        await present_question(user_id, context, chat_id)
        return

    lvl = db.get_level(user_id) or 3
    payload = build_question(user_id, w, lvl)
    db.set_current_question(user_id, payload)

    q = db.get_quiz(user_id)
    done = q['total'] - len(q['remaining'])
    total = q['total']

    if payload['mode'] == 'choice':
        flag = payload['flag']
        prompt = payload['prompt']
        rows = []
        for i, opt in enumerate(payload['options']):
            rows.append([InlineKeyboardButton(opt,
                                              callback_data=f'qa:{i}')])
        await context.bot.send_message(
            chat_id,
            f'❓ <b>{done}/{total}</b>\n{DIV}\n'
            f'{flag} <b>{prompt.upper()}</b>\n\n'
            f'Обери правильний варіант 👇',
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows)
        )
    else:
        direction = payload['direction']
        w_full = db.get_word_by_id(user_id, wid)
        if direction == 'ua2en':
            flag, prompt, instr = '🇺🇦', w_full['translation'], 'Напиши англійською 👇'
        else:
            flag, prompt, instr = '🇬🇧', w_full['word'], 'Напиши переклад українською 👇'
        await context.bot.send_message(
            chat_id,
            f'✍️ <b>{done}/{total}</b>\n{DIV}\n'
            f'{flag} <b>{prompt.upper()}</b>\n\n{instr}',
            parse_mode=ParseMode.HTML
        )


async def handle_choice_answer(user_id, selected, context, chat_id, query):
    q = db.get_quiz(user_id)
    if not q or not q['current_q'] or q['current_word_id'] is None:
        return
    payload = q['current_q']
    if payload.get('mode') != 'choice':
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

    # прибрати кнопки з питання
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if correct:
        await context.bot.send_message(
            chat_id,
            f'✅ Правильно!\n'
            f'<b>{w["word"].upper()}</b> 🔊 {w["transcription"]} · {w["translation"]}\n\n'
            f'▸ <i>{w["example1"]}</i>',
            parse_mode=ParseMode.HTML
        )
    else:
        await context.bot.send_message(
            chat_id,
            f'❌ Не вгадав.\n'
            f'Ти обрав: <s>{chosen_text}</s>\n'
            f'✅ Правильно: <b>{options[correct_index]}</b>\n'
            f'🔊 {w["transcription"]} · {w["word"].upper()} — {w["translation"]}\n\n'
            f'▸ <i>{w["example1"]}</i>',
            parse_mode=ParseMode.HTML
        )
    await asyncio.sleep(0.4)
    await present_question(user_id, context, chat_id)


async def handle_type_answer(user_id, text, context, chat_id):
    q = db.get_quiz(user_id)
    if not q or not q['current_q'] or q['current_word_id'] is None:
        return False
    payload = q['current_q']
    if payload.get('mode') != 'type':
        return False
    w = db.get_word_by_id(user_id, q['current_word_id'])
    if not w:
        return False

    answer = text.strip()
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
            f'<b>{w["word"].upper()}</b> 🔊 {w["transcription"]} · {w["translation"]}\n\n'
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
    results = q['results']
    correct = sum(1 for v in results.values() if v)
    total = q['total']
    all_ok = db.quiz_all_correct(user_id)

    wrong_words = []
    for wid, ok in results.items():
        if not ok:
            w = db.get_word_by_id(user_id, int(wid))
            if w:
                wrong_words.append(w['word'].upper())

    db.clear_quiz(user_id)

    if kind == 'official':
        if all_ok:
            nxt = db.unlock_next_batch(user_id)
            msg = (f'🎉 <b>ВІТАЮ! {correct}/{total}</b>\n{DIV}\n'
                   f'Ти пройшов офіційний тест на 100%! 💪\n')
            if nxt:
                msg += f'🔓 Розблоковано Партію #{nxt}!'
            else:
                msg += '🏆 Це була остання партія — ти красавчик!'
            await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML,
                                           reply_markup=main_menu_kb())
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
    else:
        pct = round(correct / total * 100) if total else 0
        msg = f'🎯 <b>Тренування завершено</b>\n{DIV}\n✅ {correct}/{total} ({pct}%)\n'
        if wrong_words:
            msg += f'😅 Повтори: {", ".join(wrong_words)}'
        else:
            msg += '🔥 Жодної помилки!'
        await context.bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML,
                                       reply_markup=main_menu_kb())


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

    # Gap quiz answer
    if data.startswith('gap:'):
        parts = data.split(':')
        # gap:{uid}:{word_id}:{correct_index}:{selected}
        correct_index = int(parts[3])
        selected = int(parts[4])
        word_id = int(parts[2])
        w = db.get_word_by_id(user_id, word_id)
        correct = (selected == correct_index)
        if w:
            db.record_answer(user_id, w['id'], correct)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if correct:
            await context.bot.send_message(
                chat_id,
                f'✅ Правильно!\n'
                f'<b>{w["word"].upper()}</b> 🔊 {w["transcription"]} · {w["translation"]}' if w else '✅ Правильно!',
                parse_mode=ParseMode.HTML
            )
        else:
            await context.bot.send_message(
                chat_id,
                f'❌ Неправильно. Правильна відповідь: <b>{w["word"].upper()}</b>\n'
                f'🔊 {w["transcription"]} · {w["translation"]}' if w else '❌ Неправильно.',
                parse_mode=ParseMode.HTML
            )
        return

    # ----- ONBOARDING level pick -----
    if data.startswith('lvl:'):
        lvl = int(data.split(':')[1])
        db.set_level(user_id, lvl)
        conf = LEVELS[lvl]
        await safe_edit(query,
                        f'✅ Рівень: {conf["emoji"]} <b>{conf["name"]}</b> ({conf["desc"]})',
                        None)
        await context.bot.send_message(
            chat_id, 'Готово! Натисни ➕ Додати, щоб завантажити перші слова.',
            reply_markup=persistent_kb())
        text, kb = render_home(user_id)
        await context.bot.send_message(chat_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    # якщо ще не пройшов онбординг — змусити
    if await needs_onboarding(user_id):
        return await safe_edit(query, onboarding_text(), onboarding_kb())

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
            return await update.message.reply_text(
                onboarding_text(), reply_markup=onboarding_kb(), parse_mode=ParseMode.HTML)
        home, kb = render_home(user_id)
        return await update.message.reply_text(home, reply_markup=kb, parse_mode=ParseMode.HTML)
    if text == '🎯 Тест':
        if await needs_onboarding(user_id):
            return await update.message.reply_text(
                onboarding_text(), reply_markup=onboarding_kb(), parse_mode=ParseMode.HTML)
        return await update.message.reply_text(
            f'🎯 <b>Тести</b>\n{DIV}\n'
            f'🎯 <b>Тренування</b> — будь-коли\n'
            f'🏆 <b>Офіційний</b> — 21:00–00:00, 100% відкриває нову партію',
            reply_markup=test_menu_kb(), parse_mode=ParseMode.HTML)

    if await needs_onboarding(user_id):
        return await update.message.reply_text(
            onboarding_text(), reply_markup=onboarding_kb(), parse_mode=ParseMode.HTML)

    # Активний тест?
    q = db.get_quiz(user_id)
    if q and q['current_q'] and q['current_word_id'] is not None:
        if q['current_q'].get('mode') == 'type':
            handled = await handle_type_answer(user_id, text, context, chat_id)
            if handled:
                return
        else:
            # питання з кнопками — підкажемо тиснути кнопку
            return await update.message.reply_text('👆 Обери відповідь кнопкою вище.')

    # Додавання слів?
    if context.user_data.get('state') == 'adding':
        context.user_data['state'] = None
        await process_new_words(user_id, text, update.message, context)
        return

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
                        f'{i}. <b>{w["word"].upper()}</b> 🔊 {w["transcription"]} · {w["translation"]}\n'
                        f'   ▸ <i>{w["example1"]}</i>\n'
                        f'   ▸ <i>{w["example2"]}</i>'
                    )
                lines.append('')
                tz_label = TIMEZONES.get(db.get_timezone(uid), '')
                lines.append(f'О 10:30 — міні-тест по цих словах 🎯 ({tz_label})')
                await context.bot.send_message(uid, '\n'.join(lines), parse_mode=ParseMode.HTML)

            elif kind == 'mini':
                # 10:30 — легкий тест по всіх словах з ранку
                await context.bot.send_message(
                    uid,
                    f'🎯 <b>Міні-тест</b>\n{DIV}\nПеревір слова з ранку 👇',
                    parse_mode=ParseMode.HTML
                )
                await start_quiz(uid, 'training', context, uid)

            elif kind == 'training':
                # 12:00 і 18:00 — адаптивне тренування: слабкі та нові першими
                adaptive = await _get_adaptive_words(uid)
                await start_quiz_words(uid, 'training', adaptive, context, uid)

            elif kind == 'gap':
                # 14:30 — речення з пропуском
                gap_words = words.copy()
                random.shuffle(gap_words)
                await _send_gap_quiz(uid, gap_words, context)

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


async def _send_gap_quiz(user_id, words, context):
    """14:30 — речення з пропуском для кожного слова."""
    lvl = db.get_level(user_id) or 3
    conf = LEVELS[lvl]
    nbtn = min(conf['buttons'], 4) if conf['mode'] == 'choice' else 4  # макс 4 кнопки для gap

    lines = [f'✏️ <b>Речення з пропуском</b>\n{DIV}\nВстав правильне слово 👇']
    await context.bot.send_message(user_id, '\n'.join(lines), parse_mode=ParseMode.HTML)

    for w in words[:min(len(words), 5)]:  # максимум 5 речень
        # Замінюємо слово в реченні на ___
        sentence = w['example1']
        gapped = _make_gap(sentence, w['word'])

        # Варіанти: правильне + дистрактори
        pool = list(w.get('distractors_en', []) or [])
        random.shuffle(pool)
        options = pool[:nbtn - 1] + [w['word']]
        random.shuffle(options)
        correct_index = options.index(w['word'])

        rows = [[InlineKeyboardButton(opt, callback_data=f'gap:{user_id}:{w["id"]}:{correct_index}:{i}')]
                for i, opt in enumerate(options)]

        await context.bot.send_message(
            user_id,
            f'🇬🇧 <i>{gapped}</i>\n\n'
            f'🔊 {w["transcription"]} · {w["translation"]}',
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows)
        )
        await asyncio.sleep(0.3)


def _make_gap(sentence, word):
    """Замінює слово (або його форму) на ___ в реченні."""
    import re
    pattern = re.compile(re.escape(word), re.IGNORECASE)
    result = pattern.sub('___', sentence, count=1)
    if result == sentence:
        result = sentence + ' (___)'
    return result


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
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info('WordFlow bot v2 starting...')
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
