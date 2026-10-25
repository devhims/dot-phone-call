"""DNS fallback scoped to one generated Quick Tunnel hostname."""
import ipaddress
import json
import re
import socket
import ssl
import aiohttp
from aiohttp.abc import AbstractResolver
import certifi

class TunnelResolver(AbstractResolver):
    def __init__(self,host):
        self.host=host if re.fullmatch(r'[a-z0-9-]+\.trycloudflare\.com',host or '') else None
        self.system=aiohttp.ThreadedResolver()
        self.fallback_count=0

    async def lookup(self,host,record_type):
        context=ssl.create_default_context(cafile=certifi.where())
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=context),
                timeout=aiohttp.ClientTimeout(total=2)) as client:
            async with client.get('https://cloudflare-dns.com/dns-query',
                    params={'name':host,'type':record_type},
                    headers={'Accept':'application/dns-json'},allow_redirects=False) as response:
                response.raise_for_status()
                body=await response.content.read(16385)
                if len(body)>16384:raise ValueError('DNS response too large')
                return json.loads(body)

    async def resolve(self,host,port=0,family=socket.AF_INET):
        try:
            return await self.system.resolve(host,port,family)
        except socket.gaierror as original:
            if host!=self.host:raise
            try:
                record_type=28 if family==socket.AF_INET6 else 1
                answer=await self.lookup(host,record_type)
                addresses=[]
                if isinstance(answer,dict) and answer.get('Status')==0 and isinstance(answer.get('Answer'),list):
                    for entry in answer['Answer'][:64]:
                        if not isinstance(entry,dict):continue
                        if entry.get('type')!=record_type or entry.get('name','').rstrip('.').lower()!=host:continue
                        address=ipaddress.ip_address(entry['data'])
                        if not address.is_global or address.version!=(6 if record_type==28 else 4):continue
                        addresses.append({'hostname':host,'host':str(address),'port':port,
                            'family':socket.AF_INET6 if address.version==6 else socket.AF_INET,
                            'proto':socket.IPPROTO_TCP,'flags':socket.AI_NUMERICHOST})
                if addresses:
                    self.fallback_count+=1
                    return addresses
            except (aiohttp.ClientError,OSError,ValueError,KeyError,TypeError):
                pass
            raise original

    async def close(self):
        await self.system.close()
