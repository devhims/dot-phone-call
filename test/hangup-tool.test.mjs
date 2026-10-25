import {test} from 'node:test';
import assert from 'node:assert/strict';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {InMemoryTransport} from '@modelcontextprotocol/sdk/inMemory.js';
import {makeMcpServer} from '../src/tools.mjs';
import {operate} from '../src/call-queue.mjs';

test('MCP client can discover and invoke hangup without knowing the call id',async()=>{
 const state={jobs:[{id:crypto.randomUUID(),status:'connected',created_at:Date.now(),deadline:Date.now()+60000}]};
 const server=makeMcpServer({startCall:()=>assert.fail('must not dial'),getCall:()=>{},
  endCall:input=>operate(state,'end',input)});
 const client=new Client({name:'hangup-test',version:'1'});
 const [a,b]=InMemoryTransport.createLinkedPair();
 await server.connect(a);await client.connect(b);
 try {
  const tool=(await client.listTools()).tools.find(t=>t.name==='end_owner_call');
  assert.ok(!tool.inputSchema.required?.includes('call_id'));
  const result=await client.callTool({name:'end_owner_call',arguments:{}});
  assert.equal(result.structuredContent.cancel_requested,true);
  assert.equal(result.structuredContent.id,state.jobs[0].id);
 } finally {await client.close();await server.close();}
});

test('a call result supplies an executable hangup control for that exact call',async()=>{
 const state={};
 operate(state,'pulse',{instance:'test-companion',ready:true,busy:false,audio_guard_version:1});
 const server=makeMcpServer({startCall:input=>operate(state,'start',input),
  getCall:input=>operate(state,'get',input),endCall:input=>operate(state,'end',input)});
 const client=new Client({name:'call-controls-test',version:'1'});
 const [a,b]=InMemoryTransport.createLinkedPair();
 await server.connect(a);await client.connect(b);
 try {
  const started=(await client.callTool({name:'start_owner_call',arguments:{request_key:'local-no-phone-test'}})).structuredContent;
  assert.equal(started.hangup.tool,'end_owner_call');
  assert.deepEqual(started.hangup.arguments,{call_id:started.id});
  const progress=(await client.callTool({name:'get_owner_call',arguments:{call_id:started.id}})).structuredContent;
  assert.deepEqual(progress.hangup,started.hangup);
  const ended=(await client.callTool({name:started.hangup.tool,arguments:started.hangup.arguments})).structuredContent;
  assert.equal(ended.status,'canceled');
  assert.equal(ended.cancel_requested,true);
  const after=(await client.callTool({name:'get_owner_call',arguments:{call_id:started.id}})).structuredContent;
  assert.equal(after.hangup,undefined);
 } finally {await client.close();await server.close();}
});
