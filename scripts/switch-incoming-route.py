"""Back up, switch, or restore this saved Twilio number. Never places calls."""
import argparse,json,re
from pathlib import Path
from twilio.rest import Client
from twilio.http.http_client import TwilioHttpClient
ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--url');p.add_argument('--apply',action='store_true');p.add_argument('--restore',action='store_true');args=p.parse_args()
c=json.loads((ROOT/'.secrets/inbound-worker-secrets.json').read_text())
client=Client(c['TWILIO_ACCOUNT_SID'],c['TWILIO_AUTH_TOKEN'],http_client=TwilioHttpClient(timeout=15))
found=client.incoming_phone_numbers.list(phone_number=c['INBOUND_TWILIO_NUMBER'],limit=2)
if len(found)!=1 or found[0].phone_number!=c['INBOUND_TWILIO_NUMBER']:raise SystemExit('Saved Twilio number does not match exactly one account resource')
number=found[0]
fields=['voice_url','voice_method','voice_fallback_url','voice_fallback_method','voice_application_sid','status_callback','status_callback_method']
backup=ROOT/'.secrets/incoming-route-fallback.json'
if not backup.exists():
    with backup.open('x') as f:json.dump({'account_sid':c['TWILIO_ACCOUNT_SID'],'number_sid':number.sid,'route':{k:getattr(number,k) or '' for k in fields}},f,indent=2)
    backup.chmod(0o600)
old=json.loads(backup.read_text())
if old['account_sid']!=c['TWILIO_ACCOUNT_SID'] or old['number_sid']!=number.sid:raise SystemExit('Rollback identity mismatch')
if args.restore:
    if not args.apply:raise SystemExit('Restoration requires --apply')
    expected=old['route'];client.incoming_phone_numbers(number.sid).update(**expected)
elif args.apply:
    if not re.fullmatch(r'https://handler\.twilio\.com/twiml/EH[a-f0-9]{32}',args.url or ''):raise SystemExit('Expected verified TwiML Bin URL')
    expected={'voice_url':args.url,'voice_method':'POST','voice_application_sid':'','voice_fallback_url':old['route']['voice_url'],'voice_fallback_method':old['route']['voice_method'] or 'POST'}
    client.incoming_phone_numbers(number.sid).update(**expected)
else:expected={k:getattr(number,k) or '' for k in fields}
actual=client.incoming_phone_numbers(number.sid).fetch()
if any((getattr(actual,k) or '')!=v for k,v in expected.items()):raise SystemExit('Read-back mismatch; inspect routing before retrying')
print(json.dumps({'updated':args.apply,'restored':args.restore,'voice_url':actual.voice_url,'voice_fallback_url':actual.voice_fallback_url,'backup':str(backup),'phone_dialed':False}))
