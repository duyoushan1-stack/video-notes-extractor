"""Run with the app's runtime image: real HTTP + FFprobe + FFmpeg, no external API calls."""

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
import urllib.error
import urllib.request
import uuid
import zipfile


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, DATA_DIR=str(root/'jobs'), GEMINI_API_KEY='smoke-test-placeholder')
        server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app',
            '--host', '127.0.0.1', '--port', str(port)], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            base = f'http://127.0.0.1:{port}'
            client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            for attempt in range(100):
                try:
                    with client.open(base, timeout=1):
                        break
                except OSError:
                    if server.poll() is not None:
                        raise RuntimeError('Test server failed to start')
                    time.sleep(0.1)
            else:
                raise AssertionError('Test server did not start')

            def upload(data):
                boundary = uuid.uuid4().hex
                body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                        'filename="fixture.mp4"\r\nContent-Type: video/mp4\r\n\r\n').encode()
                body += data + f'\r\n--{boundary}--\r\n'.encode()
                request = urllib.request.Request(base+'/api/jobs/upload', data=body,
                    headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
                return client.open(request, timeout=30)

            try:
                upload(b'not an MP4')
                raise AssertionError('Invalid media was accepted')
            except urllib.error.HTTPError as exc:
                assert exc.code == 415, exc.read()
                exc.close()

            for audio in (False, True):
                video = root/'fixture.mp4'
                args = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                    '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=4:duration=8']
                if audio:
                    args += ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=8']
                subprocess.run(args + ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)],
                               check=True, timeout=30)
                with upload(video.read_bytes()) as response:
                    assert response.status == 202
                    state = json.load(response)
                jid = state['id']
                try:
                    with client.open(base+'/api/jobs/active') as response:
                        assert json.load(response)['id'] == jid
                except urllib.error.HTTPError as exc:
                    # A tiny fixture may finish before the first active-job request.
                    assert exc.code == 404
                    exc.close()
                for attempt in range(100):
                    with client.open(base+'/api/jobs/'+jid) as response:
                        state = json.load(response)
                    if state['status'] in ('done', 'failed'):
                        break
                    time.sleep(0.1)
                assert state['status'] == 'done', state
                assert state['frames'] > 0, state
                with client.open(base+f'/api/jobs/{jid}/download') as response:
                    with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                        assert 'screenshots/frame-0001.jpg' in archive.namelist()
                        assert not any(name.startswith('video.') for name in archive.namelist())
                        assert b'?t=' not in archive.read('README.md')
                        metadata = json.loads(archive.read('source.json'))
                        assert metadata['source'] == 'mp4'
                        assert metadata['duration'] > 0
                        assert metadata['transcript_status'] == 'unavailable'
                assert not (root/'jobs'/jid/'video.mp4').exists()
                assert not (root/'jobs'/jid/'samples').exists()
                try:
                    urllib.request.urlopen(base+f'/api/jobs/{jid}/download')
                    raise AssertionError('Other browser could download private job')
                except urllib.error.HTTPError as exc:
                    assert exc.code == 404
                    exc.close()
                print(f'PASS: real {"audio" if audio else "silent"} MP4 upload -> {state["frames"]} frames -> ZIP, cleanup and owner isolation')
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == '__main__':
    main()
