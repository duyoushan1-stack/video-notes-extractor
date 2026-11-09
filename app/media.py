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
