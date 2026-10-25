import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';

function fixture({devices, actual='blackhole', pending=false}={}) {
  const calls=[];
  const tracks=[];
  let resolve;
  const mediaDevices={
    enumerateDevices:async()=>typeof devices==='function' ? devices() : devices??[
      {kind:'audioinput',deviceId:'macbook',label:'MacBook Pro Microphone'},
      {kind:'audioinput',deviceId:'blackhole',label:'BlackHole 16ch'},
    ],
    getUserMedia:async constraints=>{
      calls.push(constraints);
      if(pending) await new Promise(r=>{resolve=r;});
      const track={enabled:true,readyState:'live',label:'BlackHole 16ch',
        getSettings:()=>({deviceId:actual}), stop(){this.readyState='ended';}};
      tracks.push(track);
      return {getTracks:()=>[track],getAudioTracks:()=>[track]};
    },
  };
  const context=vm.createContext({navigator:{mediaDevices},Date,setTimeout:()=>0,clearTimeout:()=>{}});
  vm.runInContext(readFileSync(new URL('../chrome-extension/microphone-route.js',import.meta.url),'utf8'),context);
  return {route:context.dotPhoneMicrophone,mediaDevices,calls,tracks,release:()=>resolve()};
}
test('normal browser audio leaves Chrome microphone preference untouched',async()=>{
  const f=fixture();const constraints={audio:{deviceId:'macbook'},video:false};
  await f.mediaDevices.getUserMedia(constraints);
  assert.equal(f.calls[0],constraints);
});
test('owned call overrides explicit MacBook selection and verifies the actual track',async()=>{
  const f=fixture();await f.route.arm('job',Date.now()+60000);
  await f.mediaDevices.getUserMedia({audio:{deviceId:{exact:'macbook'},echoCancellation:false},video:false});
  assert.equal(f.calls[0].audio.deviceId.exact,'blackhole');
  assert.equal(f.calls[0].audio.echoCancellation,false);
  assert.equal(f.route.status('job').microphone_verified,true);
  f.route.disarm('job');
  assert.equal(f.tracks[0].readyState,'ended');
  await f.mediaDevices.getUserMedia({audio:{deviceId:'macbook'}});
  assert.equal(f.calls[1].audio.deviceId,'macbook');
});
test('missing BlackHole never falls back to physical microphone',async()=>{
  const f=fixture({devices:[{kind:'audioinput',deviceId:'macbook',label:'MacBook Microphone'}]});
  await assert.rejects(f.route.arm('job',Date.now()+60000),/BLACKHOLE_MICROPHONE_NOT_FOUND/);
  assert.equal(f.calls.length,0);
});
test('wrong returned device stops capture and fails verification',async()=>{
  const f=fixture({actual:'macbook'});await f.route.arm('job',Date.now()+60000);
  await assert.rejects(f.mediaDevices.getUserMedia({audio:true}),/DOT_MICROPHONE_DEVICE_MISMATCH/);
  assert.equal(f.tracks[0].readyState,'ended');
  assert.equal(f.route.status('job').microphone_verified,false);
});
test('late microphone grant after cleanup is stopped',async()=>{
  const f=fixture({pending:true});await f.route.arm('job',Date.now()+60000);
  const capture=f.mediaDevices.getUserMedia({audio:true});
  f.route.disarm('job');f.release();
  await assert.rejects(capture,/CALL_CANCELED/);
  assert.equal(f.tracks[0].readyState,'ended');
});
test('ownership prevents another job from disarming or replacing microphone route',async()=>{
  const f=fixture();await f.route.arm('job',Date.now()+60000);
  await assert.rejects(f.route.arm('other',Date.now()+60000),/DOT_MICROPHONE_ALREADY_OWNED/);
  assert.throws(()=>f.route.disarm('other'),/DOT_MICROPHONE_OWNER_MISMATCH/);
});
test('muted or ended tracks cannot pass verification',async()=>{
  const f=fixture();await f.route.arm('job',Date.now()+60000);
  await f.mediaDevices.getUserMedia({audio:true});
  f.tracks[0].enabled=false;
  assert.equal(f.route.status('job').microphone_verified,false);
});
test('unconfirmed hangup stops audio without allowing physical-mic reacquisition',async()=>{
  const f=fixture();await f.route.arm('job',Date.now()+60000);
  await f.mediaDevices.getUserMedia({audio:true});
  f.route.suspend('job');
  assert.equal(f.tracks[0].readyState,'ended');
  await assert.rejects(f.mediaDevices.getUserMedia({audio:{deviceId:'macbook'}}),/DOT_MICROPHONE_LEASE_EXPIRED/);
  assert.equal(f.calls.length,1);
  f.route.disarm('job');
});

test('cleanup during device enumeration prevents a late arm',async()=>{
  let resolve;
  const f=fixture({devices:()=>new Promise(r=>{resolve=r;})});
  const armed=f.route.arm('job',Date.now()+60000);
  f.route.disarm('job');
  resolve([{kind:'audioinput',deviceId:'blackhole',label:'BlackHole 16ch'}]);
  await assert.rejects(armed,/CALL_CANCELED/);
  assert.equal(f.route.status('job').microphone_verified,false);
});
test('Chrome label suffix is accepted but the 2-channel return device is excluded',async()=>{
  const f=fixture({devices:[
    {kind:'audioinput',deviceId:'two',label:'BlackHole 2ch'},
    {kind:'audioinput',deviceId:'blackhole',label:'BlackHole 16ch (Virtual)'},
  ]});
  await f.route.arm('job',Date.now()+60000);
  await f.mediaDevices.getUserMedia({audio:true});
  assert.equal(f.calls[0].audio.deviceId.exact,'blackhole');
  assert.equal(f.route.status('job').microphone_verified,true);
});
test('missing-device error reports BlackHole labels without exposing device IDs',async()=>{
  const f=fixture({devices:[{kind:'audioinput',deviceId:'private-device-id',label:'BlackHole 2ch'}]});
  await assert.rejects(f.route.arm('job',Date.now()+60000),error=>{
    assert.equal(error.diagnostics.input_count,1);
    assert.equal(error.diagnostics.blackhole_inputs[0].label,'BlackHole 2ch');
    assert.equal(JSON.stringify(error.diagnostics).includes('private-device-id'),false);
    return true;
  });
});
