import json
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from xml.etree import ElementTree

from registered_owner import OwnerTest, consume_arm


class OwnerPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = {'TWILIO_ACCOUNT_SID': 'AC' + '1' * 32, 'TWILIO_AUTH_TOKEN': 'fake',
                       'INBOUND_OWNER_NUMBER': '+15555550101', 'INBOUND_TWILIO_NUMBER': '+15555550102'}

    def test_only_saved_number_and_one_registered_client(self):
        probe = OwnerTest(self.config, Mock(), {'id': 'job', 'duration_seconds': 120})
        options = probe.call_options()
        self.assertEqual(options['to'], self.config['INBOUND_OWNER_NUMBER'])
        self.assertEqual(options['from_'], self.config['INBOUND_TWILIO_NUMBER'])
        self.assertFalse(options['record'])
        root = ElementTree.fromstring(options['twiml'])
        self.assertEqual([e.text for e in root.findall('Dial/Client')], probe.identities)
        self.assertIsNone(root.find('.//Number'))
        self.assertEqual(options['time_limit'], 135)
        self.assertNotIn('url', options)
        self.assertNotIn('status_callback', options)

    async def test_arm_is_exact_job_expiring_and_consumed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'arm.json'
            with patch('registered_owner.ARM', path):
                self.assertFalse(await consume_arm({'id': 'job'}))
                path.write_text(json.dumps({'call_id': 'job', 'expires_at': time.time()+60}))
                self.assertFalse(await consume_arm({'id': 'other'}))
                self.assertFalse(await consume_arm({'id': 'job', 'direction': 'inbound'}))
                self.assertTrue(await consume_arm({'id': 'job'}))
                self.assertFalse(await consume_arm({'id': 'job'}))
                path.write_text(json.dumps({'call_id': 'job', 'expires_at': time.time()-1}))
                self.assertFalse(await consume_arm({'id': 'job'}))

    def test_live_audio_has_no_generated_fixture(self):
        probe = OwnerTest(self.config, Mock(), {'id': 'job', 'duration_seconds': 120})
        with patch('registered_probe.sd.query_devices', return_value=[
                {'name': 'BlackHole 16ch', 'max_output_channels': 16}]), \
             patch('registered_probe.sd.OutputStream') as output:
            probe.start_native_audio(fixture=False)
            output.assert_called_once()
            self.assertEqual(output.call_args.kwargs['device'], 0)


if __name__ == '__main__':
    unittest.main()
