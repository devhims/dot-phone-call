import {generateKeyPairSync,createHash,randomBytes} from 'node:crypto';
import {mkdir,readFile,writeFile,copyFile,chmod} from 'node:fs/promises';
const root = new URL('../',import.meta.url);
await mkdir(new URL('.secrets/',root),{recursive:true,mode:0o700});
const path=new URL('.secrets/mac-companion.json',root);
let config;
try {config=JSON.parse(await readFile(path,'utf8'));}
catch(error) {
  if(error.code!=='ENOENT') throw error;
  const {publicKey}=generateKeyPairSync('rsa',{modulusLength:2048});
  const key=publicKey.export({type:'spki',format:'der'});
  const extensionId=[...createHash('sha256').update(key).digest('hex').slice(0,32)].map(n=>String.fromCharCode(97+parseInt(n,16))).join('');
  config={origin:'http://127.0.0.1:8894',
    cloud_token:randomBytes(32).toString('hex'), browser_token:randomBytes(32).toString('hex'),
    extension_key:key.toString('base64'),extension_id:extensionId};
  await writeFile(path,JSON.stringify(config,null,2)+'\n',{mode:0o600});
}
const output=new URL('.runtime/dot-phone-extension/',root);
const localBackend=process.argv.includes('--backend=local');
await mkdir(output,{recursive:true,mode:0o700});
for(const file of ['background.js','content.js','microphone-route.js','helper-client.js','offscreen-control.js','offscreen.html','setup.html','setup.js','setup.css']) await copyFile(new URL('chrome-extension/'+file,root),new URL(file,output));
await mkdir(new URL('bridge/',output),{recursive:true});
for(const file of ['app.js','session.js','diagnostic.js'])await copyFile(new URL('browser/'+file,root),new URL('bridge/'+file,output));
await copyFile(new URL('prototype/registered/pcm-worklet.js',root),new URL('bridge/pcm-worklet.js',output));
await copyFile(new URL('node_modules/@twilio/voice-sdk/dist/twilio.min.js',root),new URL('bridge/twilio.min.js',output));
const manifest=JSON.parse(await readFile(new URL('chrome-extension/manifest.json',root),'utf8'));
manifest.key=config.extension_key;
await writeFile(new URL('manifest.json',output),JSON.stringify(manifest,null,2));
await writeFile(new URL('pairing.js',output),'const DOT_PHONE_CONFIG = '+JSON.stringify({origin:localBackend?'http://127.0.0.1:8894':config.origin,token:config.cloud_token,hiddenAudioTestId:process.argv.includes('--hidden-audio-test')?crypto.randomUUID():null,hiddenBridge:!process.argv.includes('--tab-bridge'),callingEnabled:process.argv.includes('--enable-calling'),prepareTestId:process.argv.includes('--prepare-test')?crypto.randomUUID():null,testId:process.argv.includes('--audio-test')?crypto.randomUUID():null})+';\n',{mode:0o600});
await chmod(new URL('pairing.js',output),0o600);
console.log(JSON.stringify({extension_directory:output.pathname,extension_id:config.extension_id}));
