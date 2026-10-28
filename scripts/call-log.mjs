#!/usr/bin/env node
// Explains a Dot Phone call from the local call log.
//   npm run call-log                 the most recent call
//   npm run call-log -- 3d18377a     a call by ID (or ID prefix)
//   npm run call-log -- --list       recent calls, one line each
//   npm run call-log -- --all        also print every 5-second audio event in the timeline
import {readdirSync,readFileSync} from 'node:fs';
import {contextEvents,diagnose,formatAudio,formatEvent,groupCalls,parseLog} from '../src/call-timeline.mjs';

const args=process.argv.slice(2);
const option=name=>{const i=args.indexOf(name);return i>=0?args[i+1]:undefined;};
const dir=option('--dir')??new URL('../.runtime/logs/',import.meta.url).pathname;
const days=Number(option('--days')??7);
let files=[];
try {files=readdirSync(dir).filter(f=>/^calls-\d{4}-\d{2}-\d{2}\.jsonl$/.test(f)).sort().slice(-days);} catch {}
if(!files.length) {
  console.log(`No call log yet in ${dir}. Calls are logged once the updated backend and extension are running.`);
  process.exit(0);
}
const events=parseLog(files.map(f=>readFileSync(dir+f,'utf8')).join('\n'));
const calls=[...groupCalls(events).entries()].sort((a,b)=>a[1][0].at-b[1][0].at);
if(!calls.length) {console.log('The call log has no calls yet.');process.exit(0);}

if(args.includes('--list')) {
  for(const [id,list] of calls.slice(-20)) {
    const d=diagnose(list);
    console.log(`${new Date(d.start).toLocaleString()}  ${id.slice(0,8)}  ${d.incoming?'incoming':'outgoing'}  ${d.outcome}`);
  }
  process.exit(0);
}
const wanted=args.find(a=>!a.startsWith('--')&&a!==option('--dir')&&a!==option('--days'));
const match=wanted?calls.filter(([id])=>id.startsWith(wanted)):calls.slice(-1);
if(match.length!==1) {
  console.log(match.length?`"${wanted}" matches ${match.length} calls; use more of the ID.`:`No call matches "${wanted}". Try --list.`);
  process.exit(1);
}
const [id,list]=match[0],context=contextEvents(events,list),d=diagnose(list,context);
console.log(`Call ${id} (${d.incoming?'incoming':'outgoing'}), ${new Date(d.start).toLocaleString()}`);
console.log(`\n${d.outcome}`);
for(const line of d.findings)console.log(`  - ${line}`);
const shown=[...list,...context].sort((a,b)=>a.at-b.at).filter(e=>args.includes('--all')||e.ev!=='audio');
console.log('\nTimeline');
for(const e of shown)console.log('  '+formatEvent(e,d.start));
const audio=formatAudio(list,d.start);
if(audio.length){console.log('\nAudio levels (peak per 5 seconds; below -45 dB is silence)');for(const line of audio)console.log(line);}
