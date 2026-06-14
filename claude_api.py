import os
import json
import httpx

CLAUDE_API_KEY = os.environ.get('CLAUDE_API_KEY')
MODEL = 'claude-sonnet-4-6'

FALLBACK_DISTRACTORS = [
    {'en': 'reluctant', 'uk': 'неохочий'},
    {'en': 'thorough', 'uk': 'ретельний'},
    {'en': 'vague', 'uk': 'нечіткий, розмитий'},
    {'en': 'subtle', 'uk': 'тонкий'},
    {'en': 'eager', 'uk': 'завзятий, наполегливий'},
    {'en': 'awkward', 'uk': 'незграбний'},
    {'en': 'genuine', 'uk': 'щирий'},
    {'en': 'steady', 'uk': 'стійкий, рівномірний'},
    {'en': 'bold', 'uk': 'сміливий'},
    {'en': 'fragile', 'uk': 'крихкий, вразливий'},
    {'en': 'clumsy', 'uk': 'неуклюжий'},
    {'en': 'shallow', 'uk': 'поверхневий'},
]


async def _call_claude(prompt: str, max_tokens: int) -> str:
    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': CLAUDE_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
            json={
                'model': MODEL,
                'max_tokens': max_tokens,
                'messages': [{'role': 'user', 'content': prompt}],
            },
        )
        data = resp.json()
        text = data['content'][0]['text'].strip()
        return text.replace('```json', '').replace('```', '').strip()


async def _validate_distractors(word: str, translation: str, distractors: list) -> list:
    """Друга перевірка: чи якийсь дистрактор близький за значенням до правильного перекладу
    (синонім АБО та сама вузька тема). Повертає список булевих флагів is_bad (True = замінити)."""
    if not distractors:
        return []

    items = '\n'.join(
        f'{i+1}. "{d["en"]}" -> "{d["uk"]}"' for i, d in enumerate(distractors)
    )

    prompt = f"""Слово: "{word}", правильний переклад: "{translation}".

Ось 8 варіантів-обманок (дистракторів) для тесту з перекладом:
{items}

Завдання: перевір КОЖЕН варіант (і його англійське слово, і український переклад).
Признач true, якщо учень міг би РОЗУМНО вважати цей варіант прийнятною відповіддю замість
"{translation}" — тобто якщо він:
  (а) синонім або взаємозамінний переклад "{translation}", АБО
  (б) з тієї ж вузької теми/семантичного поля, що й "{translation}" (легко сплутати).
Приклад: для "відходи" — "уламки", "сміття", "утилізація", "забруднення", "витік" усі
з теми сміття/відходів → true (ПОГАНО). А "впертий", "захват", "мілкий" → false (ДОБРЕ).

Відповідай ВИКЛЮЧНО JSON-масивом з 8 булевих значень, без пояснень:
[true/false, ... 8 елементів]
true = близький за значенням/темою (ПОГАНО, треба замінити)
false = чітко інша тема, не сплутати (ДОБРЕ, лишити)"""

    try:
        text = await _call_claude(prompt, 200)
        flags = json.loads(text)
        if isinstance(flags, list) and len(flags) == len(distractors):
            return [bool(f) for f in flags]
    except Exception:
        pass
    return [False] * len(distractors)



def _fill_replacements(clean: list, seen: set, word: str, bad_flags: list) -> list:
    """Замінює дистрактори, позначені як 'bad', на запасні з FALLBACK_DISTRACTORS."""
    result = []
    fb_iter = iter(d for d in FALLBACK_DISTRACTORS if d['en'] not in seen and d['en'] != word)
    for d, is_bad in zip(clean, bad_flags + [False] * (len(clean) - len(bad_flags))):
        if is_bad:
            try:
                fb = next(fb_iter)
                seen.add(fb['en'])
                result.append(fb)
                continue
            except StopIteration:
                pass
        result.append(d)
    return result


