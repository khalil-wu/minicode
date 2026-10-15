"""Actual local sockets for Guardian request, drain and cancellation evidence."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace


@asynccontextmanager
async def http_endpoint(respond):
    requests, errors, tasks, writers = [], [], set(), set()
    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        writers.add(writer)
        request = None
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            method, path, _ = head[0].split(" ")
            headers = dict((key.lower(), value) for key, value in (line.split(": ", 1) for line in head[1:] if ": " in line))
            body = await reader.readexactly(int(headers.get("content-length", 0)))
            request = SimpleNamespace(method=method, path=path, headers=headers,
                body=json.loads(body) if body else None, closed=False, index=len(requests) + 1)
            requests.append(request)
            await respond(request, reader, writer)
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as error:
            errors.append(error)
        finally:
            if request is not None:
                request.closed = True
            writer.close()
            writers.discard(writer)
            tasks.discard(task)
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    fixture = SimpleNamespace(url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", requests=requests, errors=errors)
    try:
        yield fixture
    finally:
        server.close()
        await server.wait_closed()
        for writer in tuple(writers):
            writer.close()
        pending = tuple(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        assert not errors, errors


async def send_json(writer, body, status=200):
    encoded = json.dumps(body).encode()
    writer.write(f"HTTP/1.1 {status} Response\r\nContent-Type: application/json\r\nContent-Length: {len(encoded)}\r\nConnection: close\r\n\r\n".encode() + encoded)
    await writer.drain()


async def send_sse(writer, events, *, gate=None, reader=None):
    parts = [f"data: {json.dumps(event)}\n\n".encode() for event in events]
    writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {sum(map(len, parts))}\r\nConnection: close\r\n\r\n".encode())
    if gate is not None:
        writer.write(parts.pop(0))
        await writer.drain()
        released = asyncio.create_task(gate.wait())
        closed = asyncio.create_task(reader.read())
        try:
            done, _ = await asyncio.wait((released, closed), return_when=asyncio.FIRST_COMPLETED)
            if closed in done:
                return
        finally:
            released.cancel()
            closed.cancel()
            await asyncio.gather(released, closed, return_exceptions=True)
    writer.write(b"".join(parts))
    await writer.drain()


def classifier_events(index, *, choice="low", encrypted=True, final=None):
    items = []
    if encrypted:
        items.append({"type": "reasoning", "id": f"rs_actual_{index}", "summary": [], "encrypted_content": f"opaque actual reasoning {index}"})
    items.append({"type": "message", "id": f"msg_actual_{index}", "role": "assistant",
        "content": [{"type": "output_text", "text": final if final is not None else choice}]})
    return [{"type": "response.output_text.delta", "delta": choice},
        *({"type": "response.output_item.done", "item": item} for item in items),
        {"type": "response.completed", "response": {"id": f"resp_actual_{index}", "output": items,
            "usage": {"input_tokens": index * 10, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 3}}}}]


async def until(predicate):
    async with asyncio.timeout(4):
        while not predicate():
            await asyncio.sleep(.001)
