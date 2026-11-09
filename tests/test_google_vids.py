import json
import io
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import zipfile
from unittest import mock

from fastapi.testclient import TestClient
import requests

with tempfile.TemporaryDirectory() as root:
    with mock.patch.dict(os.environ, {'DATA_DIR': root}):
        from app import main

URL = 'https://docs.google.com/videos/d/valid_Vids-123/edit'
CONFIG = {'GOOGLE_CLIENT_ID': 'client', 'GOOGLE_CLIENT_SECRET': 'secret',
          'GOOGLE_REDIRECT_URI': 'https://testserver/api/google/callback'}


class OAuthTests(unittest.TestCase):
    def setUp(self):
        from app import google_vids
        self.g = google_vids
        self.g.CONNECTIONS.clear()
        self.g.PENDING.clear()
        main.JOBS.clear()
        main.OWNERS.clear()
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.env = mock.patch.dict(os.environ, CONFIG)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client.get('/')
        self.owner = self.client.cookies.get('video_notes_owner')

    def connect(self):
        response = self.client.get('/api/google/connect', follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(response.headers['location']).query)
        self.assertIn('code_challenge', query)
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertEqual(query['scope'], ['https://www.googleapis.com/auth/drive.readonly'])
        self.assertEqual(query['redirect_uri'], [CONFIG['GOOGLE_REDIRECT_URI']])
        return query['state'][0]

    def test_status_and_missing_configuration_or_connection(self):
        self.assertEqual(self.client.get('/api/google/status').json(),
                         {'configured': True, 'connected': False})
        with mock.patch.object(main.gemini, 'enabled', return_value=True):
            self.assertEqual(self.client.post('/api/jobs', json={'source': 'google_vids', 'url': URL}).status_code, 401)
        with mock.patch.dict(os.environ, {'GOOGLE_CLIENT_ID': ''}):
            self.assertEqual(self.client.get('/api/google/connect').status_code, 503)
            self.assertEqual(self.client.post('/api/jobs', json={'source': 'google_vids', 'url': URL}).status_code, 503)

    def test_callback_owner_state_expiry_replay_and_fixed_redirect(self):
        state = self.connect()
        with TestClient(main.app) as other:
            self.assertEqual(other.get('/api/google/callback', params={'state': state, 'code': 'code'}).status_code, 400)
        self.assertEqual(self.client.get('/api/google/callback?state=wrong&code=code').status_code, 400)
        with mock.patch('google_auth_oauthlib.flow.Flow.fetch_token', return_value=None), \
             mock.patch('google_auth_oauthlib.flow.Flow.credentials', new_callable=mock.PropertyMock,
                        return_value=mock.Mock(token='access-private', refresh_token='refresh-private')):
            response = self.client.get('/api/google/callback', params={'state': state, 'code': 'code', 'return_url': 'https://evil.test'}, follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        self.assertEqual(response.headers['location'], '/')
        self.assertNotIn('private', response.text)
        self.assertTrue(self.client.get('/api/google/status').json()['connected'])
        self.assertEqual(self.client.get('/api/google/callback', params={'state': state, 'code': 'code'}).status_code, 400)
        with mock.patch.object(self.g.time, 'monotonic', return_value=0):
            state = self.connect()
        with mock.patch.object(self.g.time, 'monotonic', return_value=601):
            self.assertEqual(self.client.get('/api/google/callback', params={'state': state, 'code': 'code'}).status_code, 400)

    def test_callback_denial_consumes_state_and_sanitizes_failure(self):
        state = self.connect()
        response = self.client.get('/api/google/callback', params={'state': state, 'error': 'private'})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('private', response.text)
        self.assertEqual(self.client.get('/api/google/callback', params={'state': state, 'code': 'code'}).status_code, 400)
        state = self.connect()
        with mock.patch('google_auth_oauthlib.flow.Flow.fetch_token', side_effect=ValueError('private-token')):
            response = self.client.get('/api/google/callback', params={'state': state, 'code': 'code'})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('private-token', response.text)

    def test_disconnect_origin_busy_and_revocation(self):
        credentials = mock.Mock(token='private', refresh_token='refresh')
        self.g.CONNECTIONS[self.owner] = credentials
        self.assertEqual(self.client.post('/api/google/disconnect', headers={'Origin': 'https://evil.test'}).status_code, 403)
        jid = main.reserve_job(self.owner, 'google_vids')
        self.assertEqual(self.client.post('/api/google/disconnect', headers={'Origin': 'http://testserver'}).status_code, 409)
        self.assertEqual(self.client.get('/api/google/connect').status_code, 409)
        main.update(jid, status='done')
        with mock.patch.object(self.g, 'revoke') as revoke:
            response = self.client.post('/api/google/disconnect', headers={'Origin': 'http://testserver'})
        self.assertEqual(response.status_code, 200)
        revoke.assert_called_once_with(credentials)
        self.assertFalse(self.client.get('/api/google/status').json()['connected'])

    def test_reconnect_is_blocked_while_google_revocation_is_pending(self):
        self.g.CONNECTIONS[self.owner] = mock.Mock()
        started, finish = threading.Event(), threading.Event()
        def revoke(credentials):
            started.set()
            finish.wait(5)
        responses = []
        with mock.patch.object(self.g, 'revoke', side_effect=revoke):
            worker = threading.Thread(target=lambda: responses.append(self.client.post(
                '/api/google/disconnect', headers={'Origin': 'http://testserver'})))
            worker.start()
            self.assertTrue(started.wait(2))
            try:
                self.assertEqual(self.client.get('/api/google/connect', follow_redirects=False).status_code, 409)
                self.assertEqual(self.client.get('/api/google/callback?state=s&code=c').status_code, 409)
            finally:
                finish.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(responses[0].status_code, 200)
        self.assertEqual(self.client.get('/api/google/connect', follow_redirects=False).status_code, 303)

    def test_refresh_failure_forgets_connection(self):
        credentials = mock.Mock(valid=False, refresh_token='private')
        credentials.refresh.side_effect = ValueError('private')
        self.g.CONNECTIONS[self.owner] = credentials
        with self.assertRaises(self.g.GoogleVidsError) as caught:
            self.g.access_token(credentials)
        self.assertNotIn('private', str(caught.exception))
        self.assertFalse(self.client.get('/api/google/status').json()['connected'])

    def test_connected_owner_creates_vids_job_and_worker_outputs_safe_zip(self):
        from test_upload import UploadTests, inline_launch
        original_url = URL + '?usp=sharing'
        with tempfile.TemporaryDirectory() as root:
            self.g.CONNECTIONS[self.owner] = mock.Mock(valid=True, token='private-token')
            def download(file_id, credentials, destination):
                destination.write_bytes(b'mp4')
                return {'title': 'Lecture', 'source_url': URL}
            with mock.patch.object(main, 'ROOT', Path(root)), \
                 mock.patch.object(main.gemini, 'enabled', return_value=True), \
                 mock.patch.object(main, 'launch_job', side_effect=inline_launch), \
                 mock.patch.object(self.g, 'download_vids', side_effect=download), \
                 mock.patch.object(main.subprocess, 'run', side_effect=UploadTests().media_run):
                response = self.client.post('/api/jobs', json={'source': 'google_vids', 'url': original_url})
            self.assertEqual(response.status_code, 200, response.text)
            state = response.json()
            self.assertEqual(state['status'], 'done', state)
            with zipfile.ZipFile(Path(root) / state['id'] / 'result.zip') as archive:
                source = json.loads(archive.read('source.json'))
                self.assertEqual(source['source'], 'google_vids')
                self.assertEqual(source['url'], original_url)
                self.assertIn(original_url, archive.read('README.md').decode())
                self.assertFalse(any('video.mp4' in name for name in archive.namelist()))
                self.assertNotIn('private-token', str(source))
            self.assertFalse((Path(root) / state['id'] / 'video.mp4').exists())
            with TestClient(main.app) as other:
                self.assertEqual(other.get('/api/jobs/' + state['id']).status_code, 404)

    def test_download_failure_is_safe_and_releases_job_slot(self):
        from test_upload import inline_launch
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(main, 'ROOT', Path(root)), \
             mock.patch.object(main.gemini, 'enabled', return_value=True), \
             mock.patch.object(main, 'launch_job', side_effect=inline_launch), \
             mock.patch.object(self.g, 'download_vids', side_effect=self.g.GoogleVidsError('無法存取影片或沒有下載權限')):
            self.g.CONNECTIONS[self.owner] = mock.Mock()
            response = self.client.post('/api/jobs', json={'source': 'google_vids', 'url': URL})
            self.assertEqual(response.json()['status'], 'failed')
            self.assertIn('無法存取', response.json()['error'])
            self.assertEqual(self.client.post('/api/jobs', json={'source': 'google_vids', 'url': URL}).status_code, 200)

    def test_callback_query_is_removed_from_server_log_scope(self):
        scopes = []
        async def capture(scope, receive, send):
            async def wrapped(message):
                if message['type'] == 'http.response.start':
                    scopes.append(scope['query_string'])
                await send(message)
            await main.app(scope, receive, wrapped)
        with TestClient(capture) as client:
            response = client.get('/api/google/callback?state=private-state&code=private-code')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(scopes, [b''])
        self.assertEqual(response.headers['cache-control'], 'no-store')


class DriveTests(unittest.TestCase):
    def setUp(self):
        from app import google_vids
        self.g = google_vids
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.destination = Path(self.temp.name) / 'video.mp4'
        self.credentials = mock.Mock(valid=True, token='private-token')
        self.session = requests.Session()
        self.patch = mock.patch.object(self.g.requests, 'Session', return_value=self.session)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.calls = []
        self.responses = []
        def send(method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            if isinstance(result, requests.Response):
                return result
            response = requests.Response()
            response.status_code, body = result
            response._content = body if isinstance(body, bytes) else json.dumps(body).encode()
            response._content_consumed = True
            return response
        self.send = mock.patch.object(self.session, 'request', side_effect=send)
        self.send.start()
        self.addCleanup(self.send.stop)
        self.connection = mock.Mock()
        def stream_response():
            body = self.responses.pop(0)
            return SimpleNamespace(status=body[0], read1=io.BytesIO(body[1]).read1, close=lambda: None)
        self.connection.getresponse.side_effect = stream_response
        self.connection_patch = mock.patch('http.client.HTTPSConnection', return_value=self.connection)
        self.connection_patch.start()
        self.addCleanup(self.connection_patch.stop)
        self.metadata = {'name': 'Lecture', 'mimeType': 'application/vnd.google-apps.vid', 'capabilities': {'canDownload': True}}
        self.done = {'done': True, 'response': {'downloadUri': 'https://www.googleapis.com/drive/v3/files/content'}}

    def download(self):
        return self.g.download_vids('valid_Vids-123', self.credentials, self.destination)

    def test_url_allowlist(self):
        self.assertEqual(self.g.parse_vids_url(URL), 'valid_Vids-123')
        for url in ['http://docs.google.com/videos/d/id', 'https://evil.test/videos/d/id',
                    'https://docs.google.com.evil.test/videos/d/id', 'https://user@docs.google.com/videos/d/id',
                    'https://docs.google.com:444/videos/d/id', 'https://docs.google.com/document/d/id',
                    'https://docs.google.com/videos/d/../edit', 'https://docs.google.com/videos/d/id%2fother']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.g.parse_vids_url(url)

    def test_pending_then_done_launches_export_once(self):
        self.responses = [(200, self.metadata), (200, {'name': 'operations/op'}),
                          (503, b'private error'), (200, self.done), (200, b'mp4')]
        with mock.patch.object(self.g.time, 'sleep'):
            info = self.download()
        self.assertEqual(self.destination.read_bytes(), b'mp4')
        self.assertEqual(info, {'title': 'Lecture', 'source_url': 'https://docs.google.com/videos/d/valid_Vids-123/edit'})
        self.assertEqual([c[0] for c in self.calls].count('POST'), 1)
        self.assertIn('mimeType=video%2Fmp4', self.calls[1][1])
        self.assertTrue(all(c[2]['allow_redirects'] is False for c in self.calls))

    def test_opaque_operation_id_uses_official_operations_path(self):
        self.responses = [(200, self.metadata), (200, {'name': 'opaque_operation-id'}),
                          (200, self.done), (200, b'mp4')]
        with mock.patch.object(self.g.time, 'sleep'):
            self.download()
        self.assertEqual(self.calls[2][1], 'https://www.googleapis.com/drive/v3/operations/opaque_operation-id')

    def test_signed_content_host_does_not_receive_bearer(self):
        self.responses = [(200, self.metadata), (200, {'done': True, 'response': {
            'downloadUri': 'https://drive.usercontent.google.com/download?id=opaque'}}), (200, b'mp4')]
        self.download()
        self.assertEqual(self.connection.request.call_args.kwargs['headers'], {})

    def test_export_transient_failure_never_launches_a_second_operation(self):
        self.responses = [(200, self.metadata), (503, b'private')]
        with self.assertRaises(self.g.GoogleVidsError):
            self.download()
        self.assertEqual([c[0] for c in self.calls].count('POST'), 1)

    def test_non_vids_and_download_restriction(self):
        for metadata in [dict(self.metadata, mimeType='video/mp4'), dict(self.metadata, capabilities={'canDownload': False})]:
            with self.subTest(metadata=metadata):
                self.responses = [(200, metadata)]
                with self.assertRaises(self.g.GoogleVidsError):
                    self.download()
                self.assertFalse(self.destination.exists())

    def test_403_404_and_operation_errors_are_safe(self):
        for status in [403, 404]:
            self.responses = [(status, b'private-token')]
            with self.assertRaisesRegex(self.g.GoogleVidsError, '無法存取影片或沒有下載權限'):
                self.download()
        self.responses = [(200, self.metadata), (200, {'done': True, 'error': {'code': 7, 'message': 'private-token'}})]
        with self.assertRaises(self.g.GoogleVidsError) as caught:
            self.download()
        self.assertNotIn('private-token', str(caught.exception))

    def test_canonical_operation_permission_and_authentication_errors(self):
        for code in (5, 7):
            self.responses = [(200, self.metadata), (200, {'done': True, 'error': {'code': code}})]
            with self.assertRaisesRegex(self.g.GoogleVidsError, '無法存取影片或沒有下載權限'):
                self.download()
        self.g.CONNECTIONS['owner'] = self.credentials
        self.responses = [(200, self.metadata), (200, {'done': True, 'error': {'code': 16}})]
        with self.assertRaisesRegex(self.g.GoogleVidsError, '重新連線'):
            self.download()
        self.assertNotIn('owner', self.g.CONNECTIONS)

    def test_slow_stream_is_interrupted_at_absolute_deadline_and_cleans_file(self):
        self.responses = [(200, self.metadata), (200, self.done)]
        interrupted = threading.Event()
        self.connection.sock.shutdown.side_effect = lambda *_: interrupted.set()
        def blocked_read(*args):
            interrupted.wait(2)
            return b'mp4'
        response = requests.Response()
        response.status_code = 200
        response._content = b''
        response._content_consumed = True
        response.iter_content = lambda **kwargs: iter([blocked_read()])
        self.responses.append(response)
        self.connection.getresponse.side_effect = lambda: SimpleNamespace(status=200, read1=blocked_read, close=lambda: None)
        started = time.monotonic()
        with mock.patch.object(self.g, 'TIMEOUT', 0.05):
            with self.assertRaisesRegex(self.g.GoogleVidsError, '逾時'):
                self.download()
        self.assertTrue(interrupted.is_set())
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(self.destination.exists())

    def test_timeout_does_not_restart_export(self):
        self.responses = [(200, self.metadata), (200, {'name': 'operations/op'})]
        with mock.patch.object(self.g.time, 'monotonic', side_effect=[0, 0, 0, 0, 1801]), \
             mock.patch.object(self.g.time, 'sleep'):
            with self.assertRaisesRegex(self.g.GoogleVidsError, '逾時'):
                self.download()
        self.assertEqual([c[0] for c in self.calls].count('POST'), 1)

    def test_stream_limit_cleans_partial_file(self):
        self.responses = [(200, self.metadata), (200, self.done), (200, b'large-mp4')]
        with mock.patch.object(self.g, 'MAX_BYTES', 3):
            with self.assertRaisesRegex(self.g.GoogleVidsError, '上限'):
                self.download()
        self.assertFalse(self.destination.exists())

    def test_untrusted_uri_and_redirect_never_receive_token(self):
        for uri in ['https://evil.test/video', 'https://www.googleapis.com.evil.test/video',
                    'http://www.googleapis.com/drive/v3/file', 'https://www.googleapis.com:444/drive/v3/file']:
            self.responses = [(200, self.metadata), (200, {'done': True, 'response': {'downloadUri': uri}})]
            with self.assertRaises(self.g.GoogleVidsError):
                self.download()
            self.assertFalse(any(call[1] == uri for call in self.calls))
        self.responses = [(200, self.metadata), (200, self.done), (302, b'private-token')]
        with self.assertRaises(self.g.GoogleVidsError):
            self.download()
        self.assertFalse(self.destination.exists())


if __name__ == '__main__':
    unittest.main()
