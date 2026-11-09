"""Owner-bound, memory-only Google OAuth and bounded Vids downloads."""
import http.client
import os
import re
import secrets
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import requests
from google.auth.transport.requests import Request as GoogleRequest
from google_auth_oauthlib.flow import Flow

SCOPE = 'https://www.googleapis.com/auth/drive.readonly'
API = 'https://www.googleapis.com/drive/v3/'
MAX_BYTES = 1_500_000_000
TIMEOUT = 30 * 60
CONNECTIONS = {}
PENDING = {}
LOCK = threading.RLock()


class GoogleVidsError(ValueError):
    """Only fixed, user-safe messages may cross the worker boundary."""


def parse_vids_url(url: str) -> str:
    parsed = urlsplit(url)
    match = re.fullmatch(r'/videos/d/([A-Za-z0-9_-]+)(?:/[^\s]*)?', parsed.path)
    if (parsed.scheme != 'https' or parsed.netloc != 'docs.google.com' or not match
            or any(ord(c) < 33 for c in url)):
        raise ValueError('請輸入有效的 HTTPS Google Vids 網址')
    return match[1]


def configured():
    uri = os.getenv('GOOGLE_REDIRECT_URI', '')
    parsed = urlsplit(uri)
    return bool(os.getenv('GOOGLE_CLIENT_ID') and os.getenv('GOOGLE_CLIENT_SECRET')
                and parsed.path == '/api/google/callback' and not parsed.query
                and not parsed.fragment and not parsed.username and parsed.hostname
                and (parsed.scheme == 'https' or (parsed.scheme == 'http'
                     and parsed.hostname in ('localhost', '127.0.0.1', '::1'))))


def start_connection(owner):
    flow = Flow.from_client_config({'web': {
        'client_id': os.environ['GOOGLE_CLIENT_ID'],
        'client_secret': os.environ['GOOGLE_CLIENT_SECRET'],
        'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
        'token_uri': 'https://oauth2.googleapis.com/token',
    }}, scopes=[SCOPE], redirect_uri=os.environ['GOOGLE_REDIRECT_URI'],
        autogenerate_code_verifier=True)
    url, state = flow.authorization_url(access_type='offline', prompt='consent')
    with LOCK:
        now = time.monotonic()
        for key, pending in list(PENDING.items()):
            if now - pending['created'] >= 600:
                del PENDING[key]
        PENDING[owner] = {'state': state, 'created': now, 'flow': flow}
    return url


def finish_connection(owner, state, code, error):
    with LOCK:
        pending = PENDING.get(owner)
        if not pending or not secrets.compare_digest(pending['state'], state):
            raise GoogleVidsError('Google 授權狀態無效，請重新連線')
        del PENDING[owner]  # Consume before token exchange, including denial/failure.
        if time.monotonic() - pending['created'] >= 600:
            raise GoogleVidsError('Google 授權已過期，請重新連線')
        if error or not code:
            raise GoogleVidsError('Google 授權未完成，請重新連線')
        try:
            # Passing code (not the HTTP callback URL) supports localhost without
            # globally disabling oauthlib's secure transport checks.
            pending['flow'].fetch_token(code=code, timeout=30)
            CONNECTIONS[owner] = pending['flow'].credentials
        except Exception:
            raise GoogleVidsError('Google 授權失敗，請重新連線') from None


def forget(credentials):
    with LOCK:
        for owner, current in list(CONNECTIONS.items()):
            if current is credentials:
                del CONNECTIONS[owner]


def access_token(credentials):
    try:
        if not credentials.valid:
            with requests.Session() as session:
                session.trust_env = False
                credentials.refresh(GoogleRequest(session=session))
        return credentials.token
    except Exception:
        forget(credentials)
        raise GoogleVidsError('Google 授權已過期，請重新連線') from None


def revoke(credentials):
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.post('https://oauth2.googleapis.com/revoke',
                data={'token': credentials.refresh_token or credentials.token},
                timeout=30, allow_redirects=False)
            if response.status_code not in (200, 400):
                raise GoogleVidsError('本機已斷線；Google 撤銷失敗，請至 Google 帳號移除授權')
    except requests.RequestException:
        raise GoogleVidsError('本機已斷線；Google 撤銷失敗，請至 Google 帳號移除授權') from None


def _status(code, credentials):
    if code == 401:
        forget(credentials)
        raise GoogleVidsError('Google 授權已過期，請重新連線')
    if code in (403, 404):
        raise GoogleVidsError('無法存取影片或沒有下載權限')
    raise GoogleVidsError('Google 暫時無法取得影片，請稍後重試')


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GoogleVidsError('Google 影片匯出或下載逾時（30 分鐘），請稍後重試')
    return min(30, remaining)


