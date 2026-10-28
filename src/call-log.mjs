import {appendFileSync,mkdirSync,readdirSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {z} from 'zod';

// One JSON line per call event, one file per local day. Events carry states,
// error codes, timings and audio levels only: never phone numbers, the PIN,
// speech, transcripts or credentials. `npm run call-log` reads these files.
const KEY=/^[a-z][a-z0-9_]{0,39}$/;
const RESERVED=new Set(['t','src','ev']);
const pad=(n,w=2)=>String(n).padStart(w,'0');
export function localTime(ms) {
  const d=new Date(ms),offset=-d.getTimezoneOffset(),sign=offset>=0?'+':'-';
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(),3)}${sign}${pad(Math.floor(Math.abs(offset)/60))}:${pad(Math.abs(offset)%60)}`;
}
// Flat primitive fields only; anything else is dropped rather than logged.
export function clean(fields={}) {
  const out={};
  for(const [key,value] of Object.entries(fields??{})) {
    if(Object.keys(out).length>=32)break;
    if(!KEY.test(key)||RESERVED.has(key))continue;
    if(typeof value==='string')out[key]=value.slice(0,200);
    else if(typeof value==='number'&&Number.isFinite(value))out[key]=Math.round(value*100)/100;
    else if(typeof value==='boolean'||value===null)out[key]=value;
  }
  return out;
}
export function createCallLog({dir,now=Date.now,keepDays=14,append=appendFileSync}={}) {
  if(!dir)return {event:()=>{},events:()=>0};
  mkdirSync(dir,{recursive:true,mode:0o700});
  const cutoff=localTime(now()-keepDays*86400000).slice(0,10);
  for(const name of readdirSync(dir)) {
    const day=/^calls-(\d{4}-\d{2}-\d{2})\.jsonl$/.exec(name)?.[1];
    if(day&&day<cutoff)rmSync(join(dir,name),{force:true});
  }
  function event(src,ev,fields={},at=now()) {
    if(!KEY.test(src)||!KEY.test(ev))return;
    const line={t:localTime(at),src,ev,...clean(fields)};
    try {append(join(dir,`calls-${line.t.slice(0,10)}.jsonl`),JSON.stringify(line)+'\n',{mode:0o600});} catch {}
  }
  // Batches from the extension and the hidden Twilio page. Their clocks are the
  // same Mac's, so their timestamps are kept unless clearly wrong.
  function events(src,list) {
    const checked=remoteBatch.safeParse(list);
    if(!checked.success)return 0;
    for(const {ev,at,...fields} of checked.data) {
      const time=Number.isFinite(at)&&Math.abs(at-now())<86400000?at:now();
      event(src,ev,fields,time);
    }
    return checked.data.length;
  }
  return {event,events};
}
const remoteBatch=z.array(z.object({ev:z.string().regex(KEY),at:z.number().optional()}).passthrough()).min(1).max(40);
