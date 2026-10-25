"""Create and verify a temporary public route before opening browser voice."""
import asyncio
import re
import secrets
import time
from pathlib import Path
from dataclasses import dataclass
from aiohttp import web
from bridge import Session,log

ROOT=Path(__file__).resolve().parents[1]

async def verify_public_bridge(origin,port=8891):
    state=Session(origin,'connectivity-probe',secrets.token_hex(24),None)
    app=web.Application()
    app.router.add_get('/probe/'+state.nonce,state.probe)
    runner=web.AppRunner(app,access_log=None)
    try:
        await runner.setup()
        await web.TCPSite(runner,'127.0.0.1',port).start()
        await state.verify_public_connection()
    finally:
        await runner.cleanup()

@dataclass
class PublicTunnel:
    process: object
    origin: str=None
    drain: object=None
    attempt: int=1
    setup_seconds: float=0

    async def close(self):
        if self.process.returncode is None:
            self.process.terminate()
            try:await asyncio.wait_for(self.process.wait(),5)
            except asyncio.TimeoutError:
                self.process.kill();await self.process.wait()
        if self.drain is not None:
            await self.drain

async def open_verified_tunnel(port=8891):
    started=time.monotonic()
    for attempt in range(2):
        process=await asyncio.create_subprocess_exec(str(ROOT/'.runtime/bin/cloudflared'),
            'tunnel','--url',f'http://127.0.0.1:{port}','--no-autoupdate',
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
        tunnel=PublicTunnel(process,attempt=attempt+1)
        try:
            registered=False
            async with asyncio.timeout(40):
                while not tunnel.origin or not registered:
                    line=await process.stdout.readline()
                    if not line:raise RuntimeError('TUNNEL_EXITED')
                    match=re.search(rb'https://[a-z0-9-]+\.trycloudflare\.com',line)
                    if match:tunnel.origin=match.group().decode()
                    if b'Registered tunnel connection' in line:registered=True
            async def drain_logs():
                while await process.stdout.readline():pass
            tunnel.drain=asyncio.create_task(drain_logs())
            await verify_public_bridge(tunnel.origin,port)
            tunnel.setup_seconds=round(time.monotonic()-started,2)
            log('public_tunnel_ready',attempt=tunnel.attempt,setup_seconds=tunnel.setup_seconds)
            return tunnel
        except BaseException as error:
            await tunnel.close()
            # Retry only infrastructure DNS setup, before browser/audio/dialing.
            # There is never a retry after a phone call has been created.
            if (attempt==0 and str(error)=='PUBLIC_BRIDGE_UNREACHABLE'
                    and getattr(error,'diagnostics',{}).get('reason')=='dns_lookup_failed'):
                log('public_tunnel_replaced',reason='dns_lookup_failed')
                continue
            raise
