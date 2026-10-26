"""One Chrome-owned operation per process. stdout is native messaging only."""
import asyncio
import contextlib
import fcntl
import json
import os
import re
import signal
import struct
import sys
import time
import uuid

from companion import Companion, ROOT, RUNTIME, SNAPSHOT, verify_local_audio
import aiohttp
import certifi
import ssl

MAX_FRAME = 8192


def encode_frame(message):
    body = json.dumps(message, separators=(',', ':')).encode()
    if len(body) > MAX_FRAME:
        raise ValueError('NATIVE_MESSAGE_TOO_LARGE')
    return struct.pack('=I', len(body)) + body


async def read_frame(reader):
    header = await reader.readexactly(4)
    length = struct.unpack('=I', header)[0]
    if not 0 < length <= MAX_FRAME:
        raise ValueError('INVALID_NATIVE_MESSAGE_SIZE')
    value = json.loads(await reader.readexactly(length))
    if not isinstance(value, dict):
        raise ValueError('INVALID_NATIVE_MESSAGE')
    return value


def validate_request(request):
    if request.get('type') != 'run' or request.get('mode') not in ('audio-test', 'route-test', 'prepare-test', 'hidden-audio-test', 'call', 'recover'):
        raise ValueError('INVALID_NATIVE_OPERATION')
    uuid.UUID(request['instance'])
    if request.get('backend_origin') not in (None,
            'http://127.0.0.1:8894'):
        raise ValueError('UNEXPECTED_BACKEND_ORIGIN')
    if request['mode'] == 'call':
        job = request['job']
        uuid.UUID(job['id'])
        duration = job.get('duration_seconds')
        if type(duration) is not int or not 20 <= duration <= 600:
            raise ValueError('INVALID_CALL_DURATION')
        direction = job.get('direction', 'outbound')
        if direction not in ('inbound', 'outbound'):
            raise ValueError('INVALID_CALL_DIRECTION')
        if direction == 'inbound':
            if not re.fullmatch(r'CA[0-9a-f]{32}', job.get('inbound_call_sid', '')):
                raise ValueError('INVALID_INBOUND_CALL')
        elif job.get('inbound_call_sid') is not None:
            raise ValueError('INVALID_INBOUND_CALL')
    return request


class NativeBrowser:
    def __init__(self, send):
        self.closed = False
        self.send = send

    async def send_json(self, value):
        if self.closed:
            raise RuntimeError('BROWSER_DISCONNECTED')
        self.send(value)


