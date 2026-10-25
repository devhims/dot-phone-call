"""Exercise the installed permission-owning helper without a browser or phone call."""
import asyncio
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'mac-bridge'))
from native_host import encode_frame, read_frame


async def main():
    if (ROOT/'.runtime/mac-audio-original.json').exists():
        raise RuntimeError('Restore the previous audio session before this test')
    config = json.loads((ROOT/'.secrets/mac-companion.json').read_text())
    binary = ROOT/'.runtime/Dot Phone Call Helper.app/Contents/MacOS/DotPhone'
    process = await asyncio.create_subprocess_exec(str(binary),
        'chrome-extension://'+config['extension_id']+'/',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    async def send(value):
        process.stdin.write(encode_frame(value))
        await process.stdin.drain()
    try:
        async with asyncio.timeout(25):
            await send({'type':'run','mode':'route-test','instance':str(uuid.uuid4())})
            result = None
            while result is None:
                message = await read_frame(process.stdout)
                if message['type'] == 'authenticated':
                    await send({'type':'ready','ok':True,'version':'0.1.7'})
                elif message['type'] == 'complete':
                    result = message
                elif message['type'] == 'command':
                    raise RuntimeError('Route test unexpectedly requested browser controls')
            await process.wait()
            assert process.returncode == 0
            assert result['verified'] and result['cleanup_ok'] and result['dialed'] is False
            print(json.dumps(result, indent=2))
    finally:
        if process.returncode is None:
            process.terminate()
            try: await asyncio.wait_for(process.wait(), 10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


if __name__ == '__main__':
    asyncio.run(main())
