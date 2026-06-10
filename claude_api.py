import httpx
import json
import os
import random

CLAUDE_API_KEY = os.environ.get('CLAUDE_API_KEY')

async def get_word_info(word: str) -> dict:
    prompt = f"""Дай інформацію про англійське слово "{word}" у JSON форматі.

Відповідай ТІЛЬКИ JSON, без пояснень:
{{
  "transcription": "транскрипція ВЕЛИКИМИ буквами для наголосу, наприклад СТРАГ-ел",
  "translation": "переклад українською одним словом або коротко",
  "example1": "перше речення з цим словом (просте, побутове)",
  "example2": "друге речення з цим словом (інший контекст)"
}}

Правила транскрипції:
- ВЕЛИКІ букви = наголошений склад
- Використовуй українські букви для звуків
- Просто і зрозуміло, наприклад: ЕФ-орт, СТРАГ-ел, ан-СЕР-тен, студ, ЕМП-ті"""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                'https://api.anthropic.com/v1/messages',
                headers={
                    'x-api-key': CLAUDE_API_KEY,
                    'anthropic-version': '2023-06-01',
                    'content-type': 'application/json'
                },
                json={
                    'model': 'claude-haiku-4-5-20251001',
                    'max_tokens': 300,
                    'messages': [{'role': 'user', 'content': prompt}]
                }
            )
            data = response.json()
            text = data['content'][0]['text'].strip()
            text = text.replace('```json', '').replace('```', '').strip()
            return json.loads(text)
    except Exception as e:
        return {
            'transcription': word.upper(),
            'translation': '(не вдалось отримати)',
            'example1': f'I use {word} every day.',
            'example2': f'She learned the word {word}.'
        }

async def generate_quiz(word: dict, quiz_type: str, options_pool: list) -> dict:
    if quiz_type == 'en_ua':
        correct = word['translation']
        wrong_pool = [o for o in options_pool if o != correct]
        wrong = random.sample(wrong_pool, min(2, len(wrong_pool)))
        options = wrong + [correct]
        random.shuffle(options)
        return {
            'options': options,
            'correct_index': options.index(correct)
        }

    elif quiz_type == 'fill_blank':
        sentence = word['example1'].replace(word['word'], '___')
        correct = word['word']
        wrong_pool = [o for o in options_pool if o != correct]
        wrong = random.sample(wrong_pool, min(2, len(wrong_pool)))
        options = wrong + [correct]
        random.shuffle(options)
        return {
            'sentence': sentence,
            'options': options,
            'correct_index': options.index(correct)
        }

    return {}