async def regenerate_distractors(word: str, translation: str) -> dict:
    """Генерує цілком новий набір з 8 дистракторів з ІНШИХ тем (для виправлення старих слів).
    Повертає {'distractors_en': [...8], 'distractors_uk': [...8]} або None."""
    prompt = f"""Згенеруй РІВНО 8 варіантів-обманок (дистракторів) для тесту зі словом.
Слово: "{word}", правильний переклад: "{translation}".

КРИТИЧНО: дистрактори мають бути з ЯВНО ІНШИМ значенням, НЕ з тієї ж теми, що й "{word}".
Тест працює в два боки, тому НІ англійське слово, НІ його укр-переклад не можуть бути
близькими, синонімами, або з тієї ж вузької теми, що "{word}"/"{translation}".
Приклад ПОМИЛКИ для "відходи": debris(уламки), disposal(утилізація), spillage(витік) —
з теми сміття — ЗАБОРОНЕНО. Бери натомість слова з не пов'язаних тем
(напр. для відходи: stubborn(впертий), delight(захват), shallow(мілкий), gentle(лагідний)).

Правила:
- РІВНО 8 пар, англійські слова рівня B2-C1, реально вживані, правдоподібні
- Кожне з ІНШОЇ, не пов'язаної з "{word}" теми
- uk переклад: ЗМІШАНА довжина (деякі 1 слово, деякі 2-3)
- Жодне en не дорівнює "{word}", жодна uk не дорівнює "{translation}"
- Всі 8 унікальні

Відповідай ВИКЛЮЧНО валідним JSON без markdown:
{{"distractors": [{{"en": "слово", "uk": "переклад"}}, ... РІВНО 8 ...]}}"""

    try:
        text = await _call_claude(prompt, 500)
        info = json.loads(text)
        distractors = info.get('distractors', []) or []
        clean = []
        seen = {word.strip().lower(), (translation or '').strip().lower()}
        for d in distractors:
            if not isinstance(d, dict):
                continue
            en = str(d.get('en', '')).strip().lower()
            uk = str(d.get('uk', '')).strip()
            if not en or not uk or en in seen:
                continue
            seen.add(en)
            clean.append({'en': en, 'uk': uk})
        if len(clean) < 8:
            for fb in FALLBACK_DISTRACTORS:
                if fb['en'] not in seen:
                    clean.append(fb)
                    seen.add(fb['en'])
                if len(clean) >= 8:
                    break
        clean = clean[:8]

        # перевіряємо новий набір; що лишилось погане — добиваємо запасними
        bad = await _validate_distractors(word, translation, clean)
        if any(bad):
            clean = _fill_replacements(clean, seen, word.strip().lower(), bad)

        return {
            'distractors_en': [d['en'] for d in clean[:8]],
            'distractors_uk': [d['uk'] for d in clean[:8]],
        }
    except Exception:
        return None


