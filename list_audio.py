# list_audio.py — show the microphones Jarvis can listen through.
#
#   python list_audio.py           list every input device
#   python list_audio.py --test    record two seconds from the one .env selects
#
# Put the number (or any part of the name) of the one you want in .env as MIC_DEVICE.

import os
import sys

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv

load_dotenv()


def chosen_device():
    """What .env currently points at: a number, a name fragment, or None for the default."""
    value = os.getenv("MIC_DEVICE", "").strip()
    if not value:
        return None
    return int(value) if value.lstrip("-").isdigit() else value


def list_inputs():
    devices = sd.query_devices()
    default_in = sd.default.device[0] if sd.default.device else None

    print("Input devices:\n")
    found = False
    for index, device in enumerate(devices):
        if device["max_input_channels"] < 1:
            continue
        found = True
        host = sd.query_hostapis(device["hostapi"])["name"]
        default = "   <- Windows default" if index == default_in else ""
        print(f"  [{index:>2}] {device['name']}"
              f"  ({host}, {device['max_input_channels']} ch, {int(device['default_samplerate'])} Hz){default}")

    if not found:
        print("  none found — check that a microphone is plugged in and enabled in Windows.")
        return

    current = os.getenv("MIC_DEVICE", "").strip()
    print()
    if current:
        print(f".env says MIC_DEVICE={current}")
    else:
        print(".env has no MIC_DEVICE set, so Jarvis uses the Windows default.")
    print("Set it to a number or to part of a name, e.g. MIC_DEVICE=6  or  MIC_DEVICE=Blue Yeti")
    print("Then check it works with:  python list_audio.py --test")


def test_device(seconds=2, samplerate=16000):
    device = chosen_device()
    label = device if device is not None else "Windows default"
    print(f"Recording {seconds}s from {label}. Say something...")
    try:
        audio = sd.rec(int(seconds * samplerate), samplerate=samplerate,
                       channels=1, dtype="float32", device=device)
        sd.wait()
    except Exception as e:
        print(f"Couldn't open that device: {e}")
        print("Run `python list_audio.py` and set MIC_DEVICE to one of the numbers listed.")
        return

    peak = float(np.abs(audio).max())
    bar = "#" * int(peak * 40)
    print(f"peak {peak:.3f}  |{bar:<40}|")
    if peak < 0.01:
        print("That's silence. Wrong device, muted microphone, or the input volume is at zero.")
    elif peak < 0.05:
        print("Very quiet. It may work, but raise the input volume or move closer.")
    else:
        print("Sounds good — the wake word should hear you.")


if __name__ == "__main__":
    if "--test" in sys.argv:
        test_device()
    else:
        list_inputs()