async def run_operation(config, request, reader, send):
    # Bind each operation to the queue selected by its paired extension. Old
    # extensions keep using the hosted backend until explicitly reloaded.
    if request.get('backend_origin'):
        config = {**config, 'origin': request['backend_origin']}
    companion = Companion(config)
    companion.instance = request['instance']
    # Experiment: per-tab/per-page audio routing instead of system default devices.
    companion.direct_audio = request.get('direct_audio') is True
    companion.browser = NativeBrowser(send)
    companion.cleanup_failed = SNAPSHOT.exists()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, companion.shutdown.set)
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    companion.http = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context),
        timeout=aiohttp.ClientTimeout(total=8))
    started = time.monotonic()
    mode = request['mode']
    finished = False
    result = {'mode': mode, 'dialed': False, 'verified': False}

    async def listen():
        try:
            while True:
                data = await asyncio.wait_for(read_frame(reader), 15)
                if data.get('type') == 'ready':
                    companion.browser_version = data.get('version')
                    companion.browser_ready = data.get('ok') is True and data.get('version') in ('0.1.7', '0.1.8', '0.1.9', '0.2.0')
                elif data.get('type') == 'result':
                    future = companion.pending.get(data.get('id'))
                    if future and not future.done():
                        future.set_result(data)
                elif data.get('type') == 'cancel':
                    companion.shutdown.set()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError):
            companion.browser.closed = True
            for future in companion.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError('BROWSER_DISCONNECTED'))
            companion.shutdown.set()

    listener = asyncio.create_task(listen())
    send({'type': 'authenticated', 'pid': os.getpid(), 'mode': mode})
    try:
        deadline = time.monotonic() + 25
        while not companion.browser_ready:
            if companion.shutdown.is_set() or time.monotonic() > deadline:
                raise RuntimeError('NATIVE_BROWSER_NOT_READY')
            await asyncio.sleep(.05)
        if SNAPSHOT.exists():
            await companion.recover_audio()
            if companion.cleanup_failed:
                raise RuntimeError('PREVIOUS_AUDIO_RESTORE_REQUIRED')
        if mode == 'recover':
            result.update(verified=True)
        elif mode == 'hidden-audio-test':
            from offscreen_test import test_offscreen_audio
            result.update(await test_offscreen_audio(companion))
        elif mode == 'prepare-test':
            prepared = await companion.command('prepare', diagnostic_new_tab=True)
            result.update(verified=prepared.get('ready') is True, tab_id=prepared.get('tab_id'), version=prepared.get('version'))
        elif mode == 'route-test':
            result.update(levels=await verify_local_audio(), verified=True)
        else:
            if mode == 'audio-test':
                job = {'id': 'audio-test-' + str(uuid.uuid4())}
                operation = companion.test_browser_audio(job['id'])
            else:
                job = request['job']
                operation = companion.call(job)
            companion.current = job
            companion.last_cloud_ok = time.monotonic()
            companion.task = asyncio.create_task(operation)
            deadline = time.monotonic() + (180 if mode == 'audio-test' else job['duration_seconds'] + 240)
            while not companion.task.done():
                if companion.shutdown.is_set() or time.monotonic() > deadline:
                    companion.stop_call()
                    break
                # Diagnostics never contact the call queue, claim jobs, open a
                # public tunnel, or invoke the telephone bridge.
                if mode == 'call':
                    try:
                        while companion.reports:
                            await companion.cloud('update', companion.reports[0])
                            companion.reports.pop(0)
                        state = await companion.cloud('pulse', {'ready': False, 'busy': True, 'audio_guard_version': 1})
                        if state.get('cancel_call_id') == job['id']:
                            companion.stop_call('phone_canceled')
                    except Exception:
                        if time.monotonic() - companion.last_cloud_ok > 20:
                            companion.stop_call()
                send({'type': 'heartbeat'})
                try:
                    await asyncio.wait_for(asyncio.shield(companion.task), 2)
                except asyncio.TimeoutError:
                    pass
            with contextlib.suppress(asyncio.CancelledError):
                await companion.task
            if mode == 'audio-test':
                result.update(json.loads((RUNTIME/'browser-audio-test-result.json').read_text()))
            else:
                result = {'mode': mode, 'finished': True}
        finished = True
    except Exception as error:
        result['code'] = str(error) if str(error).replace('_', '').isupper() else 'NATIVE_HELPER_FAILED'
    finally:
        companion.stop_call()
        if companion.task:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await companion.task
        for report in companion.reports:
            with contextlib.suppress(Exception):
                await companion.cloud('update', report)
        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener
        await companion.http.close()
        result.update(type='complete', helper_pid=os.getpid(), audio_restored=not SNAPSHOT.exists(),
            cleanup_ok=not companion.cleanup_failed and not SNAPSHOT.exists(),
            elapsed_seconds=round(time.monotonic()-started, 2), finished=finished)
        path = RUNTIME/'native-helper-result.json'
        path.write_text(json.dumps(result)); path.chmod(0o600)
        with contextlib.suppress(BrokenPipeError):
            send(result)
    return result


async def main():
    config = json.loads((ROOT/'.secrets/mac-companion.json').read_text())
    if len(sys.argv) != 2 or sys.argv[1] != 'chrome-extension://' + config['extension_id'] + '/':
        raise ValueError('UNAUTHORIZED_NATIVE_ORIGIN')
    if config['origin'] != 'http://127.0.0.1:8894':
        raise ValueError('UNEXPECTED_CLOUD_ORIGIN')
    # Redirect fd 1 as well as Python prints so routing subprocesses cannot
    # corrupt Chrome's protocol. Retain a separate descriptor for frames.
    output = os.fdopen(os.dup(sys.stdout.fileno()), 'wb', buffering=0)
    logfile = (RUNTIME/'native-helper.log').open('a')
    os.chmod(logfile.name, 0o600)
    os.dup2(logfile.fileno(), 1)
    os.dup2(logfile.fileno(), 2)
    def send(message):
        output.write(encode_frame(message))
    lock = (RUNTIME/'mac-companion.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    reader = asyncio.StreamReader(limit=MAX_FRAME)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    try:
        request = validate_request(await asyncio.wait_for(read_frame(reader), 10))
        await run_operation(config, request, reader, send)
    finally:
        transport.close()
        lock.close()


if __name__ == '__main__':
    asyncio.run(main())
