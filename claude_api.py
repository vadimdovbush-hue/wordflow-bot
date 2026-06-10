import os
import json
import httpx

CLAUDE_API_KEY = os.environ.get('CLAUDE_API_KEY')
MODEL = 'claude-haiku-4-5-20251001'


async def get_word_info(word: str) -> dict:
    prompt = f"""Дай інформацію про англійське слово "{word}" у форматі JSON.

Відповідай ВИКЛЮЧНО валідним JSON без пояснень і без markdown:
{{
  "transcription": "транскрипція українськими буквами, наголошений склад ВЕЛИКИМИ, напр. СТРАГ-ел",
  "translation": "короткий переклад українською (1-3 слова)",
  "example1": "просте побутове речення англійською з цим словом",
  "example2": "інше речення англійською з цим словом, інший контекст"
}}

Правила транскрипції: тільки українські букви, наголошений склад ВЕЛИКИМИ.
Приклади: effort -> ЕФ-орт, uncertain -> ан-СЕР-тен, stood -> студ, empty -> ЕМП-ті."""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                'https://api.anthropic.com/v1/messages',
                headers={
                    'x-api-key': CLAUDE_API_KEY,
                    'anthropic-version': '2023-06-01',
                    'content-type': 'application/json',
                },
                json={
                    'model': MODEL,
                    'max_tokens': 350,
                    'messages': [{'role': 'user', 'content': prompt}],
                },
            )
            data = resp.json()
            text = data['content'][0]['text'].strip()
            text = text.replace('```json', '').replace('```', '').strip()
            info = json.loads(text)
            return {
                'word': word.strip().lower(),
                'transcription': info.get('transcription', word.upper()),
                'translation': info.get('translation', '—'),
                'example1': info.get('example1', f'I use the word {word} often.'),
                'example2': info.get('example2', f'She learned the word {word}.'),
            }
    except Exception:
        return {
            'word': word.strip().lower(),
            'transcription': word.upper(),
            'translation': '(не вдалось отримати)',
            'example1': f'I use the word {word} often.',
            'example2': f'She learned the word {word}.',
        }
