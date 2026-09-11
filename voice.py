# voice.py — microphone in, speaker out

import io
import time
import numpy as np
import sounddevice as sd
import soundfile as sf
from openai import OpenAI
import config

oai = OpenAI(api_key=config.OPENAI_API_KEY)

SAMPLE_RATE = 16000          # 16 kHz mono is what speech models expect
SILENCE_THRESHOLD = 0.01     # below this volume counts as "silence" (tune if needed)
SILENCE_DURATION = 1.2       # stop after this many seconds of quiet
MAX_SECONDS = 15             # hard cap so it never records forever


def listen(on_level=None, should_stop=None) -> str:
    """Record from the mic until the user goes quiet, then return the transcript.
    The GUI passes on_level (gets the mic loudness, to animate the orb) and should_stop
    (returns True to give up early, e.g. when you mute the mic). Both are optional."""
    print("🎧 Listening...")
    chunks = []
    silent_for = 0.0
    block = int(SAMPLE_RATE * 0.1)  # process audio in 0.1s pieces

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", device=6) as stream:
        recorded = 0.0
        # Wait until the person actually starts talking (up to 8s), so leading
        # silence doesn't instantly end the recording.
        started = False
        while recorded < MAX_SECONDS:
            if should_stop and should_stop():
                return ""
            audio, _ = stream.read(block)
            chunks.append(audio.copy())
            recorded += 0.1
            volume = np.abs(audio).mean()
            if on_level:
                on_level(float(volume))

            if volume > SILENCE_THRESHOLD:
                started = True
                silent_for = 0.0
            elif started:
                silent_for += 0.1
                if silent_for >= SILENCE_DURATION:
                    break
            elif recorded > 8:   # nobody said anything
                break

    if not started:              # nobody spoke: skip the API call and report silence
        print("📝 (heard nothing)")
        return ""

    audio_data = np.concatenate(chunks, axis=0)

    # Put the recording into an in-memory WAV file (no temp file on disk)
    buf = io.BytesIO()
    sf.write(buf, audio_data, SAMPLE_RATE, format="WAV")
    buf.seek(0)
    buf.name = "speech.wav"  # the API wants a filename hint

    # Speech -> text
    result = oai.audio.transcriptions.create(
    model="gpt-transcribe",
    file=buf,
    prompt="Voice commands to a coding assistant named Jarvis about git, files, and CS coursework.",
    extra_body={
        "keywords": ["Jarvis", "git", "commit", "push", "pull", "repo", "cs101", "VS Code", "Spotify", "aight", "go offline", "stand down", "bruh", "mane", "like", "obsidian", "astra", "AI"],
        "languages": ["en"],
    },
)
    text = result.text.strip()
    print(f"📝 You said: {text}")
    return text


def synthesize(text: str):
    """Turn text into speech. Returns (samples, sample rate) without playing anything yet."""
    response = oai.audio.speech.create(
        model="gpt-4o-mini-tts",
        voice="ash",              # try: ash, onyx, ballad, sage, verse — pick a Jarvis vibe
        input=text,
        response_format="wav",
        instructions="Talk just like Jarvis from the MCU. Refined British accent. Crisp, brisk pace. Warm, extremely friendly, upbeat tone. Talk very fast and concise.",
        speed=1.3
    )
    # Load the returned WAV so it can be played through the speakers
    audio, sr = sf.read(io.BytesIO(response.content), dtype="float32")
    return audio, sr


def play(audio, sr, on_level=None):
    """Play audio and wait until it finishes, or until stop() cuts it off.
    on_level gets the loudness about 15 times a second (the GUI's orb uses it)."""
    sd.play(audio, sr)
    if on_level:
        start = time.monotonic()
        window = int(sr * 0.05)
        while sd.get_stream().active:
            i = int((time.monotonic() - start) * sr)
            piece = audio[i:i + window]
            if len(piece):
                on_level(float(np.sqrt(np.mean(np.square(piece)))))
            time.sleep(0.066)
    sd.wait()  # block until it finishes speaking


def stop():
    """Cut off whatever is playing right now."""
    sd.stop()


def speak(text: str):
    """Turn text into speech and play it out loud."""
    print(f"Jarvis: {text}")
    audio, sr = synthesize(text)
    play(audio, sr)