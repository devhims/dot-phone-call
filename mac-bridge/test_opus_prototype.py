"""No live Twilio requests. Tests the boundary that prevents probe phone calls."""
import json
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import jwt
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from twilio.request_validator import RequestValidator

from opus_prototype import Prototype


def state(**kwargs):
    return Prototype('AC'+'0'*32, 'test-token', '+15555550123', '+15555550456', **kwargs)


class DialPolicyTests(unittest.TestCase):
    def setUp(self):
        self.state = state()
        self.state.ticket = 'one-test-ticket'
        self.state.mode = 'probe'
        self.params = {'AccountSid': self.state.account, 'From': 'client:'+self.state.identity,
                       'CallSid': 'CA'+'1'*32, 'Ticket': self.state.ticket, 'Mode': 'probe'}

    def test_probe_can_only_dial_its_temporary_client(self):
        self.params['To'] = '+15555559999'
        xml = ET.fromstring(self.state.twiml(self.params))
        self.assertIsNone(xml.find('.//Number'))
        self.assertEqual(xml.find('.//Client').text, self.state.receiver)
        self.assertEqual(xml.find('Dial').attrib['record'], 'do-not-record')
        self.assertEqual(xml.find('Dial').attrib['timeLimit'], '40')
        self.assertFalse(self.state.result['phone_dialed'])

    def test_phone_mode_denied_by_default_even_with_correct_ticket(self):
        self.params['Mode'] = self.state.mode = 'owner'
        with self.assertRaises(web.HTTPForbidden): self.state.twiml(self.params)
        self.assertFalse(self.state.calls)

    def test_answer_timing_experiment_keeps_probe_client_only(self):
        self.state.answer_on_bridge = False
        xml = ET.fromstring(self.state.twiml(self.params))
        self.assertEqual(xml.find('Dial').attrib['answerOnBridge'], 'false')
        self.assertIsNone(xml.find('.//Number'))
        self.assertFalse(self.state.result['phone_dialed'])

    def test_probe_answer_timing_does_not_change_owner_route(self):
        self.state.answer_on_bridge = False
        self.state.allow_owner = True
        self.params['Mode'] = self.state.mode = 'owner'
        xml = ET.fromstring(self.state.twiml(self.params))
        self.assertEqual(xml.find('Dial').attrib['answerOnBridge'], 'true')
        self.assertEqual(xml.find('.//Number').text, self.state.owner)

    def test_opt_in_phone_target_is_saved_owner_not_browser_parameter(self):
        self.state.allow_owner = True
        self.params['Mode'] = self.state.mode = 'owner'
        self.params['To'] = '+15555559999'
        xml = ET.fromstring(self.state.twiml(self.params))
        self.assertEqual(xml.find('.//Number').text, self.state.owner)
        self.assertEqual(xml.find('Dial').attrib['callerId'], self.state.caller)

    def test_wrong_identity_account_ticket_and_mode_denied(self):
        for key in ['From', 'AccountSid', 'Ticket', 'Mode']:
            with self.subTest(key=key), self.assertRaises(web.HTTPForbidden):
                self.state.twiml({**self.params, key: 'wrong'})

    def test_ticket_cannot_start_another_call(self):
        self.state.twiml(self.params)
        with self.assertRaises(web.HTTPConflict):
            self.state.twiml({**self.params, 'CallSid': 'CA'+'2'*32})

    def test_receiver_token_has_no_outgoing_grant(self):
        self.state.key_sid = 'SK'+'3'*32
        self.state.key_secret = 'x'*32
        self.state.app_sid = 'AP'+'4'*32
        token = jwt.decode(self.state.mint(self.state.receiver, incoming=True), options={'verify_signature': False})
        self.assertTrue(token['grants']['voice']['incoming']['allow'])
        self.assertNotIn('outgoing', token['grants']['voice'])
        self.assertLessEqual(abs(token['exp'] - time.time() - 600), 2)


class WebBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_listener_has_no_ui_or_token_endpoint(self):
        proto = state()
        async with TestClient(TestServer(proto.public_app())) as client:
            for path in ['/', '/tokens', '/config', '/app.js']:
                response = await client.post(path)
                self.assertEqual(response.status, 404)
            response = await client.post('/voice/'+proto.nonce, data={})
            self.assertEqual(response.status, 403)

    async def test_signed_webhook_requires_call_policy_and_returns_client_only(self):
        proto = state()
        proto.origin = 'https://prototype.example'
        proto.ticket, proto.mode = 'ticket', 'probe'
        path = '/voice/'+proto.nonce
        params = {'AccountSid': proto.account, 'From': 'client:'+proto.identity,
                  'CallSid': 'CA'+'1'*32, 'Ticket': 'ticket', 'Mode': 'probe'}
        signature = RequestValidator('test-token').compute_signature(proto.origin+path, params)
        async with TestClient(TestServer(proto.public_app())) as client:
            with patch.object(proto, 'save_resources'):
                response = await client.post(path, data=params, headers={'X-Twilio-Signature': signature})
            self.assertEqual(response.status, 200)
            self.assertNotIn('<Number', await response.text())

    async def test_cross_origin_and_owner_token_requests_rejected(self):
        proto = state()
        async with TestClient(TestServer(proto.local_app())) as client:
            proto.local_origin = str(client.make_url('')).rstrip('/')
            response = await client.post('/tokens', json={'mode': 'probe'}, headers={'Origin': 'https://evil.example'})
            self.assertEqual(response.status, 403)
            response = await client.post('/tokens', json={'mode': 'owner'}, headers={'Origin': proto.local_origin})
            self.assertEqual(response.status, 403)
            response = await client.get('/config', headers={'Host': 'evil.example'})
            self.assertEqual(response.status, 403)


if __name__ == '__main__': unittest.main()
