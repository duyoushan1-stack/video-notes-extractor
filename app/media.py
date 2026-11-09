"""Validate uploaded media before it enters the shared frame pipeline."""

import json
import math
from pathlib import Path
import subprocess


def probe_video(path: Path) -> dict:
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)],
            capture_output=True, text=True, timeout=60, check=True)
        if result.returncode:
            raise ValueError('invalid media')
        data = json.loads(result.stdout)
        info = data['format']
        formats = info['format_name'].split(',')
        brand = info.get('tags', {}).get('major_brand', '').strip()
        duration = float(info['duration'])
        streams = data['streams']
        if ('mp4' not in formats or not brand or brand == 'qt' or
                brand.startswith(('3g', 'mjp')) or not math.isfinite(duration) or duration <= 0 or
                not any(s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')
                        for s in streams)):
            raise ValueError('invalid media')
    except (ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise ValueError('檔案必須是可讀取且含視訊軌的 MP4 影片') from exc
    return {'duration': duration, 'has_audio': any(s.get('codec_type') == 'audio' for s in streams)}


def audio_chunks(video: Path, folder: Path) -> list[tuple[Path, float, float]]:
    """Extract non-overlapping MP3 chunks with offsets on the original timeline."""
    info = probe_video(video)
    if not info['has_audio']:
        return []
    audio = folder/'audio'
    audio.mkdir(exist_ok=True)
    chunks = []
    for offset in range(0, math.ceil(info['duration']), 300):
        duration = min(300, info['duration'] - offset)
        path = audio/f'chunk-{len(chunks):05}.mp3'
        try:
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                '-ss', str(offset), '-i', str(video), '-t', str(duration), '-map', '0:a:0',
                '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'libmp3lame', '-b:a', '64k', str(path)],
                capture_output=True, timeout=600, check=True)
        except (OSError, subprocess.SubprocessError):
            # Keep usable chunks. The worker records holes against the source timeline.
            continue
        chunks.append((path, offset, duration))
    return chunks
