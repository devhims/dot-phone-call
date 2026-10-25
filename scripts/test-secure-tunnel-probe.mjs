import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
const transport = new StdioClientTransport({command:process.execPath,args:['scripts/secure-tunnel-probe.mjs'],stderr:'pipe'});
const client = new Client({name:'tunnel-probe-local-check',version:'0.1.0'});
try {
  await client.connect(transport);
  const {tools} = await client.listTools();
  assert.deepEqual(tools.map(t=>t.name),['verify_tunnel_round_trip']);
  const challenge=randomUUID();
  const response=await client.callTool({name:'verify_tunnel_round_trip',arguments:{challenge}});
  assert.equal(response.structuredContent.challenge,challenge);
  assert.equal(response.structuredContent.phone_dialing_available,false);
  assert.equal(response.structuredContent.audio_access,false);
  const denied=await client.callTool({name:'start_owner_call',arguments:{request_key:randomUUID()}}).catch(e=>e);
  assert.ok(denied instanceof Error || denied.isError,'dialing must be unavailable');
  console.log('PASS: local MCP round trip, single read-only tool, dialing unavailable. This does not verify the tunnel or Dot.');
} finally {await client.close();}