async def get_word_info(word: str) -> dict:
    prompt = f"""Дай інформацію про англійське слово "{word}" у форматі JSON.

Відповідай ВИКЛЮЧНО валідним JSON без пояснень і без markdown:
{{
  "transcription": "транскрипція українськими буквами, правильні склади, наголошений склад ВЕЛИКИМИ. Приклади: struggle→СТРА-гл, effort→ЕФ-орт, uncertain→ан-СЕР-тн, empty→ЕМП-ті, fulfilling→фул-ФІЛ-інг, believe→бі-ЛІВ, comfortable→КОМ-фор-тбл",
  "translation": "точний переклад українською, МАКСИМУМ 2 слова, найуживаніший варіант",
  "example1": "просте побутове речення англійською з цим словом",
  "example2": "інше речення англійською з цим словом, інший контекст",
  "distractors": [
    {{"en": "англ. слово-обманка рівня B2-C1", "uk": "переклад 1-3 слова"}},
    ... РІВНО 8 таких пар ...
  ]
}}

Правила translation:
- МАКСИМУМ 2 слова українською
- Найточніший і найуживаніший переклад
- НЕ описовий (не "той що робить X"), а пряме слово
- КРИТИЧНО: переклад має точно відповідати ОСНОВНОМУ значенню слова "{word}",
  а не близькому синоніму чи асоціації. Наприклад: committed -> "відданий" (НЕ "привілейований"),
  intent -> "намір" (НЕ "мета", НЕ "привід"). Перевір себе: якщо перекласти твій варіант
  назад на англійську, чи вийде саме "{word}" чи його точний синонім?

Правила transcription:
- Тільки українські букви і дефіси
- Правильний поділ на склади
- Наголошений склад ВЕЛИКИМИ літерами

Правила distractors (варіанти-обманки для тесту):
- РІВНО 8 пар
- Англійські слова рівня B2-C1, НЕ примітивні, правдоподібні на вигляд
- КРИТИЧНО ВАЖЛИВО — дистрактори мають бути з ЯВНО ІНШИМ значенням, НЕ з тієї ж теми:
  Тест працює в ДВА боки (показуємо англ. слово -> обирають укр. переклад, АБО навпаки).
  Тому НІ англійське слово, НІ його укр-переклад не можуть бути близькими до "{word}".
  ЗАБОРОНЕНО: синоніми, взаємозамінні переклади, АБО слова з тієї ж вузької теми/категорії,
  які учень міг би вважати прийнятною відповіддю.
  Приклад ПОМИЛКИ для waste (відходи): "debris" (уламки), "disposal" (утилізація),
  "spillage" (витік), "contamination" (забруднення) — усі з теми сміття/відходів,
  їх легко сплутати з "відходи" — НЕ РОБИ ТАК.
  Приклад ПОМИЛКИ для bliss (блаженство): "rapture" -> "захоплення", "reverie" -> "задума" —
  частково синоніми блаженства — НЕ РОБИ ТАК.
  ПРАВИЛЬНО: бери слова з ЦІЛКОМ ІНШИХ, не пов'язаних тем. Для waste -> напр. "stubborn"(впертий),
  "delight"(захват), "shallow"(мілкий). Для bliss -> заздрість, сумнів, втома.
- Перевір себе: для КОЖНОГО дистрактора — чи міг би учень сплутати його зі словом "{word}"
  у будь-якому з двох напрямків? Якщо так — заміни на слово з іншої теми.
- uk переклад: деякі 1 слово, деякі 2-3 слова — ЗМІШАНА довжина щоб правильна відповідь не виділялась
- Жодне en не дорівнює "{word}", жодна uk-пара не дорівнює перекладу "{word}"
- Всі 8 унікальні"""

    try:
        text = await _call_claude(prompt, 700)
        info = json.loads(text)

        distractors = info.get('distractors', []) or []
        clean = []
        seen = set()
        for d in distractors:
            if not isinstance(d, dict):
                continue
            en = str(d.get('en', '')).strip().lower()
            uk = str(d.get('uk', '')).strip()
            if not en or not uk:
                continue
            if en == word.strip().lower() or en in seen:
                continue
            seen.add(en)
            clean.append({'en': en, 'uk': uk})
        if len(clean) < 8:
            for fb in FALLBACK_DISTRACTORS:
                if fb['en'] not in seen and fb['en'] != word.strip().lower():
                    clean.append(fb)
                    seen.add(fb['en'])
                if len(clean) >= 8:
                    break

        clean = clean[:8]
        translation = info.get('translation', '—')

        # Другий прохід: перевірка дистракторів на синонімічність з перекладом
        bad_flags = await _validate_distractors(word, translation, clean)
        if any(bad_flags):
            clean = _fill_replacements(clean, seen, word.strip().lower(), bad_flags)

        return {
            'word': word.strip().lower(),
            'transcription': info.get('transcription', word.upper()),
            'translation': translation,
            'example1': info.get('example1', f'I use the word {word} often.'),
            'example2': info.get('example2', f'She learned the word {word}.'),
            'distractors_en': [d['en'] for d in clean[:8]],
            'distractors_uk': [d['uk'] for d in clean[:8]],
        }
    except Exception:
        return {
            'word': word.strip().lower(),
            'transcription': word.upper(),
            'translation': '(не вдалось отримати)',
            'example1': f'I use the word {word} often.',
            'example2': f'She learned the word {word}.',
            'distractors_en': [d['en'] for d in FALLBACK_DISTRACTORS[:8]],
            'distractors_uk': [d['uk'] for d in FALLBACK_DISTRACTORS[:8]],
        }


CEFR_HINT = {
    'A1': 'найпростіші повсякденні слова (їжа, дім, родина, базові дії)',
    'A2': 'прості побутові слова (подорожі, покупки, погода, розпорядок дня)',
    'B1': 'слова середнього рівня для роботи, навчання, почуттів, думок',
    'B2': 'слова вище середнього: абстрактні поняття, емоції, опис характеру, аргументація',
    'C1': 'просунуті слова: нюанси значень, формальна та ділова лексика, ідіоматичні вирази',
    'C2': 'найскладніші слова рівня носія: рідковживані, літературні, тонкі відтінки значень',
}


