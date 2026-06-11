import os
import json
import httpx

CLAUDE_API_KEY = os.environ.get('CLAUDE_API_KEY')
MODEL = 'claude-haiku-4-5-20251001'

# Запасний пул дистракторів (B2-C1), якщо AI недоступний
FALLBACK_DISTRACTORS = [
    {'en': 'reluctant', 'uk': 'неохочий'},
    {'en': 'thorough', 'uk': 'ретельний'},
    {'en': 'vague', 'uk': 'нечіткий'},
    {'en': 'subtle', 'uk': 'тонкий'},
    {'en': 'eager', 'uk': 'завзятий'},
    {'en': 'awkward', 'uk': 'незграбний'},
    {'en': 'genuine', 'uk': 'щирий'},
    {'en': 'steady', 'uk': 'стійкий'},
    {'en': 'bold', 'uk': 'сміливий'},
    {'en': 'fragile', 'uk': 'крихкий'},
    {'en': 'clumsy', 'uk': 'неуклюжий'},
    {'en': 'shallow', 'uk': 'поверхневий'},
]


async def get_word_info(word: str) -> dict:
    prompt = f"""Дай інформацію про англійське слово "{word}" у форматі JSON.

Відповідай ВИКЛЮЧНО валідним JSON без пояснень і без markdown:
{{
  "transcription": "транскрипція українськими буквами, наголошений склад ВЕЛИКИМИ, напр. СТРАГ-ел",
  "translation": "короткий переклад українською (1-3 слова)",
  "example1": "просте побутове речення англійською з цим словом",
  "example2": "інше речення англійською з цим словом, інший контекст",
  "distractors": [
    {{"en": "англ. слово-обманка рівня B2-C1", "uk": "його переклад українською"}},
    ... РІВНО 8 таких пар ...
  ]
}}

Правила транскрипції: тільки українські букви, наголошений склад ВЕЛИКИМИ.
Приклади: effort -> ЕФ-орт, uncertain -> ан-СЕР-тен, stood -> студ, empty -> ЕМП-ті.

Правила distractors (варіанти-обманки для тесту з вибором):
- РІВНО 8 пар.
- Англійські слова рівня B2-C1, НЕ примітивні (заборонено hello, home, people, good, big тощо).
- Жодне не повинно дорівнювати слову "{word}" чи бути його синонімом.
- Бажано та сама частина мови, що й "{word}", щоб обманки були правдоподібні.
- Кожне unique. uk — це коректний переклад відповідного en українською."""

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
import os
import json
import httpx

CLAUDE_API_KEY = os.environ.get('CLAUDE_API_KEY')
MODEL = 'claude-haiku-4-5-20251001'

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

Правила transcription:
- Тільки українські букви і дефіси
- Правильний поділ на склади
- Наголошений склад ВЕЛИКИМИ літерами

Правила distractors (варіанти-обманки для тесту):
- РІВНО 8 пар
- Англійські слова рівня B2-C1, НЕ примітивні
- Близькі за темою або частиною мови до "{word}" — щоб було важко вгадати
- НЕ синоніми до "{word}"
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
