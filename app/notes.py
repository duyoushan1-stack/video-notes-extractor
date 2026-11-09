"""Chapter-based Markdown rendering, independent of downloading and extraction."""

from typing import TypedDict
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class Cue(TypedDict):
    start: float
    text: str


class Frame(TypedDict):
    time: float
    file: str


class Chapter(TypedDict):
    start_time: float
    end_time: float
    title: str


def timestamp(seconds: float) -> str:
    seconds = int(seconds)
    return f'{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}'


def video_link(url: str, seconds: float) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != 't']
    query.append(('t', f'{int(seconds)}s'))
    return urlunsplit(parts._replace(query=urlencode(query)))


def paragraphs(cues: list[Cue]) -> list[str]:
    """Keep caption wording; break at pauses and bounded caption groups."""
    result: list[str] = []
    group: list[str] = []
    size = 0
    previous = None
    for cue in cues:
        text = cue['text'].strip()
        if not text:
            continue
        if group and (size + len(text) > 180 or
                      (previous is not None and cue['start'] - previous > 5)):
            result.extend([' '.join(group), ''])
            group, size = [], 0
        group.append(text)
        size += len(text) + 1
        previous = cue['start']
        if text.endswith(('。', '！', '？', '.', '!', '?')) and size >= 80:
            result.extend([' '.join(group), ''])
            group, size = [], 0
    if group:
        result.extend([' '.join(group), ''])
    return result


def render_notes(title: str, url: str, chapters: list[Chapter], frames: list[Frame],
                 cues: list[Cue], sample_step: int, original_count: int, *,
                 source: str = 'youtube', warnings: list[str] | None = None) -> str:
    chapters = sorted(chapters, key=lambda chapter: chapter['start_time'])
    frames = sorted(frames, key=lambda frame: frame['time'])
    cues = sorted(cues, key=lambda cue: cue['start'])
    sections = chapters or [Chapter(start_time=0, end_time=float('inf'), title='影片內容')]
    if sections[0]['start_time'] > 0:
        sections = [Chapter(start_time=0, end_time=sections[0]['start_time'], title='章節前內容'), *sections]
    # Uploaded names are text, never Markdown links or source paths.
    display_title = re.sub(r'([\\`*_{}\[\]()<>!#|])', r'\\\1', title) if source == 'mp4' else title
    lines = [f'# {display_title}', '', f'來源：{display_title if source == "mp4" else url}', '']
    lines += [f'> {warning}' for warning in (warnings or [])]
    lines += ['', '## 影片章節', '']
    for chapter in sections:
        label = chapter['title']
        if source == 'youtube':
            label = f"[{label}]({video_link(url, chapter['start_time'])})"
        lines.append(f"- {label} · {timestamp(chapter['start_time'])}")
    lines += ['', f'影格取樣：每 {sample_step} 秒；通過篩選 {original_count} 張，輸出 {len(frames)} 張。', '',
              '逐字稿資料：[transcript.json](transcript.json)', '']
    for index, chapter in enumerate(sections):
        start = chapter['start_time']
        end = sections[index + 1]['start_time'] if index + 1 < len(sections) else float('inf')
        watch = (f"[從 {timestamp(start)} 開始觀看]({video_link(url, start)})" if source == 'youtube'
                 else f'時間：{timestamp(start)}')
        lines += [f"## {chapter['title']}", '', watch, '']
        section_frames = [frame for frame in frames if start <= frame['time'] < end]
        cursor = start
        for frame_index, frame in enumerate(section_frames):
            if frame_index == 0:
                lines += paragraphs([cue for cue in cues if cursor <= cue['start'] < frame['time']])
            stop = section_frames[frame_index + 1]['time'] if frame_index + 1 < len(section_frames) else end
            label = timestamp(frame['time'])
            lines += [f"### 畫面 · {label}", '', f"![{chapter['title']} · {label}]({frame['file']})", '']
            lines += paragraphs([cue for cue in cues if frame['time'] <= cue['start'] < stop])
            cursor = stop
        if not section_frames:
            lines += paragraphs([cue for cue in cues if start <= cue['start'] < end])
    if not cues:
        lines += ['沒有可用逐字稿；此版本尚未對 MP4 執行語音轉錄。' if source == 'mp4'
                  else '無可用字幕；未執行語音轉錄。', '']
    return '\n'.join(lines).rstrip() + '\n'
