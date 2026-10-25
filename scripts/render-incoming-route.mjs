// Produces a reviewable TwiML Bin document. Never updates Twilio routing.
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {incomingTwiml} from '../src/registered-calls.mjs';
const url=process.argv[2];
if(!url)throw Error('Usage: node scripts/render-incoming-route.mjs https://handler.twilio.com/twiml/EH...');
const config=JSON.parse(await readFile(new URL('../.secrets/inbound-worker-secrets.json',import.meta.url),'utf8'));
await mkdir(new URL('../.runtime/',import.meta.url),{recursive:true,mode:0o700});
await writeFile(new URL('../.runtime/incoming-route.xml',import.meta.url),incomingTwiml(config.CLIENT_IDENTITY,url)+'\n',{mode:0o600});
console.log('Wrote .runtime/incoming-route.xml. No Twilio routing was changed.');
