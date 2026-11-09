"""Real HTTP/FFmpeg verification; default mocks Gemini, --live uses configured API.

Optionally supply --speech /srv/data/.../speech.wav for spoken audio at 2/152/302s.
"""
import argparse
import http.cookiejar
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--speech', type=Path)
    args = parser.parse_args()
    if args.live and (not os.getenv('GEMINI_API_KEY') or not args.speech):
        raise SystemExit('--live requires GEMINI_API_KEY and --speech')
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, DATA_DIR=str(root/'jobs'))
        code = 'import uvicorn; import app.main as m; '
        if not args.live:
            env['GEMINI_API_KEY'] = 'smoke-placeholder'
            # Only the external transport is mocked; audio serialization/validation remains real.
            code += '''import io,json; from unittest.mock import patch;
def response(request, **kwargs):
    body = json.loads(request.data)
    parts = body['contents'][0]['parts']
    if len(parts) > 1:
        assert parts[1]['inlineData']['mimeType'] == 'audio/mp3'
        assert len(parts[1]['inlineData']['data']) > 1000
        text = '[{"start":2,"text":"Spoken test"}]'
    elif body['generationConfig'].get('responseMimeType'):
        text = '[]'
    else:
        text = '# AI output'
    return io.BytesIO(json.dumps({'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':text}]}}]}).encode())
m.gemini.urllib.request.urlopen = response
'''
        code += f'uvicorn.run(m.app, host="127.0.0.1", port={port}, log_level="error")'
        server = subprocess.Popen([sys.executable, '-c', code], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            base = f'http://127.0.0.1:{port}'
            client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            for _ in range(100):
                try:
                    with client.open(base, timeout=1):
                        break
                except OSError:
                    if server.poll() is not None:
                        raise RuntimeError('Smoke server exited')
                    time.sleep(.1)
            else:
                raise RuntimeError('Smoke server unavailable')
            for audio in (True, False):
                video = root/'fixture.mp4'
                cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                    '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=1:duration=310']
                if audio and args.speech:
                    cmd += ['-i', str(args.speech), '-filter_complex',
                        '[1:a]asplit=3[a][b][c];[a]adelay=2000:all=1[aa];'
                        '[b]adelay=152000:all=1[bb];[c]adelay=302000:all=1[cc];'
                        '[aa][bb][cc]amix=inputs=3:normalize=0,apad,atrim=duration=310[out]',
                        '-map', '0:v', '-map', '[out]', '-c:a', 'aac']
                elif audio:
                    cmd += ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=310', '-c:a', 'aac']
                subprocess.run(cmd + ['-c:v', 'libx264', '-preset', 'ultrafast', str(video)],
                               check=True, timeout=120)
                boundary = uuid.uuid4().hex
                body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                        'filename="timing.mp4"\r\nContent-Type: video/mp4\r\n\r\n').encode() + video.read_bytes()
                for name, value in [('summary', 'true'), ('translate', 'en')]:
                    body += f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}'.encode()
                body += f'\r\n--{boundary}--\r\n'.encode()
                with client.open(urllib.request.Request(base+'/api/jobs/upload', data=body,
                    headers={'Content-Type': f'multipart/form-data; boundary={boundary}'}), timeout=60) as response:
                    assert response.status == 202
                    state = json.load(response)
                deadline = time.monotonic() + 1800
                while state['status'] not in ('done', 'failed') and time.monotonic() < deadline:
                    time.sleep(.5)
                    with client.open(base+'/api/jobs/'+state['id'], timeout=10) as response:
                        state = json.load(response)
                assert state['status'] == 'done', state
                with client.open(base+f'/api/jobs/{state["id"]}/download', timeout=30) as response:
                    with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                        cues = json.loads(archive.read('transcript.json'))
                        metadata = json.loads(archive.read('source.json'))
                        assert metadata['duration'] == 310
                        assert state['frames'] > 0
                        assert not any(n.startswith(('video.', 'audio/')) for n in archive.namelist())
                        assert b'?t=' not in archive.read('README.md')
                        if audio:
                            assert metadata['transcript_status'] == 'complete', metadata
                            assert all(cues[i]['start'] <= cues[i+1]['start'] for i in range(len(cues)-1))
                            if args.live:
                                for expected in (2, 152, 302):
                                    assert any(abs(c['start']-expected) < 6 for c in cues), (expected, cues)
                            else:
                                assert [c['start'] for c in cues] == [2, 302], cues
                            assert 'summary.md' in archive.namelist()
                            assert 'README.en.md' in archive.namelist()
                        else:
                            assert not cues
                            assert metadata['transcript_status'] == 'unavailable'
                            assert 'summary.md' not in archive.namelist()
                folder = root/'jobs'/state['id']
                for name in ('video.mp4', 'audio', 'samples'):
                    assert not (folder/name).exists(), name
                print(f'PASS {"live Gemini" if args.live else "mock Gemini"}: 310s '
                      f'{"audio" if audio else "silent"} MP4; cues={[c["start"] for c in cues]}; '
                      f'frames={state["frames"]}; ZIP and cleanup verified', flush=True)
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == '__main__':
    main()
