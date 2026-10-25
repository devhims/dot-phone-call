// Isolated stdio MCP probe. No phone modules, credentials, network, or audio APIs.
import {randomUUID} from 'node:crypto';
import {McpServer} from '@modelcontextprotocol/sdk/server/mcp.js';
import {StdioServerTransport} from '@modelcontextprotocol/sdk/server/stdio.js';
import {z} from 'zod';

const instance = randomUUID();
const server = new McpServer({name:'dot-phone-tunnel-test',version:'0.1.0'}, {
  instructions:'Read-only Secure MCP Tunnel connectivity test. This server cannot place calls, start voice, access files, or change audio. Use verify_tunnel_round_trip with a fresh challenge to prove this local server executed a request. Do not use the production phone plugin for this test.',
});
const annotations = {readOnlyHint:true,destructiveHint:false,idempotentHint:true,openWorldHint:false};
const result = data => ({content:[{type:'text',text:JSON.stringify(data)}],structuredContent:data});
server.registerTool('verify_tunnel_round_trip',{
  description:'Echo a fresh test challenge and return the local process identity and current time. Connectivity only; no phone or audio operations exist.',
  inputSchema:{challenge:z.string().min(8).max(100).regex(/^[a-zA-Z0-9 _-]+$/)},annotations,
}, async ({challenge}) => {
  console.error(JSON.stringify({event:'probe_called',instance,at:new Date().toISOString()}));
  return result({challenge,instance,server_time:new Date().toISOString(),executed_locally:true,phone_dialing_available:false,audio_access:false});
});
await server.connect(new StdioServerTransport());
