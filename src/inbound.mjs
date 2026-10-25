import { timingSafeEqual } from 'node:crypto';

const xml = content => new Response('<?xml version="1.0" encoding="UTF-8"?><Response>'+content+'</Response>',
  {headers:{'Content-Type':'text/xml; charset=utf-8','Cache-Control':'no-store'}});
const escape = value => String(value).replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
const goodbye = message => xml(`<Say>${message}</Say><Hangup/>`);

export async function validateTwilio(request, params, token) {
  const signature = request.headers.get('x-twilio-signature') ?? '';
  if (!token || !/^[A-Za-z0-9+/]{27}=$/.test(signature)) return false;
  // Reject duplicate fields before validation, avoiding ambiguous interpretations.
  if (new Set(params.keys()).size !== [...params.keys()].length) return false;
  const data = request.url + [...params.keys()].sort().map(k => k+params.get(k)).join('');
  const key = await crypto.subtle.importKey('raw',new TextEncoder().encode(token),
    {name:'HMAC',hash:'SHA-1'},false,['verify']);
  return crypto.subtle.verify('HMAC',key,Uint8Array.from(atob(signature),c=>c.charCodeAt(0)),new TextEncoder().encode(data));
}

async function pinMatches(value, env) {
  if (!/^\d{6}$/.test(value ?? '') || !/^[a-f0-9]{64}$/.test(env.INBOUND_PIN_HASH ?? '')) return false;
  const digest = await crypto.subtle.digest('SHA-256',new TextEncoder().encode(env.INBOUND_PIN_SALT+':'+value));
  const actual = Buffer.from(digest);
  return timingSafeEqual(actual,Buffer.from(env.INBOUND_PIN_HASH,'hex'));
}

function tone() {
  const samples=16000, wav=new ArrayBuffer(44+samples*2), v=new DataView(wav);
  const text=(offset,s)=>[...s].forEach((c,i)=>v.setUint8(offset+i,c.charCodeAt(0)));
  text(0,'RIFF');v.setUint32(4,36+samples*2,true);text(8,'WAVEfmt ');
  v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);
  v.setUint32(24,8000,true);v.setUint32(28,16000,true);v.setUint16(32,2,true);v.setUint16(34,16,true);
  text(36,'data');v.setUint32(40,samples*2,true);
  for(let i=0;i<8000;i++) {
    const fade=Math.min(1,i/80,(8000-i)/80);
    v.setInt16(44+i*2,Math.round(1800*fade*(Math.sin(2*Math.PI*440*i/8000)+Math.sin(2*Math.PI*480*i/8000))),true);
  }
  return new Response(wav,{headers:{'Content-Type':'audio/wav','Cache-Control':'public, max-age=86400'}});
}

export async function inbound(request, env, readBody) {
  const url = new URL(request.url);
  if (url.pathname === '/inbound/tone.wav' && request.method === 'GET') return tone();
  if (!['/inbound/voice','/inbound/pin','/inbound/wait','/inbound/status'].includes(url.pathname)) return new Response('Not found',{status:404});
  if (request.method !== 'POST') return new Response('Use POST',{status:405});
  if (url.search || !request.headers.get('content-type')?.startsWith('application/x-www-form-urlencoded')) return new Response('Invalid request',{status:400});
  if (!env.TWILIO_AUTH_TOKEN || !env.INBOUND_PIN_HASH || !env.INBOUND_PIN_SALT) return goodbye('Dot calling is unavailable.');
  const params = new URLSearchParams(await readBody(request,16384));
  if (!await validateTwilio(request,params,env.TWILIO_AUTH_TOKEN)) return new Response('Forbidden',{status:403});
  const sid=params.get('CallSid');
  if (params.get('AccountSid') !== env.TWILIO_ACCOUNT_SID || params.get('From') !== env.INBOUND_OWNER_NUMBER
      || params.get('To') !== env.INBOUND_TWILIO_NUMBER || !/^CA[0-9a-f]{32}$/.test(sid ?? '')) return xml('<Reject reason="rejected"/>');
  const owner=env.OWNER_CALLS.getByName('owner');
  const fields={inbound_call_sid:sid};
  if (url.pathname === '/inbound/status') {
    await owner.perform('inbound_status',{...fields,status:params.get('CallStatus')});
    return new Response('ok');
  }
  if (!['ringing','in-progress'].includes(params.get('CallStatus'))) return xml('<Hangup/>');
  let result;
  if (url.pathname === '/inbound/voice') result=await owner.perform('inbound_open',fields);
  else if (url.pathname === '/inbound/pin') result=await owner.perform('inbound_pin',
    {...fields,pin_ok:await pinMatches(params.get('Digits'),env)});
  else result=await owner.perform('inbound_wait',fields);
  const origin=url.origin;
  if (result.status === 'pin_required') return xml(
    `<Gather input="dtmf" numDigits="6" timeout="10" actionOnEmptyResult="true" method="POST" action="${origin}/inbound/pin"><Say>Enter your six digit Dot calling PIN.</Say></Gather><Hangup/>`);
  if (result.status === 'waiting') return xml(
    `<Play>${origin}/inbound/tone.wav</Play><Redirect method="POST">${origin}/inbound/wait</Redirect>`);
  if (result.status === 'ready') return xml(`<Connect><Stream url="${escape(result.stream_url)}"/></Connect><Hangup/>`);
  if (result.status === 'offline') return goodbye('Your Mac is offline. Please open Chrome on your Mac and try again.');
  if (result.status === 'busy') return goodbye('Dot is already on a call. Please try again later.');
  if (result.status === 'denied') return goodbye('Access denied. Goodbye.');
  return goodbye('Dot could not connect. Please try again later.');
}
