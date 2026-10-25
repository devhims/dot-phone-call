import {readFile} from 'node:fs/promises';
import {createServer} from 'node:http';
import {timingSafeEqual} from 'node:crypto';
import {StreamableHTTPServerTransport} from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import {z} from 'zod';
import {makeMcpServer} from './tools.mjs';
import {inbound} from './inbound.mjs';

async function readBody(req,limit=65536) {
  const chunks=[];let length=0;
  for await(const chunk of req) {
    length+=chunk.length;if(length>limit) throw new Error('BODY_LIMIT');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString('utf8');
}
const sameSecret=(header,token)=>{
  const a=Buffer.from(header??''),b=Buffer.from('Bearer '+token);
  return a.length===b.length && timingSafeEqual(a,b);
};
const instance=z.string().regex(/^[a-zA-Z0-9-]{16,64}$/);
const companionSchemas={
  pulse:z.object({instance,ready:z.boolean(),busy:z.boolean().optional(),audio_guard_version:z.literal(1).optional()}).strict(),
  update:z.object({instance,call_id:z.string().uuid(),status:z.enum(['starting','ringing','connected','completed','failed','canceled']),code:z.string().regex(/^[A-Z_]{1,60}$/).optional(),diagnostics:z.record(z.unknown()).optional()}).strict(),
  'inbound-ready':z.object({instance,call_id:z.string().uuid(),stream_url:z.string().max(300)}).strict(),
};

export function createLocalBackend({queue,mcpToken,companionToken,extensionId,publicOrigin,twilio,readStatus,callingEnabled=false,registered=null,readAudioDiagnostic=async()=>null}) {
  for(const token of [mcpToken,companionToken]) if(typeof token!=='string'||token.length<32) throw new Error('Invalid local credential');
  if(!/^[a-p]{32}$/.test(extensionId)) throw new Error('Invalid paired extension');
  const external=new URL(publicOrigin??'https://unused.invalid');
  if(external.protocol!=='https:'||(publicOrigin&&external.origin!==publicOrigin)||external.username||external.password) throw new Error('Invalid public origin');
  const nativeStatus=()=>({...queue.perform('status'),...(registered?{registered_client:registered.status(),reason:'The helper checks local audio and the registered Twilio client before dialing. Dot voice starts when the phone answers; incoming calls require the saved caller number and PIN.'}:{}),...((registered&&(!registered.registered()||registered.status().recovery_required))?{can_request_native_dot_call:false,reason:'The local Twilio browser client is not registered.'}:{}),...((registered&&registered.status().microphone_ready===false)?{can_request_native_dot_call:false,reason:'Enable microphone access in the Dot Phone extension setup page.'}:{}),...(!callingEnabled?{can_request_native_dot_call:false,reason:'Local migration checks are running; calling is disabled.'}:{}),plugin_transport:'secure-mcp-tunnel',backend:'local',
    fallback_policy:'Explicit hosted fallback only. Never retry an uncertain call with a new request_key.'});
  const env={...twilio,OWNER_CALLS:{getByName:()=>({perform:(action,input)=>
    !callingEnabled&&['inbound_open','inbound_pin'].includes(action)
      ? {status:'offline'} : queue.perform(action,input)})}};
  const send=(res,status,value)=>{res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(value));};
  const privateServer=createServer(async(req,res)=>{
    try {
      if(req.headers.host!==`127.0.0.1:${privateServer.address().port}`) return send(res,403,{error:'Host rejected'});
      const path=req.url;
      if(registered&&path.startsWith('/bridge/')) {
        if(req.method==='GET') {
          const assets={'/bridge/':'../browser/index.html','/bridge/app.js':'../browser/app.js','/bridge/session.js':'../browser/session.js','/bridge/diagnostic.js':'../browser/diagnostic.js','/bridge/phone-voice.js':'../browser/phone-voice.js','/bridge/phone-voice-worklet.js':'../browser/phone-voice-worklet.js','/bridge/pcm-worklet.js':'../prototype/registered/pcm-worklet.js','/bridge/twilio.min.js':'../node_modules/@twilio/voice-sdk/dist/twilio.min.js'};
          if(!assets[path])return send(res,404,{error:'Not found'});
          const data=await readFile(new URL(assets[path],import.meta.url));
          res.writeHead(200,{'Content-Type':path.endsWith('.js')?'text/javascript':'text/html','Cache-Control':'no-store','Referrer-Policy':'no-referrer','Content-Security-Policy':"frame-ancestors 'none'; object-src 'none'; base-uri 'none'"});res.end(data);return;
        }
        const pairedOrigin='chrome-extension://'+extensionId;
        const origin=req.headers.origin;
        if(origin===pairedOrigin){res.setHeader('Access-Control-Allow-Origin',pairedOrigin);res.setHeader('Vary','Origin');}
        if(req.method==='OPTIONS'&&origin===pairedOrigin&&req.headers['access-control-request-method']==='POST'){
          res.writeHead(204,{'Access-Control-Allow-Methods':'POST','Access-Control-Allow-Headers':'Authorization, Content-Type'});res.end();return;
        }
        if(req.method!=='POST'||![`http://127.0.0.1:${privateServer.address().port}`,pairedOrigin].includes(origin)||!sameSecret(req.headers.authorization,registered.capability))return send(res,403,{error:'Bridge rejected'});
        let value;
        try {value=JSON.parse(await readBody(req,8192));}catch{return send(res,400,{error:'Invalid JSON'});}
        try {
          if(path==='/bridge/events') {
            await registered.browser('events',value);
            res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-store','X-Accel-Buffering':'no'});
            res.write('data: tick\n\n');
            const timer=setInterval(()=>{if(!res.destroyed)res.write('data: tick\n\n');},2000);
            res.on('close',()=>clearInterval(timer));return;
          }
          return send(res,200,await registered.browser(path.slice('/bridge/'.length),value));
        }
        catch(error){return send(res,409,{error:/^[A-Z_]{1,80}$/.test(error.message)?error.message:'REGISTERED_REQUEST_FAILED'});}
      }

      const companion=path.startsWith('/companion/');
      if(req.headers.origin && !(companion&&req.headers.origin==='chrome-extension://'+extensionId)) return send(res,403,{error:'Origin rejected'});
      if(path!=='/mcp'&&!companion) return send(res,404,{error:'Not found'});
      if(!sameSecret(req.headers.authorization,companion?companionToken:mcpToken)) return send(res,401,{error:'Unauthorized'});
      if(req.method!=='POST') return send(res,405,{error:'Use POST'});
      if(!req.headers['content-type']?.startsWith('application/json')) return send(res,415,{error:'JSON required'});
      let parsed;
      try {parsed=JSON.parse(await readBody(req,companion?8192:65536));} catch {return send(res,400,{error:'Invalid or oversized JSON'});}
      if(companion) {
        const action=path.slice('/companion/'.length);
        // Local menu-bar health check: authenticated, read-only, no provider request.
        if(action==='service-status') {
          if(!z.object({}).strict().safeParse(parsed).success)return send(res,400,{error:'Invalid status request'});
          return send(res,200,nativeStatus());
        }
        if(registered&&action.startsWith('registered-')) {
          if(!instance.safeParse(parsed.instance).success)return send(res,400,{error:'Invalid instance'});
          try {
            if(action==='registered-finish') {
              if(queue.perform('get',{call_id:parsed.call_id}).instance!==parsed.instance)throw Error('CALL_NOT_OWNED');
              return send(res,200,await registered.finish(parsed.call_id));
            }
            const value=await registered.helper(action.slice('registered-'.length),parsed);
            if(action==='registered-session'&&!callingEnabled&&!value.busy){
              const diagnostic=await readAudioDiagnostic();
              if(diagnostic&&/^[a-f0-9-]{36}$/.test(diagnostic.id)&&diagnostic.expires_at>Date.now()&&diagnostic.expires_at<Date.now()+600000)value.audio_diagnostic_id=diagnostic.id;
            }
            return send(res,200,value);
          }catch(error){return send(res,409,{error:/^[A-Z_]{1,80}$/.test(error.message)?error.message:'REGISTERED_REQUEST_FAILED'});}
        }
        const schema=companionSchemas[action];
        if(!schema) return send(res,404,{error:'Not found'});
        const checked=schema.safeParse(parsed);
        if(!checked.success) return send(res,400,{error:'Invalid companion request'});
        if(registered&&action==='pulse'&&checked.data.ready)checked.data.ready=registered.registered();
        const result=queue.perform(action==='inbound-ready'?'inbound_ready':action,checked.data);
        // Native-host heartbeats continue while browser voice startup is pending.
        if(registered&&action==='pulse'&&checked.data.busy)await registered.heartbeat(checked.data.instance);
        return send(res,200,result);
      }
      const mcp=makeMcpServer({nativeStatus,readStatus,
        startCall:input=>callingEnabled&&(!registered||(registered.registered()&&!registered.status().recovery_required))?queue.perform('start',input):{status:'blocked',code:'LOCAL_CALLING_DISABLED',call_placed:false},getCall:input=>queue.perform('get',input),endCall:input=>queue.perform('end',input)});
      const transport=new StreamableHTTPServerTransport({sessionIdGenerator:undefined,enableJsonResponse:true});
      res.on('close',()=>{void transport.close();void mcp.close();});
      await mcp.connect(transport);
      await transport.handleRequest(req,res,parsed);
    } catch {if(!res.headersSent)send(res,500,{error:'Local request failed'});else res.end();}
  });
  // Separate listener: the public tunnel can never expose MCP, the queue's
  // companion controls, or bearer credentials, even if a route is misconfigured.
  const publicServer=createServer(async(req,res)=>{
    try {
      if(registered)return send(res,404,{error:'Use registered client routing'});
      if(!req.url.startsWith('/inbound/')) return send(res,404,{error:'Not found'});
      if(req.headers.origin) return send(res,403,{error:'Origin rejected'});
      if(!['GET','POST'].includes(req.method)) return send(res,405,{error:'Method rejected'});
      const body=req.method==='POST'?await readBody(req,16384):undefined;
      const request=new Request(publicOrigin+req.url,{method:req.method,headers:req.headers,body});
      const response=await inbound(request,env,r=>r.text());
      res.writeHead(response.status,Object.fromEntries(response.headers));
      res.end(Buffer.from(await response.arrayBuffer()));
    } catch {if(!res.headersSent)send(res,400,{error:'Webhook rejected'});else res.end();}
  });
  for(const server of [privateServer,publicServer]) {
    server.requestTimeout=15000;server.headersTimeout=10000;
    server.maxConnections=128;
  }
  return {privateServer,publicServer};
}

export function twilioStatusReader(credentials,request=fetch) {
  return async()=>{
    const sid=credentials.TWILIO_ACCOUNT_SID,token=credentials.TWILIO_AUTH_TOKEN;
    if(!/^AC[0-9a-f]{32}$/.test(sid??'')||!token) return {configured:false,reachable:false};
    const response=await request(`https://api.twilio.com/2010-04-01/Accounts/${sid}.json`,{
      headers:{Authorization:'Basic '+Buffer.from(sid+':'+token).toString('base64')},
      redirect:'error',signal:AbortSignal.timeout(5000)});
    if(!response.ok) return {configured:true,reachable:false,calls_enabled:false,conversation_calls_enabled:false};
    const data=JSON.parse(await readBody(response.body,65536));
    const enabled=data.status==='active';
    return {configured:true,reachable:true,calls_enabled:enabled,conversation_calls_enabled:enabled};
  };
}
