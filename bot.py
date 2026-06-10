import os
import logging
import asyncio
from datetime import time

import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

from database import Database, _now
from claude_api import get_word_info

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

TZ = pytz.timezone('Asia/Ho_Chi_Minh')
db = Database()

DIV = '━━━━━━━━━━━━━━━━'

# Reminder schedule (Ho Chi Minh time)
REMINDERS = [
    (9, 0, 'morning'),
    (12, 0, 'training'),
    (15, 0, 'training'),
    (18, 0, 'training'),
    (21, 0, 'official'),
]


# ============ KEYBOARDS ============
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


# ============ RENDER: HOME ============
def render_home(user_id):
    u = db.get_user(user_id)
    name = u['name'] if u else ''
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
        weekday = names[__import_weekday(d['date'])]
        filled = round(d['answered'] / maxv * 10)
        bar = '█' * filled + '░' * (10 - filled)
        text.append(f'{weekday} {bar} {d["answered"]}')
        total_ans += d['answered']
    text.append(DIV)
    text.append(f'Всього за тиждень: {total_ans} відповідей')
    return '\n'.join(text), stats_tabs_kb('week')


def __import_weekday(date_str):
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
    paused = db.is_paused(user_id)
    text = ['⚙️ <b>Налаштування</b>', DIV]
    text.append('🕘 Нагадування (за Хошиміном):')
    text.append('9:00 · 12:00 · 15:00 · 18:00 · 21:00')
    text.append(DIV)
    text.append('⏸ Пауза: ' + ('так, на паузі' if paused else 'вимкнено'))

    rows = []
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


