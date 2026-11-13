"""Qwen3-TTS 0.6B Base cloning Friday's voice on the Windows laptop (WSL): CPU vs RTX 3050.

    python remote_tts_bench.py cpu|cuda
"""
import resource
import sys
import time

import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel

device = sys.argv[1] if len(sys.argv) > 1 else "cpu"
REF = "friday-ref.wav"
REF_TEXT = ("Hey, so, quick update. The AWS run finished, and it passed, so that's nice. SGLang's still going "
            "through the sweep. Nothing you need to do right now, though. I'll let you know if that changes.")
LINE = ("Hey Amun. So the AWS run finished, and it passed, ninety-seven point six on G S M eight K. "
        "Box two's just sitting there idle, though. Want me to shut it down?")

if device == "cpu":
    torch.set_num_threads(8)
kw = {"device_map": "cuda:0" if device == "cuda" else "cpu",
      "dtype": torch.bfloat16 if device == "cuda" else torch.float32,
      "attn_implementation": "sdpa"}
t = time.time()
model = Qwen3TTSModel.from_pretrained("Qwen/Qwen3-TTS-12Hz-0.6B-Base", **kw)
load = time.time() - t


def run(text):
    if device == "cuda":
        torch.cuda.synchronize()
    t = time.time()
    wavs, sr = model.generate_voice_clone(text=text, language="English", ref_audio=REF, ref_text=REF_TEXT)
    if device == "cuda":
        torch.cuda.synchronize()
    return time.time() - t, wavs[0], sr


run("Okay, got it. I'm here.")          # warm-up
if device == "cuda":
    torch.cuda.reset_peak_memory_stats()
gen, wav, sr = run(LINE)
secs = len(wav) / sr
sf.write(f"bench-{device}.wav", wav, sr)
mem = (f"GPU peak {torch.cuda.max_memory_allocated() / 2**30:.2f} GB" if device == "cuda"
       else f"RSS peak {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20:.2f} GB")
print(f"RESULT {device:4} load {load:5.1f}s  generate {gen:5.2f}s for {secs:4.1f}s of audio  RTF {gen / secs:.2f}  {mem}",
      flush=True)
