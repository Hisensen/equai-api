"""Concurrent browser chunks must never be assembled before every write finishes."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.requests import Request

import transfer_api


def request_for(part: int, chunks, *, total: int) -> Request:
    iterator = iter(chunks)

    async def receive():
        item = next(iterator)
        if isinstance(item, asyncio.Event):
            await item.wait()
            item = next(iterator)
        return {"type": "http.request", "body": item[0], "more_body": item[1]}

    return Request({
        "type": "http", "method": "PUT", "path": "/internal/uploads",
        "query_string": f"part={part}&parts=2".encode(),
        "headers": [(b"content-type", b"image/png"),
                    (b"content-length", str(6 if part == 0 else 4).encode())],
    }, receive)


@pytest.mark.asyncio
async def test_chunk_assembly_waits_for_completed_parallel_writes():
    with tempfile.TemporaryDirectory() as folder:
        target = Path(folder) / "image.png"
        grant = {"content_type": "image/png", "size_bytes": 10, "chunk_bytes": 6, "key": "test"}
        release = asyncio.Event()
        first_half_written = asyncio.Event()

        async def slow_receive():
            first_half_written.set()
            return {"type": "http.request", "body": b"ABC", "more_body": True}

        first = request_for(0, [(b"DEF", False)], total=10)
        original_receive = first._receive
        called = False

        async def receive_first():
            nonlocal called
            if not called:
                called = True
                return await slow_receive()
            await release.wait()
            return await original_receive()

        first._receive = receive_first
        second = request_for(1, [(b"GHIJ", False)], total=10)
        with patch.object(transfer_api, "_browser_grant", return_value=grant), \
             patch.object(transfer_api, "browser_upload_path", return_value=target):
            first_task = asyncio.create_task(transfer_api.browser_upload(first, "grant"))
            await first_half_written.wait()
            try:
                response = await transfer_api.browser_upload(second, "grant")
                assert response.status_code == 204
                assert not target.exists()
            finally:
                release.set()
            response = await first_task
            assert response.status_code == 204
            assert target.read_bytes() == b"ABCDEFGHIJ"