def _json(session, method, url, credentials, deadline, headers=None):
    # Never retry files.download POST: an ambiguous response may have started an
    # operation already. Only metadata/poll GETs can be safely retried.
    for attempt in range(4):
        response = session.request(method, url, headers={
            'Authorization': 'Bearer ' + access_token(credentials), **(headers or {})},
            timeout=_remaining(deadline), allow_redirects=False)
        with response:
            if response.status_code == 200:
                return response.json()
            if method == 'GET' and response.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(min(5, _remaining(deadline)))
                continue
            _status(response.status_code, credentials)


def _download_headers(uri, credentials, resource_headers):
    parsed = urlsplit(uri)
    if (parsed.scheme != 'https' or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment):
        raise GoogleVidsError('Google 回傳未支援的下載位置，請改為手動匯出 MP4 上傳')
    # Bearer is restricted to the documented Drive API origin. Returned signed
    # Google content URLs are fetched without credentials, cookies or redirects.
    if parsed.hostname == 'www.googleapis.com' and parsed.path.startswith('/drive/v3/'):
        return {'Authorization': 'Bearer ' + access_token(credentials), **resource_headers}
    if parsed.hostname == 'drive.usercontent.google.com' or (parsed.hostname or '').endswith('.googleusercontent.com'):
        return {}
    raise GoogleVidsError('Google 回傳未支援的下載位置，請改為手動匯出 MP4 上傳')


def _stream(uri, headers, destination, credentials, deadline):
    """Use an interruptible socket, not an idle-only requests streaming timeout."""
    parsed = urlsplit(uri)
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443,
                                             timeout=_remaining(deadline))
    timer = None
    response = None
    expired = threading.Event()
    try:
        connection.connect()
        transport = connection.sock
        def interrupt():
            expired.set()
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(max(0, deadline - time.monotonic()), interrupt)
        timer.daemon = True
        timer.start()
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        connection.request('GET', path, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            _status(response.status, credentials)
        size = 0
        with destination.open('wb') as target:
            while True:
                transport.settimeout(_remaining(deadline))
                chunk = response.read1(64 * 1024)
                _remaining(deadline)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise GoogleVidsError('影片超過 1.5 GB 上限')
                target.write(chunk)
        if not size:
            raise GoogleVidsError('Google 未回傳有效影片')
    except Exception:
        if expired.is_set():
            raise GoogleVidsError('Google 影片匯出或下載逾時（30 分鐘），請稍後重試') from None
        raise
    finally:
        if timer is not None:
            timer.cancel()
        if response is not None:
            response.close()
        connection.close()


def download_vids(file_id: str, credentials, destination: Path) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9_-]+', file_id):
        raise GoogleVidsError('無效的 Google Vids ID')
    deadline = time.monotonic() + TIMEOUT
    try:
        with requests.Session() as session:
            session.trust_env = False
            metadata = _json(session, 'GET', API + 'files/' + file_id + '?' + urlencode({
                'fields': 'name,mimeType,capabilities(canDownload)', 'supportsAllDrives': 'true'}), credentials, deadline)
            if metadata.get('mimeType') != 'application/vnd.google-apps.vid':
                raise GoogleVidsError('此連結不是 Google Vids 影片')
            if metadata.get('capabilities', {}).get('canDownload') is not True:
                raise GoogleVidsError('無法存取影片或沒有下載權限')
            operation = _json(session, 'POST', API + 'files/' + file_id + '/download?'
                              + urlencode({'mimeType': 'video/mp4'}), credentials, deadline)
            resource_headers = {}
            while True:
                resource_key = operation.get('metadata', {}).get('resourceKey')
                if resource_key:
                    if not re.fullmatch(r'[A-Za-z0-9_-]+', resource_key):
                        raise GoogleVidsError('Google 回傳無效的影片資訊')
                    resource_headers = {'X-Goog-Drive-Resource-Keys': file_id + '/' + resource_key}
                if operation.get('done') is True:
                    break
                name = operation.get('name', '')
                if name.startswith('operations/'):
                    name = name[len('operations/'):]
                if not re.fullmatch(r'[A-Za-z0-9_=-]+', name):
                    raise GoogleVidsError('Google 回傳無效的匯出操作')
                time.sleep(min(5, _remaining(deadline)))
                operation = _json(session, 'GET', API + 'operations/' + name, credentials, deadline, resource_headers)
            if operation.get('error'):
                # LRO errors use google.rpc.Code, not HTTP status codes.
                code = operation['error'].get('code')
                _status({5: 404, 7: 403, 16: 401}.get(code, 503), credentials)
            uri = operation.get('response', {}).get('downloadUri', '')
            headers = _download_headers(uri, credentials, resource_headers)
            # Independent connection: no cookies, netrc, proxies or redirects.
        _stream(uri, headers, destination, credentials, deadline)
        return {'title': metadata.get('name') or 'Google Vids',
                'source_url': f'https://docs.google.com/videos/d/{file_id}/edit'}
    except GoogleVidsError:
        destination.unlink(missing_ok=True)
        raise
    except Exception:
        destination.unlink(missing_ok=True)
        raise GoogleVidsError('Google 暫時無法取得影片，請稍後重試') from None
