import {createHmac,createHash,randomBytes,timingSafeEqual} from 'node:crypto';
import {TERMINAL} from './call-queue.mjs';
const providerTerminal=new Set(['completed','canceled','failed','busy','no-answer']);
const sid=value=>/^CA[0-9a-f]{32}$/.test(value??'');
const fail=code=>{throw new Error(code);};
const equal=(a,b)=>{const x=Buffer.from(a??''),y=Buffer.from(b??'');return x.length===y.length&&timingSafeEqual(x,y);};
const xml=value=>String(value).replace(/[<>&"']/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&apos;'}[c]));
export function incomingTwiml(identity,binUrl) {
  if(!/^[a-zA-Z0-9_]{8,100}$/.test(identity)||!/^https:\/\/handler.twilio.com\/twiml\/EH[0-9a-f]{32}$/.test(binUrl)) fail('INVALID_INBOUND_ROUTE');
  return `<Response>{{^Digits}}<Gather input="dtmf" numDigits="6" timeout="10" method="POST" action="${xml(binUrl)}"><Say>Enter your six digit Dot calling PIN.</Say></Gather><Hangup/>{{/Digits}}{{#Digits}}<Dial timeout="60" timeLimit="600" answerOnBridge="true" record="do-not-record"><Client><Identity>${xml(identity)}</Identity><Parameter name="pin" value="{{.}}"/></Client></Dial><Hangup/>{{/Digits}}</Response>`;
}
export function accessToken(config,now=Date.now()) {
  if(!/^AC[0-9a-f]{32}$/.test(config.TWILIO_ACCOUNT_SID??'')||!/^SK[0-9a-f]{32}$/.test(config.TWILIO_API_KEY_SID??'')||!config.TWILIO_API_KEY_SECRET||!/^[a-zA-Z0-9_]{8,100}$/.test(config.CLIENT_IDENTITY??'')) fail('REGISTERED_CLIENT_NOT_CONFIGURED');
  const iat=Math.floor(now/1000),encode=x=>Buffer.from(JSON.stringify(x)).toString('base64url');
  const body=encode({typ:'JWT',alg:'HS256',cty:'twilio-fpa;v=1'})+'.'+encode({jti:config.TWILIO_API_KEY_SID+'-'+iat+'-'+randomBytes(8).toString('hex'),iss:config.TWILIO_API_KEY_SID,sub:config.TWILIO_ACCOUNT_SID,iat,exp:iat+3600,grants:{identity:config.CLIENT_IDENTITY,voice:{incoming:{allow:true}}}});
  return body+'.'+createHmac('sha256',config.TWILIO_API_KEY_SECRET).update(body).digest('base64url');
}
export function twilioProvider(config,request=fetch) {
  const base=`https://api.twilio.com/2010-04-01/Accounts/${config.TWILIO_ACCOUNT_SID}`;
  async function call(path,body) {
    const response=await request(base+path,{method:body?'POST':'GET',headers:{Authorization:'Basic '+Buffer.from(config.TWILIO_ACCOUNT_SID+':'+config.TWILIO_AUTH_TOKEN).toString('base64'),...(body?{'Content-Type':'application/x-www-form-urlencoded'}:{})},body:body?new URLSearchParams(body):undefined,redirect:'error',signal:AbortSignal.timeout(10000)});
    if(!response.ok) fail('TWILIO_REQUEST_FAILED');
    return response.json();
  }
  return {get:id=>{if(!sid(id))fail('INVALID_CALL_SID');return call(`/Calls/${id}.json`);},create:options=>call('/Calls.json',options),end:id=>{if(!sid(id))fail('INVALID_CALL_SID');return call(`/Calls/${id}.json`,{Status:'completed'});}};
}
// Serializes lifecycle transitions, including the intent written before dialing.
// Never retries a call creation whose response was lost.
export class RegisteredCalls {
  constructor({queue,config,provider=twilioProvider(config),enabled=false,allowPhoneDialing=false,now=Date.now}) {
    Object.assign(this,{queue,config,provider,enabled,allowPhoneDialing,now});
    this.capability=randomBytes(32).toString('hex');this.lease=null;this.tail=Promise.resolve();
    this.record=queue.readRegistered?.()??null;
    this.recovering=!!this.record;this.lastProviderCheck=0;
  }
  exclusive(fn){const next=this.tail.then(fn);this.tail=next.catch(()=>{});return next;}
  save(){this.queue.writeRegistered?.(this.record);}
  registered(){return !!this.lease?.ready&&this.now()-this.lease.at<12000;}
  status(){return {transport:'registered_twilio_client',registered:this.registered(),recovery_required:this.recovering||!!this.record?.unknown,phone_dialing_enabled:this.enabled&&this.allowPhoneDialing,browser_context:this.lease?.context??'tab',microphone_ready:this.lease?.microphoneReady??true};}
  async helper(action,input) {return this.exclusive(async()=>{
    if(action==='session')return {url:'http://127.0.0.1:8894/bridge/#'+this.capability,...this.status(),busy:!!this.record||!!this.queue.perform('status').active_call};
    const job=this.queue.perform('get',{call_id:input.call_id});
    if(job.instance!==input.instance)fail('CALL_NOT_OWNED');
    if(action==='state'&&(!this.record||this.record.ended||TERMINAL.has(job.status)||job.cancel_requested))return {ended:true};
    if(TERMINAL.has(job.status)||job.cancel_requested)fail('CALL_NOT_OWNED');
    if(action==='prepare') {
      if(!this.enabled||!this.registered()||this.recovering)fail('REGISTERED_CLIENT_UNAVAILABLE');
      if(this.record&&this.record.job!==job.id)fail('REGISTERED_CALL_BUSY');
      this.record??={job:job.id,direction:job.direction??'outbound',parent:job.inbound_call_sid??null,created:this.now()};
      this.record.instance=input.instance;this.record.helperReady=true;this.record.helperAt=this.now();this.save();
      return {ready:true};
    }
    if(!this.record||this.record.job!==job.id)fail('REGISTERED_CALL_MISSING');
    if(action==='state'){this.record.helperAt=this.now();return {connected:!!this.record.connected,ended:!!this.record.ended};}
    fail('UNKNOWN_REGISTERED_ACTION');
  });}
  async heartbeat(instance) {return this.exclusive(()=>{
    const r=this.record;
    if(!r||r.ended||r.instance!==instance||!r.helperReady)return;
    const job=this.queue.perform('get',{call_id:r.job});
    if(job.instance===instance&&!TERMINAL.has(job.status)&&!job.cancel_requested)r.helperAt=this.now();
  });}
  async finish(jobId) {return this.exclusive(async()=>{
    if(this.record?.job===jobId)await this.end();
    return {ended:!this.record};
  });}
  async verifiedCall(id) {
    const call=await this.provider.get(id);
    if(call.sid!==id||call.account_sid!==this.config.TWILIO_ACCOUNT_SID)fail('CALL_IDENTITY_MISMATCH');
    return call;
  }
  async incoming(input) {
    if(!this.enabled||!this.registered()||this.recovering)fail('REGISTERED_CLIENT_UNAVAILABLE');
    const child=await this.verifiedCall(input.sid);
    if(child.to!=='client:'+this.config.CLIENT_IDENTITY||!sid(child.parent_call_sid)||providerTerminal.has(child.status))fail('CALL_IDENTITY_MISMATCH');
    const parent=await this.verifiedCall(child.parent_call_sid);
    if(providerTerminal.has(parent.status))fail('CALL_ENDED');
    if(this.record?.direction==='outbound') {
      if(this.record.parent!==parent.sid||parent.to!==this.config.INBOUND_OWNER_NUMBER||parent.from!==this.config.INBOUND_TWILIO_NUMBER)fail('CALL_IDENTITY_MISMATCH');
    } else {
      if(parent.direction!=='inbound'||parent.from!==this.config.INBOUND_OWNER_NUMBER||parent.to!==this.config.INBOUND_TWILIO_NUMBER)fail('CALL_IDENTITY_MISMATCH');
      if(this.record) {
        if(this.record.parent!==parent.sid||this.record.child!==child.sid)fail('REGISTERED_CALL_BUSY');
      } else {
        const open=this.queue.perform('inbound_open',{inbound_call_sid:parent.sid});
        if(open.status!=='pin_required')fail('INBOUND_NOT_AVAILABLE');
        const pinOk=/^\d{6}$/.test(input.pin??'')&&/^[a-f0-9]{64}$/.test(this.config.INBOUND_PIN_HASH??'')&&equal(createHash('sha256').update(this.config.INBOUND_PIN_SALT+':'+input.pin).digest('hex'),this.config.INBOUND_PIN_HASH);
        const queued=this.queue.perform('inbound_pin',{inbound_call_sid:parent.sid,pin_ok:pinOk});
        if(!pinOk||queued.status!=='waiting')fail('INBOUND_AUTHORIZATION_FAILED');
        const job=this.queue.perform('status').active_call;
        if(job?.inbound_call_sid!==parent.sid)fail('INBOUND_NOT_AVAILABLE');
        this.record={job:job.id,direction:'inbound',parent:parent.sid,created:this.now()};
      }
    }
    this.record.child=child.sid;this.save();
    return {job_id:this.record.job};
  }
  async browser(action,input) {return this.exclusive(async()=>{
    if(!/^[a-zA-Z0-9-]{16,64}$/.test(input.client??''))fail('INVALID_CLIENT');
    if(this.lease&&this.lease.client!==input.client&&this.now()-this.lease.at<12000)fail('ANOTHER_BROWSER_REGISTERED');
    if(action==='token') {
      this.lease={client:input.client,at:this.now(),ready:this.lease?.ready??false,tokenUntil:this.now()+3600000};
      return {token:accessToken(this.config,this.now())};
    }
    if(this.lease?.client!==input.client)fail('BROWSER_NOT_REGISTERED');
    this.lease.at=this.now();
    if(action==='events')return {ready:true};
    if(action==='pulse') {
      this.lease.ready=input.registered===true;
      this.lease.context=input.context==='offscreen'?'offscreen':'tab';
      this.lease.microphoneReady=input.microphone_ready!==false;
      await this.tick();
      const r=this.record;
      let token;
      if(this.now()>(this.lease.tokenUntil??0)-120000){token=accessToken(this.config,this.now());this.lease.tokenUntil=this.now()+3600000;}
      return {token,calling_enabled:this.enabled,release_job:r?.ended?r.job:null,job:r&&!r.ended&&!this.recovering?{id:r.job,direction:r.direction,prepared:!!r.helperReady,child_sid:r.child??null}:null};
    }
    if(action==='incoming')return this.incoming(input);
    const r=this.record;
    if(!r||r.job!==input.job_id)fail('CALL_NOT_OWNED');
    if(action==='failed'){
      if(!/^[A-Z_]{1,60}$/.test(input.code??''))fail('INVALID_FAILURE_CODE');
      this.queue.perform('update',{instance:r.instance,call_id:r.job,status:'failed',code:input.code});
      await this.end();return {failed:true};
    }
    if(action==='released'){r.released=true;this.save();if(r.ended)await this.end();return {released:true};}
    if(action==='ended'){r.released=true;await this.end();return {ended:true};}
    if(r.ended||this.recovering)fail('CALL_NOT_OWNED');
    const job=this.queue.perform('get',{call_id:r.job});
    if(TERMINAL.has(job.status)||job.cancel_requested){await this.end();fail('CALL_CANCELED');}
    if(action==='ready') {
      if(!r.helperReady||!this.registered())fail('AUDIO_NOT_READY');
      if(r.unknown)fail('CALL_CREATION_OUTCOME_UNKNOWN');
      if(r.direction==='outbound'&&!r.intent) {
        if(!this.allowPhoneDialing)fail('PHONE_DIALING_DISABLED');
        for(const key of ['INBOUND_OWNER_NUMBER','INBOUND_TWILIO_NUMBER'])if(!/^\+[1-9][0-9]{7,14}$/.test(this.config[key]??''))fail('INVALID_SAVED_NUMBER');
        r.intent=true;r.unknown=true;this.save();
        const twiml=`<Response><Dial timeout="45" timeLimit="${job.duration_seconds}" answerOnBridge="true" record="do-not-record"><Client>${xml(this.config.CLIENT_IDENTITY)}</Client></Dial><Hangup/></Response>`;
        const call=await this.provider.create({To:this.config.INBOUND_OWNER_NUMBER,From:this.config.INBOUND_TWILIO_NUMBER,Twiml:twiml,Timeout:'25',TimeLimit:String(job.duration_seconds+45),Record:'false'});
        if(!sid(call.sid))fail('CALL_CREATION_OUTCOME_UNKNOWN');
        r.parent=call.sid;r.unknown=false;this.save();
        this.queue.perform('update',{instance:r.instance,call_id:r.job,status:'ringing'});
      }
      return {ready:true};
    }
    if(action==='connected') {
      if(!r.child||r.child!==input.sid||!r.helperReady)fail('CALL_NOT_OWNED');
      const child=await this.verifiedCall(r.child);
      if(child.parent_call_sid!==r.parent||child.to!=='client:'+this.config.CLIENT_IDENTITY||child.status!=='in-progress')fail('CALL_NOT_CONNECTED');
      r.connected=true;this.save();return {connected:true};
    }
    if(action==='ended'){await this.end();return {ended:true};}
    fail('UNKNOWN_REGISTERED_ACTION');
  });}
  async end() {
    const r=this.record;if(!r)return;
    r.ended=true;this.queue.perform('end',{call_id:r.job});this.save();
    if(r.parent) {
      const parent=await this.verifiedCall(r.parent);
      const expected=r.direction==='inbound'?[this.config.INBOUND_OWNER_NUMBER,this.config.INBOUND_TWILIO_NUMBER]:[this.config.INBOUND_TWILIO_NUMBER,this.config.INBOUND_OWNER_NUMBER];
      if(parent.from!==expected[0]||parent.to!==expected[1])fail('CALL_IDENTITY_MISMATCH');
      if(!providerTerminal.has(parent.status))await this.provider.end(r.parent);
      const verified=await this.verifiedCall(r.parent);
      if(!providerTerminal.has(verified.status))fail('PHONE_HANGUP_UNCONFIRMED');
    }
    if(r.unknown)fail('CALL_CREATION_OUTCOME_UNKNOWN');
    if(r.helperReady&&!r.released)return;
    this.record=null;this.recovering=false;this.save();
  }
  async tick() {
    const r=this.record;if(!r)return;
    const job=this.queue.perform('get',{call_id:r.job});
    if(this.recovering||r.ended||TERMINAL.has(job.status)||job.cancel_requested||!this.registered()||this.now()-r.created>(job.duration_seconds+90)*1000||(r.helperAt&&this.now()-r.helperAt>15000))return this.end();
    if(r.parent&&this.now()-this.lastProviderCheck>3000){
      this.lastProviderCheck=this.now();const parent=await this.verifiedCall(r.parent);
      if(providerTerminal.has(parent.status))await this.end();
    }
  }
  monitor(){return this.exclusive(()=>this.tick());}
  close(){return this.exclusive(()=>this.end());}
}
