"""Optional Gemini calls (chapters, summary, translation); disabled without GEMINI_API_KEY."""

import json
import os
import re
import time
import urllib.error
import urllib.request

API = 'https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent'
RETRY_WAITS = (15, 30, 45)  # free tier allows ~10 requests/minute
# Allowlist: the API takes these codes only; names go into prompts. Keep in sync with static/index.html.
LANGUAGES = {'zh-TW': '繁體中文', 'zh-CN': '简体中文', 'en': 'English', 'ja': '日本語', 'ko': '한국어'}


def enabled():
    return bool(os.getenv('GEMINI_API_KEY'))


def generate(prompt, json_mode=False):
    config = {'responseMimeType': 'application/json'} if json_mode else {}
    body = json.dumps({'contents': [{'parts': [{'text': prompt}]}], 'generationConfig': config}).encode()
    request = urllib.request.Request(API.format(os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')), body,
                                     {'Content-Type': 'application/json',
                                      'x-goog-api-key': os.environ['GEMINI_API_KEY']})
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                data = json.load(response)
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < len(RETRY_WAITS):
                time.sleep(RETRY_WAITS[attempt])
                continue
            raise RuntimeError(f'Gemini HTTP {e.code}：{e.read().decode(errors="replace")[:200]}') from e
    if not data.get('candidates'):
        raise RuntimeError(f"Gemini 拒絕回應：{data.get('promptFeedback', {}).get('blockReason', '未知原因')}")
    candidate = data['candidates'][0]
    if candidate.get('finishReason') != 'STOP':
        raise RuntimeError(f"Gemini 回應不完整：{candidate.get('finishReason')}")
    return ''.join(part.get('text', '') for part in candidate.get('content', {}).get('parts', []))


def clean(text):
    # Models sometimes wrap the whole answer in a ```markdown fence.
    return re.sub(r'^```\w*\n|\n?```$', '', text.strip()).strip()


def seconds(value):
    # Models sometimes answer "1:30" instead of 90.
    total = 0.0
    for part in str(value).split(':'):
        total = total * 60 + float(part)
    return total


def make_chapters(cues, duration):
    lines = '\n'.join(f"[{int(cue['start'])}] {cue['text']}" for cue in cues)
    prompt = ('以下是影片逐字稿，每行開頭方括號內為秒數。請依主題切成 3 到 15 個章節，'
              '回傳 JSON 陣列 [{"start": 秒數, "title": "章節標題"}]，標題使用逐字稿的語言，第一章從 0 開始。\n\n'
              + lines)
    data = json.loads(generate(prompt, json_mode=True))
    if isinstance(data, dict):
        data = data.get('chapters', [])
    items = []
    for item in data:
        try:
            start, title = seconds(item['start']), str(item['title']).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if start >= 0 and title:
            items.append((start, title))
    items.sort()
    return [dict(start_time=start,
                 end_time=items[index + 1][0] if index + 1 < len(items) else float(duration),
                 title=title)
            for index, (start, title) in enumerate(items)]


def summarize(markdown, language):
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', markdown)  # screenshots carry no text for the model
    return clean(generate(f'請用{language}為以下影片筆記撰寫重點摘要 Markdown：開頭一段總覽，'
                          '接著每個章節（沿用原章節標題與觀看連結）列 3 到 5 個重點，最後列出關鍵術語。'
                          '只輸出 Markdown。\n\n' + text)) + '\n'


def translate(markdown, language):
    """Return (markdown, failed chapter count); a failed chapter keeps its original text."""
    # One request per chapter keeps long videos under the model's output token limit.
    prompt = (f'把以下 Markdown 翻譯成{language}。保留 Markdown 結構、圖片路徑、連結網址與時間戳不變，'
              '只輸出翻譯後的 Markdown。\n\n')
    parts, failed = [], 0
    for chunk in re.split(r'(?m)^(?=## )', markdown):
        if not chunk.strip():
            continue
        try:
            parts.append(clean(generate(prompt + chunk)))
        except Exception as e:
            failed += 1
            parts.append(f'> 本段翻譯失敗，保留原文（{str(e)[:120]}）\n\n{chunk.strip()}')
    return '\n\n'.join(parts) + '\n', failed
