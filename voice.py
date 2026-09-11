# voice.py — microphone in, speaker out

import io
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


def listen() -> str:
    """Record from the mic until the user goes quiet, then return the transcript."""
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
            audio, _ = stream.read(block)
            chunks.append(audio.copy())
            recorded += 0.1
            volume = np.abs(audio).mean()

            if volume > SILENCE_THRESHOLD:
                started = True
                silent_for = 0.0
            elif started:
                silent_for += 0.1
                if silent_for >= SILENCE_DURATION:
                    break
            elif recorded > 8:   # nobody said anything
                break

    audio_data = np.concatenate(chunks, axis=0)

    # Put the recording into an in-memory WAV file (no temp file on disk)
    buf = io.BytesIO()
    sf.write(buf, audio_data, SAMPLE_RATE, format="WAV")
    buf.seek(0)
    buf.name = "speech.wav"  # the API wants a filename hint

    # Speech -> text
    result = oai.audio.transcriptions.create(
        model="gpt-4o-mini-transcribe",   # cheap + good; "gpt-4o-transcribe" is more accurate
        file=buf, 
        
    )
    text = result.text.strip()
    print(f"📝 You said: {text}")
    return text


def speak(text: str):
    """Turn text into speech and play it out loud."""
    print(f"Jarvis: {text}")
    response = oai.audio.speech.create(
        model="gpt-4o-mini-tts",
        voice="ash",              # try: ash, onyx, ballad, sage, verse — pick a Jarvis vibe
        input=text,
        response_format="wav",
        instructions="Talk just like Jarvis from the MCU. Refined British accent. Crisp, brisk pace. Warm, extremely friendly, upbeat tone. Talk very fast and concise.",
        speed=1.3
    )
    # Load the returned WAV and play it through the speakers
    audio, sr = sf.read(io.BytesIO(response.content), dtype="float32")
    sd.play(audio, sr)
    sd.wait()  # block until it finishes speaking