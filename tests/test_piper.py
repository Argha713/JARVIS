import subprocess
from pathlib import Path

piper = str(Path("models/piper/piper.exe").resolve())
model = str(Path("models/piper/en_US-lessac-medium.onnx").resolve())
output = str(Path("data/tts_output.wav").resolve())

print(f"Piper: {piper}")
print(f"Model: {model}")
print(f"Output: {output}")
print(f"Piper exists: {Path(piper).exists()}")
print(f"Model exists: {Path(model).exists()}")

result = subprocess.run(
    [piper, "--model", model, "--output_file", output],
    input=b"Hello sir JARVIS is online.",
    capture_output=True,
)
print(f"Return code: {result.returncode}")
print(f"Stdout: {result.stdout.decode()}")
print(f"Stderr: {result.stderr.decode()}")
print(f"WAV created: {Path(output).exists()}")
