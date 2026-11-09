import unittest

from app.notes import paragraphs, render_notes, video_link


class NotesTests(unittest.TestCase):
    def test_chapter_boundary_keeps_each_caption_once(self):
        chapters = [dict(start_time=0, end_time=10, title='First'),
                    dict(start_time=10, end_time=20, title='Second')]
        cues = [dict(start=0, text='opening'), dict(start=8, text='before boundary'),
                dict(start=10, text='at boundary'), dict(start=19, text='ending')]
        result = render_notes('Title', 'https://youtu.be/example', chapters,
                              [dict(time=5, file='one.jpg')], cues, 2, 1)
        first, second = result.split('## First\n')[1].split('## Second\n')
        self.assertIn('opening', first)
        self.assertIn('before boundary', first)
        self.assertNotIn('at boundary', first)
        self.assertIn('at boundary', second)
        for cue in cues:
            self.assertEqual(result.count(cue['text']), 1)
        self.assertNotIn('## 完整逐字稿', result)

    def test_long_unpunctuated_captions_have_paragraph_breaks(self):
        cues = [dict(start=index, text='字幕內容' * 10) for index in range(20)]
        result = paragraphs(cues)
        self.assertGreater(result.count(''), 1)
        self.assertTrue(all(len(line) <= 180 for line in result))

    def test_no_chapters_or_frames_preserves_captions(self):
        result = render_notes('Title', 'https://youtu.be/example', [], [],
                              [dict(start=1, text='caption')], 2, 0)
        self.assertIn('## 影片內容', result)
        self.assertIn('caption', result)

    def test_link_handles_short_urls_and_existing_timestamp(self):
        self.assertEqual(video_link('https://youtu.be/example?t=4', 10),
                         'https://youtu.be/example?t=10s')

    def test_ai_chapters_feed_chapter_sections(self):
        from unittest import mock
        from app import gemini
        reply = '[{"start": 30, "title": "Second"}, {"start": 0, "title": "First"}, {"start": -1, "title": "bad"}]'
        with mock.patch.object(gemini, 'generate', return_value=reply):
            chapters = gemini.make_chapters([dict(start=1, text='a'), dict(start=40, text='b')], 60)
        self.assertEqual(chapters, [dict(start_time=0.0, end_time=30.0, title='First'),
                                    dict(start_time=30.0, end_time=60.0, title='Second')])
        result = render_notes('Title', 'https://youtu.be/example', chapters, [],
                              [dict(start=1, text='a'), dict(start=40, text='b')], 2, 0)
        self.assertIn('a', result.split('## First\n')[1].split('## Second\n')[0])

    def test_translate_sends_one_request_per_chapter(self):
        from unittest import mock
        from app import gemini
        with mock.patch.object(gemini, 'generate', side_effect=lambda p: '```markdown\nX\n```') as call:
            result = gemini.translate('# T\n\n## A\n\ntext\n\n## B\n\ntext\n', 'English')
        self.assertEqual(call.call_count, 3)
        self.assertEqual(result, ('X\n\nX\n\nX\n', 0))

    def test_failed_chapter_keeps_original_and_others_survive(self):
        from unittest import mock
        from app import gemini
        replies = iter(['X', RuntimeError('429'), 'Z'])

        def fake(prompt):
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply
        with mock.patch.object(gemini, 'generate', side_effect=fake):
            result, failed = gemini.translate('# T\n\n## A\n\norig\n\n## B\n\ntext\n', 'English')
        self.assertEqual(failed, 1)
        self.assertIn('## A\n\norig', result)
        self.assertTrue(result.startswith('X') and result.endswith('Z\n'))

    def test_chapter_reply_tolerates_wrapper_and_clock_times(self):
        from unittest import mock
        from app import gemini
        reply = '{"chapters": [{"start": "1:30", "title": "B"}, {"start": 0, "title": "A"}, {"title": "no start"}]}'
        with mock.patch.object(gemini, 'generate', return_value=reply):
            chapters = gemini.make_chapters([dict(start=1, text='a')], 120)
        self.assertEqual([(c['start_time'], c['title']) for c in chapters], [(0.0, 'A'), (90.0, 'B')])

    def test_generate_retries_rate_limit(self):
        import io
        import os
        import urllib.error
        from unittest import mock
        from app import gemini
        ok = io.BytesIO(b'{"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "hi"}]}}]}')
        limited = urllib.error.HTTPError('u', 429, 'Too Many', {}, io.BytesIO(b''))
        with mock.patch.dict(os.environ, {'GEMINI_API_KEY': 'k'}), \
                mock.patch.object(gemini.time, 'sleep') as sleep, \
                mock.patch.object(gemini.urllib.request, 'urlopen', side_effect=[limited, ok]):
            self.assertEqual(gemini.generate('p'), 'hi')
        sleep.assert_called_once()

    def test_generate_reports_truncation(self):
        import io
        import os
        from unittest import mock
        from app import gemini
        cut = io.BytesIO(b'{"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "h"}]}}]}')
        with mock.patch.dict(os.environ, {'GEMINI_API_KEY': 'k'}), \
                mock.patch.object(gemini.urllib.request, 'urlopen', return_value=cut):
            with self.assertRaisesRegex(RuntimeError, 'MAX_TOKENS'):
                gemini.generate('p')

    def test_vtt_end_time_is_not_caption_text(self):
        # Load only the pure parser so tests need no web-server dependencies.
        import ast
        import re
        import tempfile
        from pathlib import Path
        tree = ast.parse(Path('app/main.py').read_text(encoding='utf-8'))
        parser = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'parse_vtt')
        scope = {'re': re}
        exec(compile(ast.Module(body=[parser], type_ignores=[]), 'parser', 'exec'), scope)
        with tempfile.TemporaryDirectory(dir='tests') as directory:
            path = Path(directory) / 'test.vtt'
            path.write_text('WEBVTT\n\n00:00:01.000 --> 00:00:03.000 align:start\n字幕內容\n', encoding='utf-8')
            self.assertEqual(scope['parse_vtt'](path), [dict(start=1.0, text='字幕內容')])


if __name__ == '__main__':
    unittest.main()

