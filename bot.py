import logging
import asyncio
from datetime import datetime, time
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from database import Database
from claude_api import get_word_info, generate_quiz

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

KYIV_TZ = pytz.timezone('Europe/Kiev')
db = Database()

REMINDER_TIMES = [
    (9, 0, 'morning'),
    (12, 0, 'quiz_en_ua'),
    (15, 0, 'fill_blank'),
    (18, 0, 'quiz_ua_en'),
    (21, 0, 'final'),
]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.add_user(user.id, user.first_name)
    await update.message.reply_text(
        f"👋 Привіт, {user.first_name}!\n\n"
        "Я допоможу тобі вчити англійські слова методом Spaced Repetition.\n\n"
        "📝 Команди:\n"
        "/add слово1, слово2 — додати слова\n"
        "/words — мої активні слова\n"
        "/test — тест прямо зараз\n"
        "/stats — моя статистика\n"
        "/help — допомога\n\n"
        "Починай з /add 💪"
    )

async def add_words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.add_user(user_id, update.effective_user.first_name)

    if not context.args:
        await update.message.reply_text("Використання: /add слово1, слово2, слово3")
        return

    text = ' '.join(context.args)
    words = [w.strip().lower() for w in text.replace(',', ' ').split() if w.strip()]

    if not words:
        await update.message.reply_text("Не знайшов слів. Спробуй: /add struggle, effort, empty")
        return

    msg = await update.message.reply_text(f"⏳ Обробляю {len(words)} слів через AI...")

    added = []
    for word in words:
        if db.word_exists(user_id, word):
            continue
        info = await get_word_info(word)
        if info:
            db.add_word(user_id, word, info['transcription'], info['translation'],
                       info['example1'], info['example2'])
            added.append((word, info))

    if not added:
        await msg.edit_text("Всі ці слова вже є у твоєму списку!")
        return

    response = f"✅ Додано {len(added)} слів:\n\n"
    for word, info in added:
        response += f"• {word.upper()} 🗣️ {info['transcription']} = {info['translation']}\n"
    response += "\nПерше нагадування о 9:00 🕘"

    await msg.edit_text(response)

async def show_words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    words = db.get_active_words(user_id)

    if not words:
        await update.message.reply_text("У тебе поки немає активних слів.\nДодай: /add struggle, effort")
        return

    response = f"📚 Твої активні слова ({len(words)}):\n\n"
    for w in words:
        reps = w['repetitions']
        stars = "⭐" * min(reps, 5)
        response += f"• {w['word'].upper()} — {w['translation']} {stars}\n"

    await update.message.reply_text(response)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = db.get_stats(user_id)

    await update.message.reply_text(
        f"📊 Твоя статистика:\n\n"
        f"📚 Активних слів: {s['active']}\n"
        f"✅ Вивчених слів: {s['learned']}\n"
        f"🔥 Всього повторень: {s['total_reps']}\n"
        f"🎯 Правильних відповідей: {s['correct']}%"
    )

