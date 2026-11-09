import json
import os
import re
import shutil
import subprocess
import threading
import uuid
import zipfile
from PIL import Image, ImageChops, ImageFilter, ImageStat
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from app import gemini
from app.notes import render_notes

ROOT = Path(os.getenv('DATA_DIR', '/srv/data')).resolve()
ROOT.mkdir(parents=True, exist_ok=True)
app = FastAPI(title='Video Notes Extractor')
app.mount('/static', StaticFiles(directory='static'), name='static')
# Single-process MVP state; use persistent queue and auth before public deployment.
JOBS = {}
LOCK = threading.Lock()

class JobRequest(BaseModel):
    url: str
    threshold: float = 0.32
    min_gap: int = 4
    max_frames: int = 120
    summary: bool = False
    summary_language: str = 'zh-TW'
    translate: str = ''


def video_id(url):
    p = urlparse(url)
    host = (p.hostname or '').lower()
    if host in ('youtube.com', 'www.youtube.com', 'm.youtube.com') and p.path == '/watch':
        v = parse_qs(p.query).get('v', [''])[0]
    elif host == 'youtu.be':
        v = p.path.strip('/')
    else:
        raise ValueError('僅支援 YouTube 影片網址')
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', v):
        raise ValueError('無效的 YouTube 影片 ID')
    return v


def run(args, cwd, timeout=3600):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=True)


def timestamp(sec):
    sec = int(float(sec))
    return f'{sec//3600:02}:{sec//60%60:02}:{sec%60:02}'


def parse_vtt(path):
    text = path.read_text(encoding='utf-8-sig', errors='replace')
    entries = []
    for block in re.split(r'\n\s*\n', text):
        match = re.search(r'(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3})\s+-->\s+', block)
        if not match:
            continue
        start = int(match.group(1) or 0)*3600 + int(match.group(2))*60 + int(match.group(3)) + int(match.group(4))/1000
        body = '\n'.join(block[match.end():].splitlines()[1:])
        lines = [re.sub(r'<[^>]+>', '', x).strip() for x in body.splitlines() if x.strip()]
        value = ' '.join(lines)
        if value and (not entries or entries[-1]['text'] != value):
            entries.append({'start': start, 'text': value})
    return entries


def update(jid, **changes):
    with LOCK:
        JOBS[jid].update(changes)


def process(jid, url, threshold, min_gap, max_frames, summary, summary_language, translate):
    folder = ROOT / jid
    folder.mkdir(exist_ok=True)
    shots = folder / 'screenshots'
    shots.mkdir(exist_ok=True)
    try:
        update(jid, status='downloading', progress=10)
        run(['yt-dlp','--no-playlist','--max-filesize','1500M','-f','bv*[height<=720]+ba/b[height<=720]/b',
             '--write-info-json','--write-subs','--write-auto-subs',
             '--sub-langs','zh-TW,zh-Hant,zh.*,en.*','--sub-format','vtt',
             '--ignore-errors','--retries','5','--sleep-subtitles','3',
             '-o','video.%(ext)s',url],folder,5400)
        info_files = list(folder.glob('*.info.json'))
        if not info_files:
            raise RuntimeError('無法取得影片資訊')
        info = json.loads(info_files[0].read_text(encoding='utf-8'))
        videos = [p for p in folder.glob('video.*') if p.suffix.lower() in ('.mp4','.webm','.mkv')]
        if not videos:
            raise RuntimeError('無法取得影片檔案')
        video = videos[0]
        subs = list(folder.glob('*.vtt'))
        # Prefer manually authored subtitles, then automatic captions; language priority from download list.
        subs.sort(key=lambda p: ('.zh-' not in p.name.lower(), '.en' in p.name.lower(), len(p.name)))
        cues = parse_vtt(subs[0]) if subs else []
        update(jid, status='analyzing', progress=50)
        # Sample the entire timeline rather than relying on FFmpeg's high scene threshold.
        # The sample index maps to source time (2s cadence, with the first at ~0s).
        samples = folder / 'samples'
        samples.mkdir(exist_ok=True)
        sample_step = 2
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
               '-i', str(video), '-vf', f'fps=1/{sample_step},scale=960:-2',
               '-q:v', '3', str(samples / 'sample-%06d.jpg')]
        result = subprocess.run(cmd, cwd=folder, capture_output=True, text=True, timeout=5400)
        if result.returncode:
            raise RuntimeError('FFmpeg 影格取樣失敗：' + result.stderr[-500:])
        sampled = sorted(samples.glob('sample-*.jpg'))
        if not sampled:
            raise RuntimeError('未取得任何影格')

        def features(path):
            with Image.open(path) as source:
                rgb = source.convert('RGB')
                small = rgb.resize((160, 90))
                gray = small.convert('L')
                stats = ImageStat.Stat(gray)
                # Simple focus proxy: high-frequency edge variance.
                edges = gray.filter(ImageFilter.FIND_EDGES)
                focus = ImageStat.Stat(edges).var[0]
                return small, stats.mean[0], focus

        def difference(a, b):
            return ImageStat.Stat(ImageChops.difference(a, b)).mean[0] / 255.0

        # Drop dark/faded frames, then keep distinct stable content. Compare against
        # the last accepted frame, not just the immediately previous sample.
        candidates = []
        previous = None
        for index, path in enumerate(sampled):
            im, brightness, focus = features(path)
            if brightness < 23 or focus < 9:
                continue
            time_sec = index * sample_step
            # Skip transitional frames when adjacent samples are dramatically different.
            # Do not blindly drop unique short slides: this is a soft quality filter.
            if previous is None or difference(im, previous[0]) >= max(0.025, threshold * 0.15):
                candidates.append((time_sec, path, im, focus))
                previous = (im, time_sec)

        selected = []
        for t, path, im, focus in candidates:
            if selected and t - selected[-1][0] < min_gap:
                # Keep the sharper frame in a short burst.
                if focus > selected[-1][3] * 1.25:
                    selected[-1] = (t, path, im, focus)
                continue
            selected.append((t, path, im, focus))

        # If there are too many frames, spread selections across the WHOLE video,
        # never silently truncate the final chapters.
        original_count = len(selected)
        if len(selected) > max_frames:
            positions = [round(i * (len(selected)-1) / (max_frames-1))
                         for i in range(max_frames)] if max_frames > 1 else [0]
            selected = [selected[i] for i in positions]
        frames = []
        for t, path, _, _ in selected:
            target = shots / f'frame-{len(frames)+1:04}.jpg'
            shutil.copy2(path, target)
            frames.append({'time': round(t, 2), 'file': f'screenshots/{target.name}'})
        shutil.rmtree(samples)
        update(jid, candidate_frames=original_count, sampled_frames=len(sampled))
        chapters = info.get('chapters') or []
        warnings = []
        if not chapters and cues and gemini.enabled():
            update(jid, status='chaptering', progress=75)
            try:
                chapters = gemini.make_chapters(cues, info.get('duration') or cues[-1]['start'])
            except Exception as e:
                warnings.append(f'AI 分章失敗，改用單一章節：{e}'[:300])
        markdown = render_notes(info.get('title', '影片筆記'), url, chapters, frames, cues,
                                sample_step, original_count)
        (folder/'README.md').write_text(markdown, encoding='utf-8')
        (folder/'transcript.json').write_text(json.dumps(cues,ensure_ascii=False,indent=2),encoding='utf-8')
        (folder/'chapters.json').write_text(json.dumps(chapters,ensure_ascii=False,indent=2),encoding='utf-8')
        (folder/'frames.json').write_text(json.dumps(frames,ensure_ascii=False,indent=2),encoding='utf-8')
        extras = []
        if summary or translate:
            update(jid, status='ai', progress=85)
        # AI outputs are optional extras: a failure here must not lose the base notes.
        if summary:
            try:
                (folder/'summary.md').write_text(gemini.summarize(markdown, gemini.LANGUAGES[summary_language]), encoding='utf-8')
                extras.append('summary.md')
            except Exception as e:
                warnings.append(f'AI 摘要失敗：{e}'[:300])
        if translate:
            translated, failed = gemini.translate(markdown, gemini.LANGUAGES[translate])
            # Safe as a filename: translate is an allowlisted code.
            (folder/f'README.{translate}.md').write_text(translated, encoding='utf-8')
            extras.append(f'README.{translate}.md')
            if failed:
                warnings.append(f'有 {failed} 段翻譯失敗，已保留原文並標註')
        with zipfile.ZipFile(folder/'result.zip','w',zipfile.ZIP_DEFLATED) as z:
            for name in ['README.md','transcript.json','chapters.json','frames.json', *extras]:
                z.write(folder/name,name)
            for p in shots.glob('*.jpg'):
                z.write(p,p.relative_to(folder))
        video.unlink(missing_ok=True)
        update(jid,status='done',progress=100,frames=len(frames),cues=len(cues),has_subtitles=bool(cues),warnings=warnings)
    except Exception as e:
        update(jid,status='failed',error=str(e)[:800])

