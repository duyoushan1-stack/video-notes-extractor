import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

import httpx
from fastapi.testclient import TestClient
from PIL import Image
from starlette.requests import ClientDisconnect, Request

with tempfile.TemporaryDirectory() as initial_root:
    with mock.patch.dict(os.environ, {'DATA_DIR': initial_root}):
        from app import main


def inline_launch(jid, req, **kwargs):
    main.process(jid, req.url, req.threshold, req.min_gap, req.max_frames,
                 req.summary, req.summary_language, req.translate, **kwargs)


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.patch = mock.patch.object(main, 'ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        main.JOBS.clear()
        if hasattr(main, 'OWNERS'):
            main.OWNERS.clear()
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.enabled = mock.patch.object(main.gemini, 'enabled', return_value=True)
        self.enabled.start()
        self.addCleanup(self.enabled.stop)

    def upload(self, content=b'mp4-content', filename='lecture.mp4', **kwargs):
        return self.client.post('/api/jobs/upload', files={'file': (filename, content, 'video/mp4')}, **kwargs)

    def media_run(self, args, **kwargs):
        if args[0] == 'ffprobe':
            return subprocess.CompletedProcess(args, 0, json.dumps({
                'format': {'format_name': 'mov,mp4,m4a,3gp,3g2,mj2', 'duration': '8',
                           'tags': {'major_brand': 'isom'}},
                'streams': [{'codec_type': 'video'}]}), '')
        if args[0] == 'ffmpeg':
            path = Path(args[-1].replace('%06d', '000001'))
            Image.effect_noise((320, 180), 80).convert('RGB').save(path)
            return subprocess.CompletedProcess(args, 0, '', '')
        if args[0] == 'yt-dlp':
            folder = Path(kwargs['cwd'])
            (folder / 'video.mp4').write_bytes(b'downloaded')
            (folder / 'video.info.json').write_text(json.dumps({'title': 'YT title', 'duration': 8}))
            (folder / 'video.en.vtt').write_text('WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nhello\n')
            return subprocess.CompletedProcess(args, 0, '', '')
        raise AssertionError(args)

    def test_upload_to_zip_uses_relative_images_and_honest_source(self):
        with mock.patch.object(main, 'launch_job', side_effect=inline_launch), \
                mock.patch.object(main.subprocess, 'run', side_effect=self.media_run):
            response = self.upload(filename='../../lecture.mp4')
        self.assertEqual(response.status_code, 202, response.text)
        state = response.json()
        self.assertEqual(state['status'], 'done', state)
        self.assertEqual(state['source'], 'mp4')
        self.assertEqual(state['title'], 'lecture.mp4')
        self.assertEqual(state['transcript_status'], 'unavailable')
        jid = state['id']
        result = self.client.get(f'/api/jobs/{jid}/download')
        self.assertEqual(result.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
            self.assertIn('screenshots/frame-0001.jpg', archive.namelist())
            self.assertNotIn('video.mp4', archive.namelist())
            notes = archive.read('README.md').decode()
            self.assertIn('lecture.mp4', notes)
            self.assertIn('screenshots/frame-0001.jpg', notes)
            self.assertNotIn('?t=', notes)
            self.assertIn('尚未', notes)
            source = json.loads(archive.read('source.json'))
            self.assertEqual(source['filename'], 'lecture.mp4')
            self.assertNotIn(str(self.root), json.dumps(source))
            self.assertEqual(json.loads(archive.read('transcript.json')), [])
        self.assertEqual(self.client.get(f'/api/jobs/{jid}/preview').status_code, 200)
        self.assertFalse((self.root / jid / 'video.mp4').exists())
        self.assertFalse((self.root / jid / 'samples').exists())

    def test_file_limit_uses_received_bytes_and_releases_slot(self):
        with mock.patch.object(main, 'MAX_UPLOAD_BYTES', 8, create=True):
            response = self.upload(content=b'x' * 9)
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(list(self.root.iterdir()), [])
        with mock.patch.object(main, 'launch_job'):
            with mock.patch.object(main.subprocess, 'run', side_effect=self.media_run):
                self.assertEqual(self.upload().status_code, 202)

    def test_wrong_media_is_415_and_leaves_no_file(self):
        bad = subprocess.CompletedProcess([], 1, '', 'private filesystem path')
        with mock.patch.object(main.subprocess, 'run', return_value=bad):
            response = self.upload()
        self.assertEqual(response.status_code, 415, response.text)
        self.assertNotIn('private filesystem', response.text)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_rejected_upload_reports_cleanup_failure_without_holding_slot(self):
        bad = subprocess.CompletedProcess([], 1, '', 'invalid media')
        with mock.patch.object(main.subprocess, 'run', return_value=bad), \
                mock.patch.object(main.shutil, 'rmtree', side_effect=PermissionError('locked folder')):
            response = self.upload()
        self.assertEqual(response.status_code, 415)
        state = list(main.JOBS.values())[-1]
        self.assertEqual(state['status'], 'failed')
        self.assertTrue(any('清理' in warning for warning in state['warnings']))
        with mock.patch.object(main, 'launch_job'), \
                mock.patch.object(main.subprocess, 'run', side_effect=self.media_run):
            self.assertEqual(self.upload().status_code, 202)

    def test_multiple_files_and_duplicate_fields_are_rejected(self):
        for parts in [
            [('file', ('a.mp4', b'a')), ('file', ('b.mp4', b'b'))],
            [('file', ('a.mp4', b'a')), ('threshold', (None, '0.3')), ('threshold', (None, '0.4'))],
        ]:
            response = self.client.post('/api/jobs/upload', files=parts)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_file_invalid_options_and_unavailable_ai_are_explicit(self):
        response = self.client.post('/api/jobs/upload', files={'threshold': (None, '0.3')})
        self.assertEqual(response.status_code, 400)
        for data in [{'threshold': 'NaN'}, {'max_frames': '301'}, {'translate': '../bad'},
                     {'source': 'youtube'}, {'summary': 'true'}, {'threshold': 'abc'}]:
            response = self.upload(data=data)
            self.assertIn(response.status_code, (400, 422), response.text)
            self.assertEqual(list(self.root.iterdir()), [])
        with mock.patch.object(main.gemini, 'enabled', return_value=False):
            self.assertEqual(self.upload().status_code, 400)

    def test_owner_isolation_and_refresh_reattach(self):
        with mock.patch.object(main, 'launch_job'), \
                mock.patch.object(main.subprocess, 'run', side_effect=self.media_run):
            response = self.upload()
        self.assertEqual(response.status_code, 202)
        jid = response.json()['id']
        self.assertEqual(self.client.get('/api/jobs/active').json()['id'], jid)
        cookie = response.headers.get('set-cookie', '').lower()
        self.assertIn('httponly', cookie)
        self.assertIn('samesite=lax', cookie)
        with TestClient(main.app) as other:
            for path in ['/api/jobs/active', f'/api/jobs/{jid}', f'/api/jobs/{jid}/preview',
                         f'/api/jobs/{jid}/download']:
                self.assertEqual(other.get(path).status_code, 404)
        self.assertEqual(self.upload().status_code, 429)

    def test_worker_failure_cleans_media_and_releases_slot(self):
        def fail_frames(args, **kwargs):
            if args[0] == 'ffmpeg':
                return subprocess.CompletedProcess(args, 1, '', 'bad media')
            return self.media_run(args, **kwargs)
        with mock.patch.object(main, 'launch_job', side_effect=inline_launch), \
                mock.patch.object(main.subprocess, 'run', side_effect=fail_frames):
            response = self.upload()
        self.assertEqual(response.status_code, 202)
        state = response.json()
        self.assertEqual(state['status'], 'failed')
        self.assertFalse((self.root / state['id'] / 'video.mp4').exists())
        self.assertFalse((self.root / state['id'] / 'samples').exists())
        with mock.patch.object(main, 'launch_job'), \
                mock.patch.object(main.subprocess, 'run', side_effect=self.media_run):
            self.assertEqual(self.upload().status_code, 202)

    def test_youtube_json_still_generates_captions_and_zip_without_google_setup(self):
        with mock.patch.object(main, 'launch_job', side_effect=inline_launch), \
                mock.patch.object(main.subprocess, 'run', side_effect=self.media_run), \
                mock.patch.object(main.gemini, 'enabled', return_value=False):
            response = self.client.post('/api/jobs', json={'url': 'https://youtu.be/abcdefghijk'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'done')
        jid = response.json()['id']
        with zipfile.ZipFile(io.BytesIO(self.client.get(f'/api/jobs/{jid}/download').content)) as archive:
            self.assertIn('hello', archive.read('README.md').decode())
            self.assertIn('?t=', archive.read('README.md').decode())
            self.assertEqual(json.loads(archive.read('source.json'))['transcript_source'], 'youtube_captions')

    def test_cleanup_error_never_holds_the_only_job_slot(self):
        original_unlink = Path.unlink

        def locked_video(path, *args, **kwargs):
            if path.name == 'video.mp4':
                raise PermissionError('locked video')
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(main, 'launch_job', side_effect=inline_launch), \
                mock.patch.object(main.subprocess, 'run', side_effect=self.media_run), \
                mock.patch.object(Path, 'unlink', locked_video):
            response = self.upload()
        self.assertEqual(response.status_code, 202)
        state = response.json()
        self.assertIn(state['status'], ('done', 'failed'))
        self.assertTrue(any('清理' in w for w in state['warnings']), state)
        with mock.patch.object(main, 'launch_job'), \
                mock.patch.object(main.subprocess, 'run', side_effect=self.media_run):
            self.assertEqual(self.upload().status_code, 202)

    def test_stream_admission_precedes_body_and_disconnect_releases_slot(self):
        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()

            async def slow_body():
                entered.set()
                await release.wait()
                raise ClientDisconnect()
                yield b''

            async def forbidden_body():
                self.fail('Busy request must not consume any request body')
                yield b''

            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
                first = asyncio.create_task(client.post('/api/jobs/upload', content=slow_body(),
                    headers={'content-type': 'multipart/form-data; boundary=abc'}))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    second = await client.post('/api/jobs/upload', content=forbidden_body(),
                        headers={'content-type': 'multipart/form-data; boundary=abc'})
                    self.assertEqual(second.status_code, 429)
                    youtube = await client.post('/api/jobs', json={'url': 'https://youtu.be/abcdefghijk'})
                    self.assertEqual(youtube.status_code, 429)
                finally:
                    release.set()
                self.assertEqual((await first).status_code, 400)
                self.assertEqual(list(self.root.iterdir()), [])
                self.assertTrue(all(j['status'] == 'failed' for j in main.JOBS.values()))
        asyncio.run(scenario())

    def test_malformed_truncated_and_oversized_multipart(self):
        for body in [b'garbage', b'--abc\r\nContent-Disposition: form-data; name="file"; filename="a.mp4"\r\n\r\ndata']:
            response = self.client.post('/api/jobs/upload', content=body,
                                       headers={'content-type': 'multipart/form-data; boundary=abc'})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(list(self.root.iterdir()), [])
        with mock.patch.object(main, 'MAX_REQUEST_OVERHEAD', 10, create=True), \
                mock.patch.object(main, 'MAX_UPLOAD_BYTES', 8, create=True):
            response = self.client.post('/api/jobs/upload', content=b'x' * 19,
                                       headers={'content-type': 'multipart/form-data; boundary=abc'})
            self.assertEqual(response.status_code, 413)

    def test_idle_timeout_releases_slot(self):
        async def scenario():
            async def idle():
                await asyncio.sleep(1)
                yield b''
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                         base_url='http://testserver') as client:
                response = await client.post('/api/jobs/upload', content=idle(),
                    headers={'content-type': 'multipart/form-data; boundary=abc'})
                self.assertEqual(response.status_code, 408)
        with mock.patch.object(main, 'UPLOAD_IDLE_SECONDS', 0.01, create=True):
            asyncio.run(scenario())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cancellation_cleans_partially_received_file_and_releases_slot(self):
        async def scenario():
            receiving = asyncio.Event()
            first = True

            async def receive():
                nonlocal first
                if first:
                    first = False
                    body = (b'--abc\r\nContent-Disposition: form-data; name="file"; '
                            b'filename="a.mp4"\r\n\r\npartial bytes')
                    return {'type': 'http.request', 'body': body, 'more_body': True}
                receiving.set()
                await asyncio.Event().wait()

            request = Request({'type': 'http', 'method': 'POST', 'path': '/api/jobs/upload',
                'headers': [(b'content-type', b'multipart/form-data; boundary=abc')],
                'state': {'owner': 'a' * 32}}, receive)
            task = asyncio.create_task(main.upload_job(request))
            await asyncio.wait_for(receiving.wait(), 2)
            self.assertEqual(len(list(self.root.glob('*/video.mp4'))), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(list(self.root.iterdir()), [])
            self.assertTrue(all(j['status'] == 'failed' for j in main.JOBS.values()))
        asyncio.run(scenario())
        with mock.patch.object(main, 'launch_job'), \
                mock.patch.object(main.subprocess, 'run', side_effect=self.media_run):
            self.assertEqual(self.upload().status_code, 202)


class ProbeTests(unittest.TestCase):
    def test_probe_accepts_real_mp4_video_with_optional_audio(self):
        from app.media import probe_video
        for audio in (False, True):
            streams = [{'codec_type': 'video'}] + ([{'codec_type': 'audio'}] if audio else [])
            data = {'format': {'format_name': 'mov,mp4,m4a,3gp,3g2,mj2', 'duration': '9.5',
                               'tags': {'major_brand': 'isom'}}, 'streams': streams}
            with mock.patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, json.dumps(data), '')):
                self.assertEqual(probe_video(Path('video.mp4')), {'duration': 9.5, 'has_audio': audio})

    def test_probe_rejects_fake_mp4_audio_only_nonfinite_and_quicktime(self):
        from app.media import probe_video
        samples = [
            {'format': {'format_name': 'matroska,webm', 'duration': '3'}, 'streams': [{'codec_type': 'video'}]},
            {'format': {'format_name': 'mov,mp4,m4a,3gp,3g2,mj2', 'duration': '3', 'tags': {'major_brand': 'qt  '}}, 'streams': [{'codec_type': 'video'}]},
            {'format': {'format_name': 'mov,mp4,m4a,3gp,3g2,mj2', 'duration': '3'}, 'streams': [{'codec_type': 'audio'}]},
            {'format': {'format_name': 'mp4', 'duration': 'NaN'}, 'streams': [{'codec_type': 'video'}]},
        ]
        for sample in samples:
            with mock.patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, json.dumps(sample), '')):
                with self.assertRaises(ValueError):
                    probe_video(Path('fake.mp4'))


class ParserCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_waits_for_inflight_write_before_closing_and_cleanup(self):
        from app import uploads
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        parser_factory = uploads.MultipartParser
        write_errors = []

        def blocking_factory(*args, **kwargs):
            parser = parser_factory(*args, **kwargs)
            original = parser.write

            def write(data):
                entered.set()
                release.wait(3)
                try:
                    return original(data)
                except Exception as exc:
                    write_errors.append(exc)
                    raise
                finally:
                    finished.set()
            parser.write = write
            return parser

        body = (b'--abc\r\nContent-Disposition: form-data; name="file"; '
                b'filename="a.mp4"\r\n\r\nvideo\r\n--abc--\r\n')

        async def receive():
            return {'type': 'http.request', 'body': body, 'more_body': False}

        request = Request({'type': 'http', 'headers': [
            (b'content-type', b'multipart/form-data; boundary=abc')]}, receive)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(uploads, 'MultipartParser', side_effect=blocking_factory):
            task = asyncio.create_task(uploads.receive_upload(request, Path(directory)/'video.mp4',
                                       1000, 1000, 1))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                await asyncio.sleep(0.02)
                premature_close = task.done()
            finally:
                release.set()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                self.assertTrue(await asyncio.to_thread(finished.wait, 2))
            self.assertFalse(premature_close, 'must wait for writer before closing its file')
            self.assertEqual(write_errors, [])


if __name__ == '__main__':
    unittest.main()