async def generate_cefr_word(cefr_level: str, exclude_words: list) -> dict:
    """Генерує ОДНЕ реальне вживане англійське слово рівня CEFR + повну інформацію."""
    exclude_str = ', '.join(exclude_words[:40]) if exclude_words else '(список порожній)'
    hint = CEFR_HINT.get(cefr_level, '')

    prompt = f"""Згенеруй ОДНЕ англійське слово рівня CEFR {cefr_level} та інформацію про нього у форматі JSON.

Рівень {cefr_level}: {hint}

КРИТИЧНО ВАЖЛИВІ правила вибору слова:
- Слово має РЕАЛЬНО ВЖИВАТИСЬ у живій англійській (розмова, листування, робота, медіа)
- НЕ технічний термін, НЕ абревіатура, НЕ власна назва, НЕ застаріле слово
- Корисне для повсякденного спілкування, роботи, подорожей, опису емоцій та думок
- Відповідає саме рівню {cefr_level} за критеріями Cambridge/Oxford
- НЕ використовуй жодне з цих слів (вже вивчені): {exclude_str}

Відповідай ВИКЛЮЧНО валідним JSON без пояснень і без markdown:
{{
  "word": "саме слово англійською (нижній регістр)",
  "transcription": "транскрипція українськими буквами, наголошений склад ВЕЛИКИМИ. Приклади: struggle→СТРА-гл, effort→ЕФ-орт, achieve→е-ЧІВ",
  "translation": "точний переклад українською, МАКСИМУМ 2 слова",
  "example1": "просте побутове речення англійською з цим словом",
  "example2": "інше речення англійською, інший контекст",
  "distractors": [
    {{"en": "англ. слово-обманка рівня {cefr_level}", "uk": "переклад 1-3 слова"}},
    ... РІВНО 8 пар ...
  ]
}}

Правила translation:
- КРИТИЧНО: переклад має точно відповідати ОСНОВНОМУ значенню слова,
  а не близькому синоніму чи асоціації. Наприклад: committed -> "відданий" (НЕ "привілейований"),
  intent -> "намір" (НЕ "мета", НЕ "привід"). Перевір себе: якщо перекласти твій варіант
  назад на англійську, чи вийде саме це слово чи його точний синонім?

Правила distractors:
- РІВНО 8 пар, рівня {cefr_level}, реально вживані (не примітивні, не технічні), правдоподібні
- КРИТИЧНО ВАЖЛИВО — дистрактори з ЯВНО ІНШИМ значенням, НЕ з тієї ж теми:
  Тест працює в ДВА боки (англ. слово -> укр. переклад, АБО навпаки), тому
  НІ англійське слово, НІ його укр-переклад не можуть бути близькими до правильного слова.
  ЗАБОРОНЕНО: синоніми, взаємозамінні переклади, АБО слова з тієї ж вузької теми/категорії,
  які учень міг би вважати прийнятною відповіддю.
  Приклад ПОМИЛКИ для waste (відходи): "debris"(уламки), "disposal"(утилізація),
  "spillage"(витік), "contamination"(забруднення) — усі з теми сміття — НЕ РОБИ ТАК.
  Приклад ПОМИЛКИ для bliss: "rapture"->"захоплення" (синонім) — НЕ РОБИ ТАК.
  Приклад: persistent (наполегливий) -> НЕ "впертий", НЕ "послідовний" (синоніми).
  ПРАВИЛЬНО: бери слова з ЦІЛКОМ ІНШИХ, не пов'язаних тем.
- Перевір себе: для КОЖНОГО дистрактора — чи міг би учень сплутати його з правильним словом
  у будь-якому з двох напрямків? Якщо так — заміни на слово з іншої теми.
- uk: ЗМІШАНА довжина (деякі 1 слово, деякі 2-3) щоб правильна відповідь не виділялась
- Всі унікальні, жодне не дорівнює основному слову чи його перекладу"""

    try:
        text = await _call_claude(prompt, 800)
        info = json.loads(text)

        word = str(info.get('word', '')).strip().lower()
        if not word or ' ' in word:
            return None

        distractors = info.get('distractors', []) or []
        clean = []
        seen = set()
        for d in distractors:
            if not isinstance(d, dict):
                continue
            en = str(d.get('en', '')).strip().lower()
            uk = str(d.get('uk', '')).strip()
            if not en or not uk or en == word or en in seen:
                continue
            seen.add(en)
            clean.append({'en': en, 'uk': uk})
        if len(clean) < 8:
            for fb in FALLBACK_DISTRACTORS:
                if fb['en'] not in seen and fb['en'] != word:
                    clean.append(fb)
                    seen.add(fb['en'])
                if len(clean) >= 8:
                    break

        clean = clean[:8]
        translation = info.get('translation', '—')

        # Другий прохід: перевірка дистракторів на синонімічність з перекладом
        bad_flags = await _validate_distractors(word, translation, clean)
        if any(bad_flags):
            clean = _fill_replacements(clean, seen, word, bad_flags)

        return {
            'word': word,
            'transcription': info.get('transcription', word.upper()),
            'translation': translation,
            'example1': info.get('example1', f'I use the word {word} often.'),
            'example2': info.get('example2', f'She learned the word {word}.'),
            'distractors_en': [d['en'] for d in clean[:8]],
            'distractors_uk': [d['uk'] for d in clean[:8]],
        }
    except Exception:
        return None