@app.get('/')
def home():
    return FileResponse('static/index.html')

@app.post('/api/jobs')
def create_job(req: JobRequest):
    try:
        vid = video_id(req.url)
    except ValueError as e:
        raise HTTPException(400,str(e))
    if not (0.1 <= req.threshold <= 0.8 and 2 <= req.min_gap <= 60 and 1 <= req.max_frames <= 300):
        raise HTTPException(400,'參數超出允許範圍')
    if req.summary_language not in gemini.LANGUAGES or req.translate not in ('', *gemini.LANGUAGES):
        raise HTTPException(400,'不支援的語系')
    if (req.summary or req.translate) and not gemini.enabled():
        raise HTTPException(400,'未設定 GEMINI_API_KEY，無法摘要或翻譯')
    with LOCK:
        if sum(j['status'] not in ('done','failed') for j in JOBS.values()) >= 1:
            raise HTTPException(429,'已有任務進行中，請稍後再試')
        jid = uuid.uuid4().hex
        JOBS[jid] = {'id':jid,'status':'queued','progress':0,'video_id':vid}
    thread = threading.Thread(target=process,args=(jid,req.url,req.threshold,req.min_gap,req.max_frames,req.summary,req.summary_language,req.translate),daemon=True)
    thread.start()
    return JOBS[jid]

# Declared before /api/jobs/{jid} so 'active' is not taken as a job id.
@app.get('/api/jobs/active')
def active_job():
    with LOCK:
        running = [j for j in JOBS.values() if j['status'] not in ('done','failed')]
    if not running:
        raise HTTPException(404,'沒有進行中的任務')
    return running[0]

@app.get('/api/jobs/{jid}')
def get_job(jid: str):
    if jid not in JOBS:
        raise HTTPException(404,'找不到任務')
    return JOBS[jid]

@app.get('/api/jobs/{jid}/download')
def download(jid: str):
    if jid not in JOBS or JOBS[jid]['status'] != 'done':
        raise HTTPException(404,'尚無可下載的結果')
    return FileResponse(ROOT/jid/'result.zip',filename='video-notes.zip',media_type='application/zip')

@app.get('/api/jobs/{jid}/preview')
def preview(jid: str):
    if jid not in JOBS or JOBS[jid]['status'] != 'done':
        raise HTTPException(404,'尚無預覽')
    return FileResponse(ROOT/jid/'README.md',media_type='text/markdown; charset=utf-8')
