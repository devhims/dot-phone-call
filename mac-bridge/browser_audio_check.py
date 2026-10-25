"""A fresh spoken challenge must return through BlackHole before dialing.

macOS say generates only the test stimulus. Local Whisper recognizes only the
test response. Neither is used in the telephone conversation. Temporary audio
is removed on success, failure, timeout and cancellation.
"""
import asyncio
import json
from pathlib import Path
import re
import secrets
import tempfile
import time
import wave

import numpy as np
import soxr
from audio import AudioBridge, encode, decode

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT/'.runtime/ggml-base.en.bin'
WHISPER = Path('/opt/homebrew/bin/whisper-cli')
PROMPT_VERSION = 'last-middle-first-v1'


async def command(*args, timeout=20):
    process = await asyncio.create_subprocess_exec(*map(str, args),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
        if process.returncode:
            raise RuntimeError('AUDIO_CHECK_TOOL_FAILED')
        return stdout.decode(errors='replace')
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


def words(text):
    return re.findall(r'[a-z]+', text.lower())


class AudioCheckError(RuntimeError):
    def __init__(self, code, diagnostics):
        super().__init__(code)
        self.diagnostics=diagnostics


def ordered_match(actual, wanted):
    # Require every fresh challenge word in order, within a short response.
    # Small acknowledgments are harmless; a lone final word is not proof.
    for start in range(len(actual)):
        if not wanted or actual[start] != wanted[0]: continue
        position=1
        for token in actual[start+1:start+len(wanted)+4]:
            if position < len(wanted) and token == wanted[position]: position+=1
        if position == len(wanted): return True
    return False


def check_reply(samples, transcript, expected):
    peak = float(np.max(np.abs(samples), initial=0))
    # Reject silence before accepting any recognizer text. ASR can hallucinate
    # text on silence, and an arbitrary sound does not prove a round trip.
    active = float(np.count_nonzero(np.abs(samples) > .01)/8000)
    actual, wanted = words(transcript), words(expected)
    diagnostics={'stage':'browser_audio_preflight','phone_dialed':False,
        'expected_response':' '.join(wanted),
        'heard_challenge_words':[token for token in actual if token in wanted][:12],
        'reply_peak':round(peak,4),'active_seconds':round(active,2)}
    if peak < .01 or active < .15:
        raise AudioCheckError('DOT_AUDIO_SILENT',diagnostics)
    if not ordered_match(actual, wanted):
        raise AudioCheckError('DOT_AUDIO_REPLY_MISMATCH',diagnostics)
    return {'verified': True, 'reply_peak': round(peak, 4),
            'active_seconds': round(active, 2), 'expected': expected,
            'transcript': transcript.strip()}


async def settle_browser_audio(audio, *, canceled=None):
    """Allow voice startup and the opening greeting to finish before speaking."""
    audio.discard_reply()
    quiet_frames=0
    speech_detected=False
    start=time.monotonic()
    for i in range(500):
        if canceled is not None and canceled.is_set():
            raise RuntimeError('CALL_CANCELED_BEFORE_DIAL')
        audio.receive(b'\xff'*160)
        samples=decode(audio.next_packet())
        speaking=float(np.max(np.abs(samples),initial=0))>.01
        speech_detected |= speaking
        quiet_frames=0 if speaking else quiet_frames+1
        await asyncio.sleep(max(0,start+(i+1)*.02-time.monotonic()))
        # Require both a startup interval and uninterrupted quiet. The opening
        # greeting can begin after an initially silent connection.
        if i+1>=150 and quiet_frames>=50:
            return {'elapsed_seconds':round((i+1)*.02,2),
                    'quiet_seconds':round(quiet_frames*.02,2),
                    'speech_detected':bool(speech_detected)}
    raise RuntimeError('DOT_AUDIO_NOT_QUIET')


async def recognize_reply(samples, expected, reply, diagnostics=None, evidence=None):
    # Never invoke ASR on silence or insufficient speech.
    active_indices=np.flatnonzero(np.abs(samples)>.01)
    if len(active_indices)<1200:
        check_reply(samples,'',expected)
    first=max(0,int(active_indices[0])-2000)
    last=min(len(samples),int(active_indices[-1])+2001)
    speech_samples=samples[first:last]
    if diagnostics is not None:
        diagnostics['recognition_audio_seconds']=round(len(speech_samples)/8000,2)
    resampled=soxr.resample(speech_samples,8000,16000)
    with wave.open(str(reply),'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000)
        wav.writeframes((np.clip(resampled,-1,1)*32767).astype('<i2').tobytes())
    transcript=await command(WHISPER,'-m',MODEL,'-f',reply,'-l','en',
                             '-nt','-np','-ng',timeout=20)
    if diagnostics is not None: diagnostics['transcript']=transcript.strip()
    if evidence:
        import shutil
        shutil.copyfile(reply,evidence)
        evidence.with_suffix('.json').write_text(json.dumps({
            'expected':expected,'transcript':transcript.strip(),
            'reply_peak':float(np.max(np.abs(samples),initial=0))}))
    return check_reply(samples,transcript,expected)


async def verify_browser_audio(audio, *, response_seconds=22, evidence=None, canceled=None, diagnostics=None):
    verification_start=time.monotonic()
    if not MODEL.is_file() or not WHISPER.is_file():
        raise RuntimeError('AUDIO_CHECK_DEPENDENCY_MISSING')
    # A fresh phrase prevents an old recording or an unrelated reply from passing.
    stimulus = [secrets.choice(group) for group in (
        ('purple','yellow','silver','orange'), ('garden','window','mountain','river'),
        ('rabbit','tiger','eagle','dolphin'))]
    expected = ' '.join(reversed(stimulus))
    # Put an explicit positional instruction after the words too. A live Dot
    # transcript dropped "reverse" from the old single-clause instruction.
    # Never speak the expected sequence: a direct echo must still fail.
    prompt = ('Audio connection test. Reverse the word order. The words are: '
              + ', '.join(stimulus) + '. '
              'Reply with the last word, then the middle word, then the first word.')
    challenge={'prompt_version':PROMPT_VERSION,'prompt':prompt,'expected':expected}
    if diagnostics is not None: diagnostics.update(challenge)
    # This is generated test text only, never a transcript of the owner's speech.
    print(json.dumps({'event':'browser_audio_challenge',**challenge}),flush=True)
    with tempfile.TemporaryDirectory(prefix='dot-audio-', dir=ROOT/'.runtime') as directory:
        directory = Path(directory)
        phrase, reply = directory/'phrase.wav', directory/'reply.wav'
        await command('/usr/bin/say','-v','Samantha','-r','175','-o',phrase,
                      '--file-format=WAVE','--data-format=LEI16@8000',prompt)
        with wave.open(str(phrase)) as wav:
            speech = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').astype('float32')/32768
        startup=await settle_browser_audio(audio,canceled=canceled)
        if diagnostics is not None: diagnostics['startup_audio']=startup
        if isinstance(startup,dict):
            print(json.dumps({'event':'browser_audio_settled',**startup}),flush=True)
        audio.discard_reply()
        blocks = int(np.ceil(len(speech)/160))
        result=[]
        start=time.monotonic()
        limit=blocks+int(response_seconds*50)
        quiet_frames=active_samples=checked_active=0
        attempts=0
        pending=None
        last_error=None
        verified=None
        metrics=None

        def check_canceled():
            if canceled is not None and canceled.is_set():
                raise RuntimeError('CALL_CANCELED_BEFORE_DIAL')

        def launch_check():
            nonlocal checked_active,attempts
            checked_active=active_samples
            attempts+=1
            # A snapshot prevents concurrent capture from changing ASR input.
            return asyncio.create_task(recognize_reply(np.concatenate(result),
                expected,reply,diagnostics,evidence))

        def collect_check():
            nonlocal pending,last_error,verified
            task=pending
            pending=None
            try:
                verified=task.result()
            except AudioCheckError as error:
                # Greetings and partial replies are not terminal failures while
                # the response window is still open.
                last_error=error

        try:
            for i in range(limit):
                check_canceled()
                if pending is not None and pending.done():
                    collect_check()
                    if verified: break
                block=np.zeros(160,dtype='float32')
                if i<blocks:
                    chunk=speech[i*160:(i+1)*160]
                    block[:len(chunk)]=chunk
                audio.receive(encode(block))
                returned=decode(audio.next_packet())
                result.append(returned)
                count=int(np.count_nonzero(np.abs(returned)>.01))
                active_samples+=count
                quiet_frames=0 if count else quiet_frames+1
                # Finish injecting the prompt before dialing. Keep early replies,
                # then recognize after 600 ms of quiet. Capture keeps running
                # while the subprocess works, so later speech is not dropped.
                if (i+1>=blocks and quiet_frames>=30 and active_samples>=1200
                        and active_samples>checked_active and pending is None):
                    pending=launch_check()
                await asyncio.sleep(max(0,start+(i+1)*.02-time.monotonic()))
            check_canceled()
            # A final recognition is allowed at the deadline, including speech
            # without a clean pause. Cancellation also stops an in-flight ASR.
            while not verified:
                if pending is None:
                    if last_error is not None and active_samples==checked_active:
                        raise last_error
                    pending=launch_check()
                while not pending.done():
                    check_canceled()
                    await asyncio.wait([pending],timeout=.02)
                check_canceled()
                collect_check()
                if not verified and active_samples==checked_active:
                    raise last_error
            check_canceled()
            metrics=audio.metrics()
            audio.reset_for_call()
            return verified
        finally:
            if pending is not None:
                pending.cancel()
                try:
                    await pending
                except (asyncio.CancelledError,Exception):
                    pass
            elapsed=time.monotonic()-start
            if metrics is None: metrics=audio.metrics()
            if isinstance(metrics,dict):
                if diagnostics is not None: diagnostics['audio_metrics']=metrics
                print(json.dumps({'event':'browser_audio_test_metrics',**metrics}),flush=True)
            timings={'audio_check_seconds':round(time.monotonic()-verification_start,2),
                'stimulus_seconds':round(blocks*.02,2),
                'capture_seconds':round(len(result)*.02,2),
                'decision_after_stimulus_seconds':round(max(0,elapsed-blocks*.02),2),
                'unused_response_window_seconds':round(max(0,limit*.02-elapsed),2),
                'asr_attempts':attempts}
            if diagnostics is not None: diagnostics['timings']=timings
            print(json.dumps({'event':'browser_audio_check_timing',**timings}),flush=True)



async def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path)
    args = parser.parse_args()
    audio = AudioBridge()
    try:
        audio.start()
        print(json.dumps(await verify_browser_audio(audio, evidence=args.evidence)), flush=True)
    finally:
        audio.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except RuntimeError as error:
        print(json.dumps({'verified':False,'code':str(error)}), flush=True)
        raise SystemExit(1)
