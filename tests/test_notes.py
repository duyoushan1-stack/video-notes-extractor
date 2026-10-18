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