# ============ COMMANDS ============
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.add_user(user.id, user.first_name)
    text, kb = render_home(user.id)
    await update.message.reply_text(
        f'👋 Привіт, {user.first_name}! Я допоможу вивчати англійські слова.\n\n' + text,
        reply_markup=kb, parse_mode=ParseMode.HTML
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.add_user(update.effective_user.id, update.effective_user.first_name)
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
    text, kb = render_home(update.effective_user.id)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


# ============ ADD WORDS ============
async def process_new_words(user_id, raw_text, message, context):
    parts = []
    for chunk in raw_text.replace('\n', ',').replace(';', ',').split(','):
        c = chunk.strip().lower()
        if c:
            parts.append(c)
    parts = [p for p in dict.fromkeys(parts)]  # unique, keep order

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

    lines = [f'✅ Додано {added} слів:', DIV]
    for wd in words_data:
        lines.append(f'• <b>{wd["word"].upper()}</b> 🔊 {wd["transcription"]} — {wd["translation"]}')
    lines.append('')
    lines.append('Слова розбито на партії по 10. Перша партія активна! 🚀')

    await status.edit_text('\n'.join(lines), parse_mode=ParseMode.HTML,
                           reply_markup=main_menu_kb())


# ============ QUIZ ENGINE (UA -> EN typing) ============
async def start_quiz(user_id, kind, context, chat_id):
    words = db.get_active_batch_words(user_id)
    if not words:
        await context.bot.send_message(chat_id, '📭 Немає активних слів для тесту.')
        return False
    import random
    ids = [w['id'] for w in words]
    random.shuffle(ids)
    db.start_quiz(user_id, kind, ids)
    if kind == 'official':
        await context.bot.send_message(
            chat_id,
            f'🏆 <b>ОФІЦІЙНИЙ ТЕСТ</b>\n{DIV}\n'
            f'{len(ids)} слів. Напиши кожне правильно — і відкриється наступна партія!\n'
            f'Помилка = доведеться повторити.',
            parse_mode=ParseMode.HTML
        )
    else:
        await context.bot.send_message(
            chat_id,
            f'🎯 <b>Тренування</b>\n{DIV}\n{len(ids)} слів. Пиши англійською 💪',
            parse_mode=ParseMode.HTML
        )
    await send_next_question(user_id, context, chat_id)
    return True


async def send_next_question(user_id, context, chat_id):
    wid = db.quiz_next(user_id)
    if wid is None:
        await finish_quiz(user_id, context, chat_id)
        return
    w = db.get_word_by_id(user_id, wid)
    if not w:
        await send_next_question(user_id, context, chat_id)
        return
    hint = w['word'][:2] + '…'
    q = db.get_quiz(user_id)
    done = q['total'] - len(q['remaining'])
    await context.bot.send_message(
        chat_id,
        f'✍️ <b>{done}/{q["total"]}</b>\n\n'
        f'Напиши англійською:\n'
        f'🇺🇦 <b>{w["translation"].upper()}</b>\n'
        f'🔊 підказка: {hint}',
        parse_mode=ParseMode.HTML
    )


async def handle_quiz_answer(user_id, text, context, chat_id):
    q = db.get_quiz(user_id)
    if not q or q['current_word_id'] is None:
        return False
    w = db.get_word_by_id(user_id, q['current_word_id'])
    if not w:
        return False

    answer = text.strip().lower()
    correct = answer == w['word'].lower()
    db.record_answer(user_id, w['id'], correct, user_typed=None if correct else answer)
    db.quiz_record(user_id, correct)

    if correct:
        await context.bot.send_message(chat_id, '✅ Правильно!')
    else:
        await context.bot.send_message(
            chat_id,
            f'❌ Майже!\n'
            f'Ти написав: <s>{answer}</s>\n'
            f'✅ Правильно: <b>{w["word"].upper()}</b>\n'
            f'🔊 {w["transcription"]}\n\n'
            f'▸ <i>{w["example1"]}</i>\n'
            f'▸ <i>{w["example2"]}</i>',
            parse_mode=ParseMode.HTML
        )
    await asyncio.sleep(0.4)
    await send_next_question(user_id, context, chat_id)
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
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton('🎯 Тренування', callback_data='test:training')],
            [InlineKeyboardButton('🏆 Офіційний тест', callback_data='test:official')],
            [InlineKeyboardButton('◀️ Меню', callback_data='m:home')],
        ])
        return await safe_edit(
            query,
            f'🎯 <b>Тести</b>\n{DIV}\n'
            f'🎯 <b>Тренування</b> — будь-коли, не впливає на прогрес\n'
            f'🏆 <b>Офіційний</b> — лише 21:00–00:00, 100% відкриває нову партію',
            kb
        )

    if data == 'test:training':
        await query.message.delete()
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
        await query.message.delete()
        await start_quiz(user_id, 'official', context, chat_id)
        return

    # SETTINGS
    if data == 'm:settings':
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
    text = update.message.text

    # Active quiz?
    q = db.get_quiz(user_id)
    if q and q['current_word_id'] is not None:
        handled = await handle_quiz_answer(user_id, text, context, update.message.chat_id)
        if handled:
            return

    # Adding words?
    if context.user_data.get('state') == 'adding':
        context.user_data['state'] = None
        await process_new_words(user_id, text, update.message, context)
        return

    # Default: show menu
    home, kb = render_home(user_id)
    await update.message.reply_text(home, reply_markup=kb, parse_mode=ParseMode.HTML)


# ============ REMINDERS ============
async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    kind = context.job.data
    for u in db.get_all_users():
        uid = u['user_id']
        if db.is_paused(uid):
            continue
        words = db.get_active_batch_words(uid)
        if not words:
            continue
        try:
            if kind == 'morning':
                lines = ['📚 <b>Слова на сьогодні</b>', DIV]
                for i, w in enumerate(words, 1):
                    lines.append(
                        f'{i}. <b>{w["word"].upper()}</b> 🔊 {w["transcription"]} · {w["translation"]}\n'
                        f'   ▸ <i>{w["example1"]}</i>\n'
                        f'   ▸ <i>{w["example2"]}</i>'
                    )
                lines.append('')
                lines.append('Наступне нагадування о 12:00 🕘')
                await context.bot.send_message(uid, '\n'.join(lines), parse_mode=ParseMode.HTML)
            elif kind == 'training':
                await start_quiz(uid, 'training', context, uid)
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
    for h, m, kind in REMINDERS:
        jq.run_daily(reminder_job, time=time(hour=h, minute=m, tzinfo=TZ), data=kind,
                     name=f'rem_{h}_{m}')
    logger.info('Reminders scheduled (Asia/Ho_Chi_Minh)')


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

    logger.info('WordFlow bot starting...')
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
