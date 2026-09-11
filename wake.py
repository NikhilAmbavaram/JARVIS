# wake.py — listen locally for "Hey Jarvis"

import numpy as np
import sounddevice as sd
from openwakeword.model import Model

SAMPLE_RATE = 16000
CHUNK = 1280          # openWakeWord expects 80ms frames (1280 samples @ 16kHz)
THRESHOLD = 0.5       # 0-1 confidence; raise if it triggers too easily

_model = None         # loaded the first time, then reused, so dozing off doesn't reload it every time

def wait_for_wake_word():
    """Block until the user says 'Hey Jarvis'. Runs fully locally & free."""
    global _model
    if _model is None:
        # "hey_jarvis" is a built-in pretrained model — perfect for us.
        _model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
    else:
        _model.reset()  # forget last time's audio, or the old "Hey Jarvis" could wake him straight back up
    print("Offline")

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        blocksize=CHUNK, device=6) as stream:
        while True:
            audio, _ = stream.read(CHUNK)      # CHUNK = how much to read, audio = what you got
            frame = np.squeeze(audio)
            prediction = _model.predict(frame)
            if prediction["hey_jarvis"] > THRESHOLD:
                print("Woke up")
                return