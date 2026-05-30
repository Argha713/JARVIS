import sounddevice as sd

print("All input-capable devices:")
for i, d in enumerate(sd.query_devices()):
    if d["max_input_channels"] > 0:
        api = sd.query_hostapis(d["hostapi"])["name"]
        print(f"  [{i}] {d['name']!r} | api={api} | {d['max_input_channels']}ch | native={d['default_samplerate']}Hz")

print()
print("Default input device:")
print(sd.query_devices(kind="input"))

print()
print("Checking WASAPI input device settings:")
for i, d in enumerate(sd.query_devices()):
    if d["max_input_channels"] > 0:
        api = sd.query_hostapis(d["hostapi"])["name"]
        if "WASAPI" in api:
            for rate in [16000, 44100, 48000]:
                try:
                    sd.check_input_settings(device=i, samplerate=rate, channels=1)
                    print(f"  [{i}] {d['name']!r} @ {rate}Hz: OK")
                except Exception as e:
                    print(f"  [{i}] {d['name']!r} @ {rate}Hz: FAIL - {e}")
