"""Render both hosted TwiML branches. Fetches instructions only; never creates a call."""
import argparse,json,re,xml.etree.ElementTree as ET
from pathlib import Path
import requests
from twilio.request_validator import RequestValidator
ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('url');args=p.parse_args()
if not re.fullmatch(r'https://handler\.twilio\.com/twiml/EH[a-f0-9]{32}',args.url):raise SystemExit('Expected TwiML Bin URL')
c=json.loads((ROOT/'.secrets/inbound-worker-secrets.json').read_text())
validator=RequestValidator(c['TWILIO_AUTH_TOKEN'])
for digits in [None,'123456']:
    data={'AccountSid':c['TWILIO_ACCOUNT_SID']}
    if digits:data['Digits']=digits
    signature=validator.compute_signature(args.url,data)
    response=requests.post(args.url,data=data,headers={'X-Twilio-Signature':signature},timeout=15,allow_redirects=False)
    response.raise_for_status()
    assert '{{' not in response.text,'Unrendered template'
    root=ET.fromstring(response.text);assert root.tag=='Response'
    if digits:
        dial=root.find('Dial');assert dial is not None
        assert root.find('Gather') is None
        assert dial.findtext('Client/Identity')==c['CLIENT_IDENTITY']
        parameter=dial.find('Client/Parameter');assert parameter.attrib=={'name':'pin','value':digits}
        assert dial.get('answerOnBridge')=='true' and dial.get('record')=='do-not-record'
    else:
        gather=root.find('Gather');assert gather is not None
        assert gather.get('action')==args.url and gather.get('numDigits')=='6'
        assert root.find('Dial') is None
    print(json.dumps({'branch':'client' if digits else 'pin_prompt','http_status':response.status_code,'valid':True,'phone_dialed':False}))
