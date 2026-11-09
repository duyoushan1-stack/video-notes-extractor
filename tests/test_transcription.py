import base64
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.error
import zipfile

from app import gemini, media
import test_upload as upload_fixture

main = upload_fixture.main
inline_launch = upload_fixture.inline_launch


def reply(text, finish='STOP'):
    return io.BytesIO(json.dumps({'candidates': [{'finishReason': finish,
        'content': {'parts': [{'text': text}]}}]}).encode())


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.audio = Path(self.directory.name)/'chunk.mp3'
        self.audio.write_bytes(b'mp3 bytes')

    def test_inline_audio_json_and_stable_sort(self):
        with mock.patch.dict(os.environ, {'GEMINI_API_KEY': 'test'}), \
                mock.patch.object(gemini.urllib.request, 'urlopen', return_value=reply(
                    '[{"start":3,"text":"later"},{"start":2,"text":"first"},{"start":2,"text":"same"}]')) as call:
            cues = gemini.transcribe_audio(self.audio, 5)
        self.assertEqual([c['text'] for c in cues], ['first', 'same', 'later'])
        body = json.loads(call.call_args.args[0].data)
        self.assertEqual(body['contents'][0]['parts'][1]['inlineData'],
            {'mimeType': 'audio/mp3', 'data': base64.b64encode(b'mp3 bytes').decode()})
        self.assertEqual(body['generationConfig']['responseMimeType'], 'application/json')
        self.assertEqual(call.call_args.kwargs['timeout'], 600)

    def test_default_model_and_text_caller_still_work(self):
        with mock.patch.dict(os.environ, {'GEMINI_API_KEY': 'test'}, clear=True), \
                mock.patch.object(gemini.urllib.request, 'urlopen', return_value=reply('text')) as call:
            self.assertEqual(gemini.generate('prompt'), 'text')
        self.assertIn('/gemini-3.8-flash:generateContent', call.call_args.args[0].full_url)
        self.assertEqual(json.loads(call.call_args.args[0].data)['contents'][0]['parts'], [{'text': 'prompt'}])

    def test_rejects_entire_invalid_chunk(self):
        for value in ['oops', '{}', '[{"start":NaN,"text":"x"}]',
                      '[{"start":Infinity,"text":"x"}]', '[{"start":5,"text":"x"}]',
                      '[{"start":-1,"text":"x"}]', '[{"start":true,"text":"x"}]',
                      '[{"start":"2","text":"x"}]', '[{"start":0,"text":" "}]',
                      '[{"start":0,"text":42}]', '[{"start":0,"text":"ok"},{}]']:
            with self.subTest(value=value), mock.patch.object(gemini, 'generate', return_value=value):
                with self.assertRaisesRegex(RuntimeError, '轉錄'):
                    gemini.transcribe_audio(self.audio, 5)

    def test_no_speech_is_empty(self):
        with mock.patch.object(gemini, 'generate', return_value='[]'):
            self.assertEqual(gemini.transcribe_audio(self.audio, 5), [])

    def test_non_stop_and_errors_are_safe(self):
        with mock.patch.dict(os.environ, {'GEMINI_API_KEY': 'secret'}), \
                mock.patch.object(gemini.urllib.request, 'urlopen', return_value=reply('[]', 'MAX_TOKENS')):
            with self.assertRaisesRegex(RuntimeError, '轉錄'):
                gemini.transcribe_audio(self.audio, 5)
        for code in (400, 401, 403, 429, 500, 502, 503, 504):
            def failure(*args, **kwargs):
                raise urllib.error.HTTPError('secret-url', code, 'bad', {}, io.BytesIO(b'secret-body'))
            with self.subTest(code=code), mock.patch.dict(os.environ, {'GEMINI_API_KEY': 'secret'}), \
                    mock.patch.object(gemini.urllib.request, 'urlopen', side_effect=failure) as call, \
                    mock.patch.object(gemini.time, 'sleep') as sleep:
                with self.assertRaises(RuntimeError) as error:
                    gemini.transcribe_audio(self.audio, 5)
                self.assertNotIn('secret', str(error.exception))
                retry = code in (429, 500, 502, 503, 504)
                self.assertEqual(call.call_count, 4 if retry else 1)
                self.assertEqual(sleep.call_args_list, [mock.call(n) for n in (15, 30, 45)] if retry else [])

    def test_retry_then_success(self):
        limited = urllib.error.HTTPError('u', 429, 'limited', {}, io.BytesIO())
        with mock.patch.dict(os.environ, {'GEMINI_API_KEY': 'test'}), \
                mock.patch.object(gemini.urllib.request, 'urlopen', side_effect=[limited, reply('[]')]), \
                mock.patch.object(gemini.time, 'sleep') as sleep:
            self.assertEqual(gemini.transcribe_audio(self.audio, 5), [])
        sleep.assert_called_once_with(15)

    def test_audio_chunks_cover_short_tail_and_encoding(self):
        with mock.patch.object(media, 'probe_video', return_value={'duration': 305.5, 'has_audio': True}), \
                mock.patch.object(media.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            chunks = media.audio_chunks(Path('video.mp4'), Path(self.directory.name))
        self.assertEqual([(offset, duration) for _, offset, duration in chunks], [(0, 300), (300, 5.5)])
        for call in run.call_args_list:
            args = call.args[0]
            for flag, value in [('-ac', '1'), ('-ar', '16000'), ('-b:a', '64k')]:
                self.assertEqual(args[args.index(flag)+1], value)

    def test_extraction_retains_first_chunk_when_tail_fails(self):
        with mock.patch.object(media, 'probe_video', return_value={'duration': 305, 'has_audio': True}), \
                mock.patch.object(media.subprocess, 'run', side_effect=[
                    subprocess.CompletedProcess([], 0), subprocess.CalledProcessError(1, ['ffmpeg'])]):
            chunks = media.audio_chunks(Path('video.mp4'), Path(self.directory.name))
        self.assertEqual([(offset, duration) for _, offset, duration in chunks], [(0, 300)])


class TranscriptionWorkerTests(unittest.TestCase):
    setUp = upload_fixture.UploadTests.setUp
    upload = upload_fixture.UploadTests.upload
    media_run = upload_fixture.UploadTests.media_run
    def run_audio(self, replies, has_audio=True, chunk_error=False, missing_tail=False):
        def probe(path):
            return {'duration': 305, 'has_audio': has_audio}
        def chunks(video, folder):
            audio = folder/'audio'
            audio.mkdir()
            (audio/'partial.mp3').write_bytes(b'audio')
            if chunk_error:
                raise RuntimeError('private path')
            return [(audio/'a.mp3', 0, 300)] if missing_tail else [(audio/'a.mp3', 0, 300), (audio/'b.mp3', 300, 5)]
        with mock.patch.object(main, 'launch_job', side_effect=inline_launch), \
                mock.patch.object(main, 'probe_video', side_effect=probe), \
                mock.patch.object(main, 'audio_chunks', side_effect=chunks) as extraction, \
                mock.patch.object(main.subprocess, 'run', side_effect=self.media_run), \
                mock.patch.object(gemini, 'transcribe_audio', side_effect=replies) as transcribe, \
                mock.patch.object(gemini, 'make_chapters', return_value=[]) as chapters, \
                mock.patch.object(gemini, 'summarize', return_value='# Summary\n') as summary, \
                mock.patch.object(gemini, 'translate', return_value=('# Translation\n', 0)) as translate, \
                mock.patch.object(main, 'update', wraps=main.update) as updates:
            response = self.upload(data={'summary': 'true', 'translate': 'en'})
        self.assertEqual(response.status_code, 202, response.text)
        state = response.json()
        self.assertEqual(state['status'], 'done', state)
        folder = self.root/state['id']
        self.assertFalse((folder/'audio').exists())
        self.assertFalse((folder/'video.mp4').exists())
        with zipfile.ZipFile(folder/'result.zip') as archive:
            files = {name: archive.read(name).decode() for name in archive.namelist() if not name.endswith('.jpg')}
            self.assertFalse(any(name.startswith('audio/') for name in archive.namelist()))
        return state, files, (extraction, transcribe, chapters, summary, translate, updates)

    def test_offsets_and_complete_outputs(self):
        state, files, calls = self.run_audio([[{'start': 2, 'text': 'A'}], [{'start': 3, 'text': 'B'}]])
        self.assertEqual([c['start'] for c in json.loads(files['transcript.json'])], [2, 303])
        self.assertEqual(state['transcript_status'], 'complete')
        self.assertEqual(state['transcript_source'], 'gemini')
        self.assertTrue(any(c.kwargs.get('status') == 'transcribing' for c in calls[-1].call_args_list))

    def test_partial_warns_in_all_outputs(self):
        state, files, calls = self.run_audio([[{'start': 2, 'text': 'A'}], RuntimeError('private-secret')])
        self.assertEqual(state['transcript_status'], 'partial')
        self.assertEqual(json.loads(files['transcript.json']), [{'start': 2, 'text': 'A'}])
        for name in ('README.md', 'summary.md', 'README.en.md', 'source.json'):
            self.assertIn('00:05:00', files[name])
            self.assertIn('00:05:05', files[name])
            self.assertIn('不完整', files[name])
            self.assertNotIn('private-secret', files[name])
        for call in calls[2:5]:
            call.assert_called_once()

    def test_missing_extracted_tail_preserves_usable_transcript(self):
        state, files, calls = self.run_audio([[{'start': 2, 'text': 'A'}]], missing_tail=True)
        self.assertEqual(state['transcript_status'], 'partial')
        self.assertIn('00:05:00', files['source.json'])
        self.assertIn('00:05:05', files['summary.md'])
        self.assertEqual(json.loads(files['transcript.json']), [{'start': 2, 'text': 'A'}])

    def test_unavailable_skips_text_ai(self):
        for replies, audio, chunk_error in [([RuntimeError('bad')]*2, True, False),
                ([], False, False), ([[], []], True, False), ([], True, True)]:
            with self.subTest(audio=audio, error=chunk_error):
                state, files, calls = self.run_audio(replies, audio, chunk_error)
                self.assertEqual(state['transcript_status'], 'unavailable')
                self.assertTrue(state['warnings'])
                self.assertNotIn('summary.md', files)
                for call in calls[2:5]:
                    call.assert_not_called()
                if not audio:
                    calls[0].assert_not_called()
                    calls[1].assert_not_called()
