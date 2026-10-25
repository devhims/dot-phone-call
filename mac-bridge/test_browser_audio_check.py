import unittest
import numpy as np
from browser_audio_check import check_reply


class ReplyTests(unittest.TestCase):
    def test_silence_never_passes_even_with_matching_asr(self):
        with self.assertRaisesRegex(RuntimeError, 'DOT_AUDIO_SILENT'):
            check_reply(np.zeros(16000), 'purple garden seven', 'purple garden seven')

    def test_unrelated_audio_and_stale_challenge_fail(self):
        with self.assertRaisesRegex(RuntimeError, 'DOT_AUDIO_REPLY_MISMATCH'):
            check_reply(np.ones(16000)*.1, 'yellow window nine', 'purple garden seven')

    def test_matching_spoken_reply_passes(self):
        self.assertTrue(check_reply(np.ones(16000)*.1,
            'Purple garden seven.', 'purple garden seven')['verified'])

    def test_acknowledgment_before_or_between_all_challenge_words_passes(self):
        for transcript in ('Okay, rabbit garden purple.', 'Rabbit garden, okay, purple.'):
            self.assertTrue(check_reply(np.ones(16000)*.1, transcript,
                                        'rabbit garden purple')['verified'])

    def test_last_word_only_and_untransformed_input_still_fail(self):
        for transcript in ('Okay, purple.', 'purple garden rabbit',
                           'rabbit one two three four five six seven eight nine garden purple'):
            with self.assertRaisesRegex(RuntimeError, 'DOT_AUDIO_REPLY_MISMATCH'):
                check_reply(np.ones(16000)*.1, transcript, 'rabbit garden purple')


class CaptureTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_during_stimulus_is_retained_without_accepting_stimulus_echo(self):
        import wave
        from unittest.mock import AsyncMock, MagicMock, patch
        from audio import encode
        from browser_audio_check import verify_browser_audio
        audio=MagicMock()
        index=0
        def receive(payload):
            nonlocal index
            index+=1
        audio.receive.side_effect=receive
        # Dot replies before the narrator has finished. The old implementation
        # discarded this whole response and returned DOT_AUDIO_SILENT.
        audio.next_packet.side_effect=lambda:encode(np.ones(160)*(.1 if 20 <= index < 30 else 0))
        async def fake_command(*args, **kwargs):
            if args[0]=='/usr/bin/say':
                output=args[args.index('-o')+1]
                with wave.open(str(output),'wb') as wav:
                    wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(8000)
                    wav.writeframes(np.zeros(8000,dtype='<i2').tobytes())
                return ''
            with wave.open(str(args[args.index('-f')+1])) as wav:
                self.assertLessEqual(wav.getnframes(),16000)
                self.assertGreater(np.abs(np.frombuffer(wav.readframes(wav.getnframes()),dtype='<i2')).max(),100)
            return 'Rabbit garden purple.'
        with patch('browser_audio_check.command',side_effect=fake_command), \
                patch('browser_audio_check.settle_browser_audio',new=AsyncMock()) as settle, \
                patch('browser_audio_check.Path.is_file',return_value=True), \
                patch('browser_audio_check.secrets.choice',side_effect=lambda group:group[0]), \
                patch('browser_audio_check.asyncio.sleep',new=AsyncMock()):
            proof=await verify_browser_audio(audio,response_seconds=1)
        settle.assert_awaited_once_with(audio,canceled=None)
        self.assertTrue(proof['verified'])
        self.assertEqual(proof['expected'],'rabbit garden purple')

    async def test_spoken_instruction_explains_reply_order_after_challenge_words(self):
        import wave
        from unittest.mock import AsyncMock, MagicMock, patch
        from audio import encode
        from browser_audio_check import verify_browser_audio
        audio=MagicMock()
        audio.next_packet.return_value=encode(np.ones(160)*.1)
        diagnostic={}
        async def fake_command(*args, **kwargs):
            if args[0]=='/usr/bin/say':
                prompt=args[-1]
                # The failed browser transcript lost "reverse". The instruction
                # must remain explicit without relying on that one word.
                instruction=prompt.split('yellow, window, tiger.',1)[-1]
                self.assertIn('last word, then the middle word, then the first word', instruction)
                self.assertNotIn('tiger window yellow', prompt.lower())
                with wave.open(str(args[args.index('-o')+1]),'wb') as wav:
                    wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(8000)
                    wav.writeframes(np.zeros(160,dtype='<i2').tobytes())
                return ''
            return 'Tiger window yellow.'
        with patch('browser_audio_check.command',side_effect=fake_command), \
                patch('browser_audio_check.settle_browser_audio',new=AsyncMock()), \
                patch('browser_audio_check.Path.is_file',return_value=True), \
                patch('browser_audio_check.secrets.choice',side_effect=['yellow','window','tiger']), \
                patch('browser_audio_check.asyncio.sleep',new=AsyncMock()):
            proof=await verify_browser_audio(audio,response_seconds=1,diagnostics=diagnostic)
        self.assertTrue(proof['verified'])
        self.assertEqual(proof['expected'],'tiger window yellow')
        self.assertEqual(diagnostic['prompt_version'],'last-middle-first-v1')


class StartupTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_opening_greeting_to_finish_before_stimulus(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from audio import encode
        from browser_audio_check import settle_browser_audio
        audio=MagicMock()
        frames=0
        def packet():
            nonlocal frames
            frames+=1
            # Initial quiet does not establish readiness. A greeting starts
            # after 1 second and finishes at 4 seconds.
            return encode(np.ones(160)*(.1 if 50 <= frames <= 200 else 0))
        audio.next_packet.side_effect=packet
        with patch('browser_audio_check.asyncio.sleep',new=AsyncMock()):
            result=await settle_browser_audio(audio)
        self.assertEqual(frames,250)
        self.assertTrue(result['speech_detected'])
        self.assertEqual(result['quiet_seconds'],1)

    async def test_continuous_audio_and_cancel_never_start_challenge(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch
        from audio import encode
        from browser_audio_check import settle_browser_audio
        audio=MagicMock()
        audio.next_packet.return_value=encode(np.ones(160)*.1)
        with patch('browser_audio_check.asyncio.sleep',new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError,'DOT_AUDIO_NOT_QUIET'):
                await settle_browser_audio(audio)
            canceled=asyncio.Event();canceled.set()
            with self.assertRaisesRegex(RuntimeError,'CALL_CANCELED_BEFORE_DIAL'):
                await settle_browser_audio(audio,canceled=canceled)


class EarlyValidationTests(unittest.IsolatedAsyncioTestCase):
    async def run_stream(self, segments, transcripts, *, hold_until=0, cancel_at=0):
        import asyncio
        import wave
        from unittest.mock import AsyncMock, MagicMock, patch
        from audio import encode
        from browser_audio_check import verify_browser_audio
        real_sleep=asyncio.sleep
        audio=MagicMock()
        state={'frames':0,'asr_calls':0,'asr_canceled':False}
        canceled=asyncio.Event()
        release=asyncio.Event()
        def receive(payload):
            state['frames']+=1
            if hold_until and state['frames']>=hold_until: release.set()
            if cancel_at and state['frames']>=cancel_at: canceled.set()
        audio.receive.side_effect=receive
        audio.next_packet.side_effect=lambda:encode(np.ones(160)*(.1 if any(
            lo<=state['frames']<hi for lo,hi in segments) else 0))
        async def tick(seconds): await real_sleep(0)
        async def fake_command(*args, **kwargs):
            if args[0]=='/usr/bin/say':
                with wave.open(str(args[args.index('-o')+1]),'wb') as wav:
                    wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(8000)
                    wav.writeframes(np.zeros(8000,dtype='<i2').tobytes())
                return ''
            state['asr_calls']+=1
            try:
                if hold_until: await release.wait()
                await real_sleep(0)
                return transcripts[min(state['asr_calls']-1,len(transcripts)-1)]
            except asyncio.CancelledError:
                state['asr_canceled']=True
                raise
        with patch('browser_audio_check.command',side_effect=fake_command), \
                patch('browser_audio_check.settle_browser_audio',new=AsyncMock()), \
                patch('browser_audio_check.Path.is_file',return_value=True), \
                patch('browser_audio_check.secrets.choice',side_effect=lambda group:group[0]), \
                patch('browser_audio_check.asyncio.sleep',side_effect=tick):
            try:
                state['proof']=await verify_browser_audio(audio,canceled=canceled)
            except RuntimeError as error:
                state['error']=str(error)
        state['reset']=audio.reset_for_call.called
        return state

    async def test_valid_reply_returns_early_while_capture_continues_during_asr(self):
        state=await self.run_stream([(60,80)],['rabbit garden purple'],hold_until=150)
        self.assertTrue(state['proof']['verified'])
        self.assertGreaterEqual(state['frames'],150)
        self.assertLess(state['frames'],200)
        self.assertEqual(state['asr_calls'],1)

    async def test_greeting_mismatch_keeps_listening_for_later_correct_reply(self):
        state=await self.run_stream([(60,80),(180,200)],['Hello there.','rabbit garden purple'])
        self.assertTrue(state['proof']['verified'])
        self.assertEqual(state['asr_calls'],2)
        self.assertLess(state['frames'],300)

    async def test_wrong_reply_waits_to_deadline_and_never_passes(self):
        state=await self.run_stream([(60,80)],['purple garden rabbit'])
        self.assertEqual(state['error'],'DOT_AUDIO_REPLY_MISMATCH')
        self.assertEqual(state['frames'],1150)
        self.assertEqual(state['asr_calls'],1)
        self.assertFalse(state['reset'])

    async def test_silence_never_launches_asr_or_passes(self):
        state=await self.run_stream([],['rabbit garden purple'])
        self.assertEqual(state['error'],'DOT_AUDIO_SILENT')
        self.assertEqual(state['asr_calls'],0)
        self.assertFalse(state['reset'])

    async def test_cancel_stops_pending_recognizer_before_any_success(self):
        state=await self.run_stream([(60,80)],['rabbit garden purple'],hold_until=5000,cancel_at=150)
        self.assertEqual(state['error'],'CALL_CANCELED_BEFORE_DIAL')
        self.assertTrue(state['asr_canceled'])
        self.assertFalse(state['reset'])
