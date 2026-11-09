"""Bounded multipart parsing without pre-spooling an untrusted request body."""

import asyncio
from pathlib import Path
import re

from fastapi import HTTPException, Request
from python_multipart import MultipartParser
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect


async def run_upload_io(func, *args):
    # Do not close/delete media while a parser write or probe still uses it.
    work = asyncio.create_task(run_in_threadpool(func, *args))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        try:
            await work
        finally:
            raise


async def receive_upload(request: Request, path: Path, limit: int, overhead: int,
                         idle_seconds: float) -> tuple[str, dict]:
    content_type, params = parse_options_header(request.headers.get('content-type', ''))
    boundary = params.get(b'boundary', b'')
    if content_type != b'multipart/form-data' or not boundary or len(boundary) > 200:
        raise HTTPException(400, '請使用 multipart/form-data 上傳一個 MP4')
    length = request.headers.get('content-length')
    if length:
        try:
            size = int(length)
        except ValueError:
            raise HTTPException(400, '無效的 Content-Length')
        if size < 0:
            raise HTTPException(400, '無效的 Content-Length')
        if size > limit + overhead:
            raise HTTPException(413, '上傳請求超過大小上限')

    fields, headers = {}, {}
    name = filename = None
    header_name, header_value, value = bytearray(), bytearray(), bytearray()
    file_size = request_size = header_size = parts = 0
    is_file = file_seen = complete = False
    allowed = {'threshold', 'min_gap', 'max_frames', 'summary', 'summary_language', 'translate'}

    def part_begin():
        nonlocal header_size, parts
        parts += 1
        if parts > 7:
            raise HTTPException(400, '只允許一個檔案及指定的處理參數')
        headers.clear()
        value.clear()
        header_size = 0

    def header_field(data, start, end):
        nonlocal header_size
        header_size += end - start
        if header_size > 8192:
            raise HTTPException(400, '表單標頭過長')
        header_name.extend(data[start:end])

    def header_data(data, start, end):
        nonlocal header_size
        header_size += end - start
        if header_size > 8192:
            raise HTTPException(400, '表單標頭過長')
        header_value.extend(data[start:end])

    def header_end():
        key = bytes(header_name).lower()
        if key in headers:
            raise HTTPException(400, '重複的表單標頭')
        headers[key] = bytes(header_value)
        header_name.clear()
        header_value.clear()

    def headers_finished():
        nonlocal name, filename, is_file, file_seen
        disposition, options = parse_options_header(headers.get(b'content-disposition', b''))
        if disposition != b'form-data':
            raise HTTPException(400, '無效的表單內容')
        name = options.get(b'name', b'').decode('utf-8')
        is_file = b'filename' in options
        if is_file:
            if name != 'file' or file_seen:
                raise HTTPException(400, '只能上傳一個 file 檔案')
            file_seen = True
            raw_name = options[b'filename'].decode('utf-8')
            filename = re.split(r'[/\\]', raw_name)[-1]
            filename = ''.join(c for c in filename if c.isprintable())[:200]
            if not filename:
                raise HTTPException(400, '請選擇影片檔案')
        elif name not in allowed or name in fields:
            raise HTTPException(400, '不支援或重複的處理參數')

    with path.open('wb') as output:
        def part_data(data, start, end):
            nonlocal file_size
            if is_file:
                file_size += end - start
                if file_size > limit:
                    raise HTTPException(413, 'MP4 超過 1.5 GB 大小上限')
                output.write(data[start:end])
            else:
                if len(value) + end - start > 1024:
                    raise HTTPException(400, '處理參數過長')
                value.extend(data[start:end])

        def part_end():
            if not is_file:
                fields[name] = value.decode('utf-8')

        def end():
            nonlocal complete
            complete = True

        parser = MultipartParser(boundary, {
            'on_part_begin': part_begin, 'on_header_field': header_field,
            'on_header_value': header_data, 'on_header_end': header_end,
            'on_headers_finished': headers_finished, 'on_part_data': part_data,
            'on_part_end': part_end, 'on_end': end})
        stream = request.stream().__aiter__()
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(anext(stream), timeout=idle_seconds)
                except StopAsyncIteration:
                    break
                request_size += len(chunk)
                if request_size > limit + overhead:
                    raise HTTPException(413, '上傳請求超過大小上限')
                for offset in range(0, len(chunk), 1024 * 1024):
                    await run_upload_io(parser.write, chunk[offset:offset + 1024 * 1024])
            # parser.finalize currently does not verify the terminating boundary.
            if not complete or not file_seen or not file_size:
                raise HTTPException(400, '上傳不完整或沒有影片檔案')
        except TimeoutError:
            raise HTTPException(408, '上傳逾時：60 秒未收到資料，請重新上傳')
        except (ClientDisconnect, MultipartParseError, UnicodeError, ValueError):
            raise HTTPException(400, '上傳中斷或表單內容無效')
    return filename, fields
