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
- Англійські слова рівня B2-C1, НЕ примітивні
- Близькі за темою або частиною мови до "{word}" — щоб було важко вгадати
- КРИТИЧНО: жодна uk-пара НЕ може бути синонімом або взаємозамінною з перекладом "{word}".
  Приклад ПОМИЛКИ для bliss (блаженство): "rapture" -> "захоплення", "reverie" -> "задума" —
  це частково синоніми блаженства, і всі вони звучали б "правильно" у вправі — НЕ РОБИ ТАК.
  Замість цього бери слова з ІНШОЇ емоції/семантичної категорії: bliss -> заздрість, сумнів, втома.
- uk переклад: деякі 1 слово, деякі 2-3 слова — ЗМІШАНА довжина щоб правильна відповідь не виділялась
- Жодне en не дорівнює "{word}"
- Всі 8 унікальні"""

    try:
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
                    'max_tokens': 700,
                    'messages': [{'role': 'user', 'content': prompt}],
                },
            )
            data = resp.json()
            text = data['content'][0]['text'].strip()
            text = text.replace('```json', '').replace('```', '').strip()
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

            return {
                'word': word.strip().lower(),
                'transcription': info.get('transcription', word.upper()),
                'translation': info.get('translation', '—'),
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
- РІВНО 8 пар, рівня {cefr_level}, реально вживані (не примітивні, не технічні)
- Близькі за темою/частиною мови до слова — щоб було важко вгадати
- КРИТИЧНО: жодна uk-пара НЕ може бути синонімом або взаємозамінною з перекладом слова.
  Приклад ПОМИЛКИ для bliss (блаженство): "rapture" -> "захоплення", "reverie" -> "задума" —
  це синоніми блаженства, всі звучали б "правильно" — НЕ РОБИ ТАК.
  Інший приклад: persistent (наполегливий) -> НЕ "впертий", НЕ "послідовний" (синоніми).
  Замість цього бери слова з ІНШОЇ семантичної категорії, не пов'+"'"+'язаної зі значенням слова.
- uk: ЗМІШАНА довжина (деякі 1 слово, деякі 2-3) щоб правильна відповідь не виділялась
- Всі унікальні, жодне не дорівнює основному слову"""

    try:
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
                    'max_tokens': 800,
                    'messages': [{'role': 'user', 'content': prompt}],
                },
            )
            data = resp.json()
            text = data['content'][0]['text'].strip()
            text = text.replace('```json', '').replace('```', '').strip()
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

            return {
                'word': word,
                'transcription': info.get('transcription', word.upper()),
                'translation': info.get('translation', '—'),
                'example1': info.get('example1', f'I use the word {word} often.'),
                'example2': info.get('example2', f'She learned the word {word}.'),
                'distractors_en': [d['en'] for d in clean[:8]],
                'distractors_uk': [d['uk'] for d in clean[:8]],
            }
    except Exception:
        return None
