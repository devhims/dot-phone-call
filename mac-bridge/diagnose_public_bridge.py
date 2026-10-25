"""Exercise the real public WebSocket probe without audio, credentials, or dialing."""
import argparse,asyncio,json,re,time
from pathlib import Path
from aiohttp import web
import aiohttp,ssl,certifi
from bridge import Session
ROOT=Path(__file__).resolve().parents[1]
class NoAudio:
    def start(self): raise AssertionError('Diagnostic must not start audio')
    def close(self): pass
    def metrics(self): return {}
async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--system-dns',action='store_true',help='Reproduce the old system-only DNS path')
    args=parser.parse_args()
    status=json.loads((ROOT/'.runtime/mac-companion-status.json').read_text())
    assert not status['busy'] and not status['cleanup_required'] and time.time()-status['updated_at']<15
    state=Session('https://example.invalid','diagnostic','diagnostic',NoAudio())
    runner=web.AppRunner(state.app(),access_log=None);await runner.setup()
    await web.TCPSite(runner,'127.0.0.1',8891).start()
    process=None;drain=None;events=[];stopping=False;start=time.monotonic()
    def event(**fields):
        item={'seconds':round(time.monotonic()-start,2),**fields};events.append(item);print(json.dumps(item),flush=True)
    try:
        process=await asyncio.create_subprocess_exec(str(ROOT/'.runtime/bin/cloudflared'),'tunnel','--url','http://127.0.0.1:8891','--no-autoupdate',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
        async with asyncio.timeout(40):
            while True:
                line=await process.stdout.readline()
                if not line: raise RuntimeError('Tunnel exited before URL')
                match=re.search(rb'https://[a-z0-9-]+\.trycloudflare\.com',line)
                if match:
                    state.origin=match.group().decode();event(stage='url_announced');break
        async def drain_logs():
            while line:=await process.stdout.readline():
                if b'Registered tunnel connection' in line:event(stage='tunnel_registered')
                elif b' ERR ' in line and not stopping:event(stage='tunnel_error',detail=line.decode(errors='replace').strip()[:500])
        drain=asyncio.create_task(drain_logs())
        context=ssl.create_default_context(cafile=certifi.where())
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=context)) as client:
            class ObservedClient:
                def ws_connect(self,*args,**kwargs):
                    original=client.ws_connect(*args,**kwargs)
                    class Connection:
                        async def __aenter__(self):
                            try:return await original.__aenter__()
                            except Exception as error:
                                event(stage='probe_error',error_type=type(error).__name__,http_status=getattr(error,'status',None),errno=getattr(getattr(error,'os_error',None),'errno',None))
                                raise
                        async def __aexit__(self,*args):return await original.__aexit__(*args)
                    return Connection()
            try:
                await state.verify_public_connection(ObservedClient() if args.system_dns else None)
                event(stage='public_verified')
            except Exception as error:
                event(stage='failed',code=str(error))
                from urllib.parse import urlsplit
                host=urlsplit(state.origin).hostname
                for resolver in ([],['@1.1.1.1']):
                    query=await asyncio.create_subprocess_exec('/usr/bin/dig',*resolver,'+time=2','+tries=1','+short',host,'A',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                    answer,_=await query.communicate()
                    event(stage='direct_dns',resolver='configured' if not resolver else 'cloudflare',answer=answer.decode().strip()[:300])
                try:
                    addresses=await asyncio.get_running_loop().getaddrinfo(host,443)
                    event(stage='os_dns_recheck',address_count=len(addresses))
                except Exception as error:event(stage='os_dns_recheck',error_type=type(error).__name__,errno=getattr(error,'errno',None))
    finally:
        stopping=True
        if process and process.returncode is None:
            process.terminate()
            try:await asyncio.wait_for(process.wait(),5)
            except asyncio.TimeoutError:process.kill();await process.wait()
        if drain:await drain
        await runner.cleanup()
        p=ROOT/'.runtime/tunnel-diagnostic.json';p.write_text(json.dumps({'events':events,'dialed':False},indent=2)+'\n');p.chmod(0o600)
if __name__=='__main__':asyncio.run(main())