async def send_morning_cards(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    words = db.get_todays_words(user_id)
    if not words:
        return

    response = "📚 Твої слова на сьогодні:\n\n"
    for i, w in enumerate(words, 1):
        response += (
            f"{i}. {w['word'].upper()} 🗣️ {w['transcription']}\n"
            f"🇺🇦 {w['translation']}\n"
            f"• {w['example1']}\n"
            f"• {w['example2']}\n\n"
        )

    await context.bot.send_message(chat_id=user_id, text=response)
    db.set_quiz_state(user_id, 'morning_done', None)

async def send_quiz_en_ua(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    words = db.get_todays_words(user_id)
    if not words:
        return

    db.set_quiz_state(user_id, 'quiz_en_ua', [w['word'] for w in words])
    await send_next_quiz_en_ua(context, user_id)

async def send_next_quiz_en_ua(context, user_id: int):
    state = db.get_quiz_state(user_id)
    if not state or not state.get('remaining'):
        await context.bot.send_message(chat_id=user_id, text="✅ Квіз завершено! Молодець 💪")
        return

    word_name = state['remaining'][0]
    word = db.get_word(user_id, word_name)
    if not word:
        return

    quiz = await generate_quiz(word, 'en_ua', db.get_all_translations(user_id))

    keyboard = [[InlineKeyboardButton(opt, callback_data=f"ans_{word_name}_{i}")]
                for i, opt in enumerate(quiz['options'])]

    await context.bot.send_message(
        chat_id=user_id,
        text=f"❓ Що означає {word_name.upper()}?\n\n🗣️ {word['transcription']}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    db.set_current_question(user_id, word_name, quiz['correct_index'], 'en_ua')

async def send_fill_blank(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    words = db.get_todays_words(user_id)
    if not words:
        return

    db.set_quiz_state(user_id, 'fill_blank', [w['word'] for w in words])
    await send_next_fill_blank(context, user_id)

async def send_next_fill_blank(context, user_id: int):
    state = db.get_quiz_state(user_id)
    if not state or not state.get('remaining'):
        await context.bot.send_message(chat_id=user_id, text="✅ Квіз завершено! 💪")
        return

    word_name = state['remaining'][0]
    word = db.get_word(user_id, word_name)
    if not word:
        return

    quiz = await generate_quiz(word, 'fill_blank', db.get_all_words(user_id))

    keyboard = [[InlineKeyboardButton(opt, callback_data=f"blank_{word_name}_{i}")]
                for i, opt in enumerate(quiz['options'])]

    await context.bot.send_message(
        chat_id=user_id,
        text=f"✍️ Заповни пропуск:\n\n\"{quiz['sentence']}\"",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    db.set_current_question(user_id, word_name, quiz['correct_index'], 'fill_blank')

async def send_quiz_ua_en(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    words = db.get_todays_words(user_id)
    if not words:
        return

    db.set_quiz_state(user_id, 'quiz_ua_en', [w['word'] for w in words])
    await context.bot.send_message(
        chat_id=user_id,
        text="💪 Тест UA→EN! Пиши слово англійською.\n\nПочинаємо:"
    )
    await send_next_quiz_ua_en(context, user_id)

async def send_next_quiz_ua_en(context, user_id: int):
    state = db.get_quiz_state(user_id)
    if not state or not state.get('remaining'):
        await context.bot.send_message(chat_id=user_id, text="✅ Квіз завершено! 💪")
        return

    word_name = state['remaining'][0]
    word = db.get_word(user_id, word_name)
    if not word:
        return

    hint = word_name[:3] + "..."
    await context.bot.send_message(
        chat_id=user_id,
        text=f"💪 Напиши англійською:\n\n🇺🇦 {word['translation'].upper()}\n🗣️ підказка: {hint}"
    )
    db.set_current_question(user_id, word_name, word_name, 'ua_en')

async def send_final_test(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    words = db.get_todays_words(user_id)
    if not words:
        return

    text = "🏆 Фінальний тест! Пиши відповіді через кому:\n\n"
    for i, w in enumerate(words, 1):
        text += f"{i}. {w['translation'].upper()} = ?\n"

    db.set_quiz_state(user_id, 'final', [w['word'] for w in words])
    await context.bot.send_message(chat_id=user_id, text=text)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data.startswith('ans_'):
        parts = data.split('_', 2)
        word_name = parts[1]
        chosen = int(parts[2])

        q = db.get_current_question(user_id)
        if not q:
            return

        correct = q['correct_index']
        if chosen == correct:
            db.update_repetition(user_id, word_name, True)
            await query.edit_message_text(f"✅ Правильно! {word_name.upper()} = {db.get_word(user_id, word_name)['translation']}")
        else:
            db.update_repetition(user_id, word_name, False)
            word = db.get_word(user_id, word_name)
            await query.edit_message_text(f"❌ Неправильно!\n{word_name.upper()} = {word['translation']}\n🗣️ {word['transcription']}")

        db.remove_from_remaining(user_id, word_name)
        await asyncio.sleep(1)
        await send_next_quiz_en_ua(context, user_id)

    elif data.startswith('blank_'):
        parts = data.split('_', 2)
        word_name = parts[1]
        chosen = int(parts[2])

        q = db.get_current_question(user_id)
        if not q:
            return

        correct = q['correct_index']
        if chosen == correct:
            db.update_repetition(user_id, word_name, True)
            await query.edit_message_text(f"✅ Правильно!")
        else:
            word = db.get_word(user_id, word_name)
            await query.edit_message_text(f"❌ Неправильно! Правильна відповідь: {word_name}")

        db.remove_from_remaining(user_id, word_name)
        await asyncio.sleep(1)
        await send_next_fill_blank(context, user_id)

    elif data.startswith('learned_'):
        word_name = data.replace('learned_', '')
        db.mark_learned(user_id, word_name)
        await query.edit_message_text(f"✅ {word_name.upper()} переміщено в архів!")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip().lower()

    q = db.get_current_question(user_id)
    if not q:
        return

    state = db.get_quiz_state(user_id)

    if state and state.get('type') == 'ua_en':
        word_name = q['correct_index']
        if text == word_name.lower():
            db.update_repetition(user_id, word_name, True)
            await update.message.reply_text(f"✅ Правильно! 🎉")
        else:
            db.update_repetition(user_id, word_name, False)
            word = db.get_word(user_id, word_name)
            await update.message.reply_text(
                f"❌ Неправильно!\n"
                f"Правильно: {word_name.upper()}\n"
                f"🗣️ {word['transcription']}"
            )
        db.remove_from_remaining(user_id, word_name)
        await send_next_quiz_ua_en(context, user_id)

    elif state and state.get('type') == 'final':
        words = state.get('remaining', [])
        answers = [a.strip().lower() for a in text.replace(',', ' ').split() if a.strip()]

        correct_count = 0
        result = "🏆 Результати фінального тесту:\n\n"
        for i, word_name in enumerate(words):
            word = db.get_word(user_id, word_name)
            user_ans = answers[i] if i < len(answers) else ''
            is_correct = user_ans == word_name.lower()
            if is_correct:
                correct_count += 1
                db.update_repetition(user_id, word_name, True)
                result += f"✅ {word_name.upper()}\n"
            else:
                db.update_repetition(user_id, word_name, False)
                result += f"❌ {word_name.upper()} (ти написав: {user_ans})\n"

        result += f"\nРезультат: {correct_count}/{len(words)} 🎯"

        keyboard = []
        for word_name in words:
            keyboard.append([InlineKeyboardButton(
                f"✅ Вивчив {word_name.upper()}",
                callback_data=f"learned_{word_name}"
            )])

        await update.message.reply_text(result, reply_markup=InlineKeyboardMarkup(keyboard))
        db.clear_quiz_state(user_id)

async def run_reminders(app: Application):
    while True:
        now = datetime.now(KYIV_TZ)
        users = db.get_all_users()

        for user in users:
            user_id = user['user_id']
            for hour, minute, reminder_type in REMINDER_TIMES:
                if now.hour == hour and now.minute == minute:
                    try:
                        if reminder_type == 'morning':
                            await send_morning_cards(app, user_id)
                        elif reminder_type == 'quiz_en_ua':
                            await send_quiz_en_ua(app, user_id)
                        elif reminder_type == 'fill_blank':
                            await send_fill_blank(app, user_id)
                        elif reminder_type == 'quiz_ua_en':
                            await send_quiz_ua_en(app, user_id)
                        elif reminder_type == 'final':
                            await send_final_test(app, user_id)
                    except Exception as e:
                        logger.error(f"Reminder error for {user_id}: {e}")

        await asyncio.sleep(60)

def main():
    import os
    token = os.environ.get('TELEGRAM_TOKEN')
    if not token:
        raise ValueError("TELEGRAM_TOKEN not set")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_words))
    app.add_handler(CommandHandler("words", show_words))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    loop = asyncio.get_event_loop()
    loop.create_task(run_reminders(app))

    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
