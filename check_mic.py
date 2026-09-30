"""
Run this to find your correct mic device index and channel count.
python check_mic.py
"""
import sounddevice as sd

print("\n=== ALL AUDIO DEVICES ===\n")
devices = sd.query_devices()
for i, d in enumerate(devices):
    if d['max_input_channels'] > 0:
        marker = " <<< INPUT" 
        print(f"  [{i:2d}] {d['name']}")
        print(f"        channels={d['max_input_channels']}, samplerate={int(d['default_samplerate'])}{marker}")

print("\n=== DEFAULT INPUT DEVICE ===")
try:
    default = sd.query_devices(kind='input')
    print(f"  Name:     {default['name']}")
    print(f"  Channels: {default['max_input_channels']}")
    print(f"  Rate:     {int(default['default_samplerate'])}")
    idx = sd.default.device[0]
    print(f"  Index:    {idx}")
except Exception as e:
    print(f"  Error: {e}")

print("\n=== TESTING device_index=20 ===")
try:
    d = sd.query_devices(20)
    print(f"  Name:     {d['name']}")
    print(f"  Channels: {d['max_input_channels']}")
    print(f"  Rate:     {int(d['default_samplerate'])}")
except Exception as e:
    print(f"  Device 20 error: {e}")

print("\n=== QUICK RECORD TEST (2 seconds, default mic) ===")
try:
    import numpy as np
    default = sd.query_devices(kind='input')
    ch = int(default['max_input_channels'])
    print(f"  Recording {ch}ch for 2 seconds...")
    audio = sd.rec(int(2 * 16000), samplerate=16000, channels=ch, dtype='int16')
    sd.wait()
    print(f"  SUCCESS — shape={audio.shape}, max={np.abs(audio).max()}")
    if ch > 1:
        mono = audio.mean(axis=1).astype('int16')
        print(f"  Downmixed to mono: shape={mono.shape}")
except Exception as e:
    print(f"  FAILED: {e}")

print("\nThis is a raw sounddevice dump for debugging — for actually picking a")
print("device, use Settings > Audio Devices in the app instead: it shows a")
print("clean, deduplicated list (see voice/audio_devices.py) instead of the")
print("one-row-per-host-API mess above, and applies your choice immediately.\n")