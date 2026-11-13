"""One voice, one queue: every spoken word on the Mac goes through here, so an alert never talks
over a reply and `stop` silences everything at once.

Audio goes to a single PortAudio output stream in 100 ms chunks: no process per sentence, no gaps
while the next sentence is synthesized (it renders while the current one plays), and `stop` takes
effect within one chunk."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
import heapq
import itertools
import logging
import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

log = logging.getLogger("fridayd.speech")

PRIORITY = {"high": 0, "normal": 1, "low": 2}
CHUNK_S = 0.1


def default_output_device() -> Optional[int]:
    """macOS's current default output device (CoreAudio's id), or None if it can't be read.
    PortAudio reads the device list once, when it starts: a speaker that connects later (Bluetooth)
    is invisible to it until it's re-initialized, so the speaker checks this before each utterance."""
    try:
        import ctypes
        import ctypes.util
        ca = _coreaudio[0] if _coreaudio else None
        if ca is None:
            ca = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreAudio"))
            _coreaudio.append(ca)

        class Address(ctypes.Structure):
            _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]

        def fourcc(s: str) -> int:
            return int.from_bytes(s.encode(), "big")
        addr = Address(fourcc("dOut"), fourcc("glob"), 0)   # kAudioHardwarePropertyDefaultOutputDevice
        dev, size = ctypes.c_uint32(0), ctypes.c_uint32(4)
        status = ca.AudioObjectGetPropertyData(ctypes.c_uint32(1), ctypes.byref(addr), ctypes.c_uint32(0), None,
                                               ctypes.byref(size), ctypes.byref(dev))
        return dev.value if status == 0 else None
    except Exception:
        return None


_coreaudio: list = []


def reload_audio_devices() -> str:
    """Re-read the device list (PortAudio caches it from startup). Only with no stream open."""
    try:
        sd._terminate()
        sd._initialize()
        return sd.query_devices(kind="output")["name"]
    except Exception as e:
        log.warning("reloading audio devices failed: %s", e)
        return "?"

# How words should be said (whole-word, case-sensitive). TTS models read spelling, not phonetics,
# so the fix is a respelling. Extend or override in friday.toml under [pronunciations].
PRONUNCIATIONS = {
    "Aman": "Amun",
    "SGLang": "S G Lang",
    "vLLM": "V L L M",
    "GSM8K": "G S M eight K",
    "B200": "B two hundred",
    "B300": "B three hundred",
    "H100": "H one hundred",
    "H200": "H two hundred",
    "trn2": "Trainium two",
}


def pronounce(text: str, table: dict[str, str]) -> str:
    if not table:
        return text
    pattern = re.compile(r"(?<!\w)(" + "|".join(map(re.escape, sorted(table, key=len, reverse=True))) + r")(?!\w)")
    return pattern.sub(lambda m: table[m.group(1)], text)


def speakable(text: str, pronunciations: Optional[dict[str, str]] = None) -> str:
    """Turn chat-formatted text into something worth hearing."""
    text = re.sub(r"```.*?```", " (code omitted) ", text, flags=re.S)
    text = re.sub(r"`([^`]{1,40})`", r"\1", text)
    text = re.sub(r"`[^`]+`", " (code) ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "(link)", text)
    text = re.sub(r"^\s*\|.*\|\s*$", " ", text, flags=re.M)        # tables don't read aloud
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.M)
    text = re.sub(r"[*_~]{1,3}([^*_~]+)[*_~]{1,3}", r"\1", text)
    text = re.sub(r"(?<!\w)(?:/[\w.\-]+){3,}/?", lambda m: Path(m.group(0)).name or "a path", text)
    text = text.replace("→", " to ").replace("≈", " about ").replace("&", " and ")
    text = pronounce(text, PRONUNCIATIONS if pronunciations is None else pronunciations)
    return re.sub(r"\s+", " ", text).strip()


def sentences(text: str) -> list[str]:
    """Sentence chunks; the first is kept short (split at a comma) so speech starts sooner."""
    parts = [p for p in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'])", text) if p.strip()]
    out: list[str] = []
    for p in parts:
        if out and len(out[-1]) < 40:  # glue tiny fragments so prosody doesn't stutter
            out[-1] = f"{out[-1]} {p}"
        else:
            out.append(p)
    if out and len(out[0]) > 90:
        head, sep, tail = out[0].partition(", ")
        if sep and 15 < len(head) < 90:
            out[0:1] = [head + ",", tail]
    return out


class KokoroEngine:
    """Kokoro-82M via ONNX Runtime (int8, CPU): ~0.08x real time on an M3 Max."""

    def __init__(self, model_dir: Path, voice: str = "af_heart", speed: float = 1.15, int8: bool = True):
        import onnxruntime as ort
        from kokoro_onnx import Kokoro
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        model = model_dir / ("kokoro-v1.0.int8.onnx" if int8 else "kokoro-v1.0.onnx")
        session = ort.InferenceSession(str(model), opts, providers=["CPUExecutionProvider"])
        self.tts = Kokoro.from_session(session, str(model_dir / "voices-v1.0.bin"))
        self.voice, self.speed = voice, speed
        self.lang = "en-gb" if voice.startswith("b") else "en-us"
        self.sample_rate = 24000

    def render(self, text: str) -> np.ndarray:
        audio, sr = self.tts.create(text, voice=self.voice, speed=self.speed, lang=self.lang)
        self.sample_rate = sr
        return audio.astype(np.float32)


class MlxEngine:
    """Any mlx-audio TTS model on the Apple GPU, streamed in chunks as it generates.

    ``options`` pass straight to ``model.generate`` (voice, instruct, ref_audio, ref_text, ...)."""

    def __init__(self, repo: str, seed: Optional[int] = None, **options):
        import mlx.core as mx
        from mlx_audio.tts.utils import load_model
        self.mx = mx
        self.model = load_model(repo)
        self.seed = seed  # same seed every line keeps a sampled voice (VoiceDesign) from drifting
        self.options = {k: v for k, v in options.items() if v not in (None, "")}
        self.sample_rate = 24000
        for _ in self.stream("Okay, got it. I'm here."):  # compile kernels and learn the output rate
            pass

    def stream(self, text: str, attempts: int = 3):
        """Yield audio chunks. Sampling models occasionally end a short input at once with no audio
        (seen with Qwen3-TTS on one-word lines), so an empty result is retried."""
        for attempt in range(attempts):
            if self.seed is not None:
                self.mx.random.seed(self.seed + attempt)
            produced = False
            for res in self.model.generate(text=text, stream=True, verbose=False, **self.options):
                self.mx.eval(res.audio)
                self.sample_rate = res.sample_rate
                chunk = np.array(res.audio, dtype=np.float32).reshape(-1)
                if chunk.size:
                    produced = True
                    yield chunk
            if produced:
                return
        log.warning("no audio after %d attempts for %r", attempts, text[:60])

    def render(self, text: str) -> np.ndarray:
        parts = list(self.stream(text))
        return np.concatenate(parts) if parts else np.zeros(0, np.float32)


class RemoteEngine:
    """Friday's voice generated on the Windows laptop (qwentts.cpp on its RTX 3050) and streamed here as
    16-bit 24 kHz PCM through the SSH tunnel. Playback stays on the Mac."""

    def __init__(self, url: str, timeout: float = 4, first_chunk_frames: int = 2):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.first_chunk_frames = first_chunk_frames
        self.sample_rate = 24000

    def healthy(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=1.5) as r:
                return r.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def stream(self, text: str):
        body = json.dumps({"input": text, "language": "English", "response_format": "pcm",
                           "first_chunk_frames": self.first_chunk_frames}).encode()
        req = urllib.request.Request(f"{self.url}/v1/audio/speech", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            buf = b""
            while True:
                data = resp.read1(16384)
                if not data:
                    break
                buf += data
                n = len(buf) // 2 * 2
                if n >= 4800:                       # ≥ 0.1 s per chunk
                    yield np.frombuffer(buf[:n], dtype="<i2").astype(np.float32) / 32768.0
                    buf = buf[n:]
            if len(buf) >= 2:
                n = len(buf) // 2 * 2
                yield np.frombuffer(buf[:n], dtype="<i2").astype(np.float32) / 32768.0

    def render(self, text: str) -> np.ndarray:
        parts = list(self.stream(text))
        return np.concatenate(parts) if parts else np.zeros(0, np.float32)


class FallbackEngine:
    """Laptop first; the Mac's own MLX voice when the laptop doesn't answer (asleep, off, tunnel down).
    The Mac voice is loaded only when needed and unloaded after a quiet stretch, so its ~2.6 GB stays
    free while the laptop is serving."""

    def __init__(self, remote: RemoteEngine, make_local: Callable[[], "MlxEngine"],
                 recheck_s: float = 30, unload_after_s: float = 600):
        self.remote, self.make_local = remote, make_local
        self.recheck_s, self.unload_after_s = recheck_s, unload_after_s
        self.local: Optional[MlxEngine] = None
        self.remote_down_until = 0.0
        self.last_local_use = 0.0
        self.using = "laptop"
        self.sample_rate = 24000
        self._lock = threading.Lock()

    def _get_local(self) -> "MlxEngine":
        with self._lock:
            if self.local is None:
                log.info("laptop voice unavailable: loading the Mac voice")
                self.local = self.make_local()
            return self.local

    def _maybe_unload(self) -> None:
        if self.local is not None and time.time() - self.last_local_use > self.unload_after_s:
            with self._lock:
                self.local = None
            import gc
            gc.collect()
            try:
                import mlx.core as mx
                mx.clear_cache()
            except ImportError:
                pass
            log.info("laptop voice is back: unloaded the Mac voice")

    def stream(self, text: str):
        if time.time() >= self.remote_down_until:
            produced = False
            try:
                for chunk in self.remote.stream(text):
                    produced = True
                    yield chunk
                self.using = "laptop"
                self._maybe_unload()
                return
            except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError) as e:
                log.warning("laptop voice failed (%s); using the Mac voice for %ss", e, int(self.recheck_s))
                self.remote_down_until = time.time() + self.recheck_s
                if produced:
                    return          # part of this sentence already played; don't repeat it
        self.using = "mac"
        self.last_local_use = time.time()
        yield from self._get_local().stream(text)

    def render(self, text: str) -> np.ndarray:
        parts = list(self.stream(text))
        return np.concatenate(parts) if parts else np.zeros(0, np.float32)


class PiperEngine:
    def __init__(self, model: Path, length_scale: float = 0.85):
        from piper import PiperVoice, SynthesisConfig
        self.voice = PiperVoice.load(str(model))
        self.config = SynthesisConfig(length_scale=length_scale)
        self.sample_rate = self.voice.config.sample_rate

    def render(self, text: str) -> np.ndarray:
        chunks = [c.audio_float_array for c in self.voice.synthesize(text, syn_config=self.config)]
        return np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(0, np.float32)


MAX_CHUNK_CHARS = 300   # one generation per ~20 s of speech: long enough to hold one voice, short enough to stay stable


def group_chunks(parts: list[str], whole: bool, lead_alone: bool = True) -> list[str]:
    """How text is split into generations. Engines that stream audio themselves (``whole``) get the
    first sentence alone (``lead_alone``), so speech starts fast, then the rest in large pieces, so the
    voice stays the same across sentences. Others get one sentence per generation."""
    if not whole or len(parts) <= 1:
        return parts
    out, cur = ([parts[0]], "") if lead_alone else ([], "")
    for p in (parts[1:] if lead_alone else parts):
        if cur and len(cur) + 1 + len(p) > MAX_CHUNK_CHARS:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}" if cur else p
    if cur:
        out.append(cur)
    return out


@dataclass(order=True)
class Utterance:
    priority: int
    seq: int
    text: str = field(compare=False)
    source: str = field(compare=False, default="reply")
    created: float = field(compare=False, default_factory=time.time)
    on_unspoken: Optional[Callable[["Utterance", str], None]] = field(compare=False, default=None)
    wanted: Optional[Callable[[], bool]] = field(compare=False, default=None)   # False when it comes up: skip it
    feed: Optional[queue.Queue] = field(compare=False, default=None)   # text still arriving (LLM stream)


class SpeechStream:
    """An utterance whose text arrives while it is being spoken: ``feed()`` pieces, then ``close()``."""

    def __init__(self, q: queue.Queue):
        self._q = q

    def feed(self, text: str) -> None:
        if text:
            self._q.put(text)

    def close(self) -> None:
        self._q.put(None)


class Speaker(threading.Thread):
    """Plays utterances in priority order. Waits while the mic is in use (a call, or Wispr Flow
    dictation); if the mic stays busy past ``mic_hold_s`` it is a call, and the utterance is handed
    to ``on_unspoken`` (a notification / the away recap) instead of being spoken over it."""

    def __init__(self, engine, mic_busy: Callable[[], bool], mic_hold_s: float = 90,
                 pronunciations: Optional[dict[str, str]] = None):
        super().__init__(name="speaker", daemon=True)
        self.engine = engine
        self.whole = hasattr(engine, "stream")
        self.mic_busy = mic_busy
        self.mic_hold_s = mic_hold_s
        self.pronunciations = {**PRONUNCIATIONS, **(pronunciations or {})}
        self._heap: list[Utterance] = []
        self._cv = threading.Condition()
        self._seq = itertools.count()
        self._generation = 0
        self._render = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")
        self.speaking: Optional[str] = None  # the utterance being delivered (a streamed answer stays open while Friday works)
        self.line: Optional[str] = None      # the piece being played right now (captions)
        self.source: Optional[str] = None
        self.audible = False                 # sound is actually coming out (not a pause while Friday works)
        self.muted = False                   # quiet mode: captions only (FridayEars shows them), nothing played
        self._out_device: Optional[int] = default_output_device()   # what PortAudio was started with
        self.recent: list[tuple[float, str]] = []   # (when, text) of what she said lately: to spot the mic hearing her
        self.on_audible: Optional[Callable[[str, bool, str], None]] = None   # (source, on, line)
        self.engine.render("Ready. I'm here.")  # load and warm the model before the first real utterance

    @property
    def talking(self) -> bool:
        return self.speaking is not None and self.audible

    def _set_audible(self, on: bool, source: str, line: Optional[str]) -> None:
        if on == self.audible:
            return
        self.audible = on
        if self.on_audible:
            try:
                self.on_audible(source, on, line or "")
            except Exception:
                log.exception("on_audible failed")

    def _push(self, utt: Utterance) -> None:
        with self._cv:
            heapq.heappush(self._heap, utt)
            self._cv.notify()

    def say(self, text: str, priority: str = "normal", source: str = "reply",
            on_unspoken: Optional[Callable[[Utterance, str], None]] = None,
            wanted: Optional[Callable[[], bool]] = None) -> bool:
        text = speakable(text, self.pronunciations)
        if not text:
            return False
        self._push(Utterance(PRIORITY.get(priority, 1), next(self._seq), text, source, on_unspoken=on_unspoken,
                             wanted=wanted))
        return True

    def open_stream(self, priority: str = "normal", source: str = "voice") -> SpeechStream:
        q: queue.Queue = queue.Queue()
        self._push(Utterance(PRIORITY.get(priority, 1), next(self._seq), "", source, feed=q))
        return SpeechStream(q)

    def said_recently(self, text: str, within_s: float = 20) -> bool:
        """Did the follow-up mic just hear her through the speaker? True when the reply is, word for word in
        order, (mostly) a run of what she said in the last ``within_s``. Reusing a few of her words ("do it",
        "rerun it", "tell me more") isn't an echo."""
        import difflib
        def norm(t: str) -> list[str]:          # "OK" and "okay" are the same word to a recognizer
            return ["okay" if w == "ok" else w for w in re.findall(r"[a-z0-9']+", t.lower())]
        words = norm(text)
        if not words:
            return False
        cutoff = time.time() - within_s
        spoken = norm(" ".join(t for at, t in self.recent if at >= cutoff))
        if not spoken:
            return False
        if len(words) <= 3:                       # short: only if it's exactly the tail of something she said
            return " ".join(spoken).endswith(" ".join(words)) or " ".join(words) in " ".join(spoken).split(". ")
        m = difflib.SequenceMatcher(None, words, spoken, autojunk=False).find_longest_match(0, len(words), 0, len(spoken))
        return m.size >= 0.8 * len(words)

    def stop(self) -> int:
        """Stop: silence now and clear the queue. Queued alerts aren't lost: they go back through
        ``on_unspoken`` (held for the recap and the panel). Returns how many utterances were dropped."""
        with self._cv:
            dropped = list(self._heap)
            self._heap.clear()
            self._generation += 1
            n = len(dropped) + (1 if self.speaking else 0)
        for utt in dropped:
            if utt.on_unspoken:
                try:
                    utt.on_unspoken(utt, "stopped")
                except Exception:
                    log.exception("on_unspoken failed")
        return n

    def hush(self) -> None:
        """He started talking (push-to-talk): cut what she's saying now. The queue stays; it plays after
        (the mic is busy until he's done)."""
        with self._cv:
            self._generation += 1

    def queued(self) -> int:
        with self._cv:
            return len(self._heap)

    def run(self) -> None:
        while True:
            with self._cv:
                while not self._heap:
                    self._cv.wait()
                utt = heapq.heappop(self._heap)
                gen = self._generation
            if utt.wanted is not None:
                try:
                    if not utt.wanted():
                        continue          # e.g. a permission question that's been answered or expired
                except Exception:
                    pass
            try:
                self._speak(utt, gen)
            except Exception:
                log.exception("speaking failed: %r", utt.text[:80])

    def _open_output(self, sr: int):
        """An output stream on macOS's current default device: follows a Bluetooth speaker that connected
        (or a device that disappeared) since the last utterance. One retry after a reload if it fails."""
        current = default_output_device()
        if current is not None and current != self._out_device:
            name = reload_audio_devices()
            log.info("default output changed (%s -> %s): now playing on %s", self._out_device, current, name)
            self._out_device = current
        for attempt in (1, 2):
            try:
                stream = sd.OutputStream(samplerate=sr, channels=1, dtype="float32", latency="low")
                stream.start()
                return stream
            except sd.PortAudioError as e:
                if attempt == 2:
                    raise
                log.warning("opening the audio output failed (%s); reloading devices and retrying", e)
                reload_audio_devices()
                self._out_device = default_output_device()

    def _wait_for_mic(self, gen: int) -> bool:
        deadline = time.time() + self.mic_hold_s
        while self.mic_busy():
            if time.time() > deadline or gen != self._generation:
                return False
            time.sleep(1)
        return True

    def _chunks(self, utt: Utterance, gen: int):
        """Text pieces to synthesize, in order. A fed utterance is cut at sentence ends as text streams
        in: the first sentence goes alone (fast start), later pieces wait for >= 120 characters."""
        if utt.feed is None:
            yield from group_chunks(sentences(utt.text), self.whole)
            return
        buf, first = "", True
        while gen == self._generation:
            try:
                piece = utt.feed.get(timeout=0.4)
                stalled = False
            except queue.Empty:
                piece, stalled = "", True   # the text paused: Friday said something and went off to use a tool
            done = piece is None
            if not done:
                buf += piece
                if stalled and re.search(r"[.!?:;]['\")\]]?\s*$", buf):
                    # A finished sentence and nothing more coming for now ("Checking master_sglang now."):
                    # say it now, not after the tool, when the next text would have shown where it ends.
                    ready, buf = buf, ""
                else:
                    ends = [m.end() for m in re.finditer(r"[.!?:;]['\")\]]?(?=\s)", buf)]
                    if not ends:
                        continue
                    cut = ends[0] if first else ends[-1]
                    if not first and self.whole and cut < 120:
                        continue
                    ready, buf = buf[:cut], buf[cut:]
            else:
                ready, buf = buf, ""
            text = speakable(ready, self.pronunciations)
            if text:
                for chunk in [text] if first else group_chunks(sentences(text), self.whole, lead_alone=False):
                    yield chunk
                first = False
            if done:
                return

    def _speak(self, utt: Utterance, gen: int) -> None:
        if not self.muted and not self._wait_for_mic(gen):
            if gen == self._generation:
                log.info("mic busy for %ss, not speaking: %s", self.mic_hold_s, utt.text[:80])
                if utt.on_unspoken:
                    utt.on_unspoken(utt, "mic_busy")
            return
        started = time.time()
        sr = self.engine.sample_rate
        audio_q: queue.Queue = queue.Queue(maxsize=64)

        abort = threading.Event()             # the player gave up (e.g. no output device): stop synthesizing

        def produce() -> None:  # generation runs ahead of playback, chunk by chunk
            try:
                for part in self._chunks(utt, gen):
                    if gen != self._generation or abort.is_set():
                        return
                    if self.muted:   # quiet mode: each sentence becomes a caption, nothing is synthesized
                        for sent in sentences(part) or [part]:
                            audio_q.put((None, sent))
                        continue
                    chunks = self.engine.stream(part) if self.whole else [self.engine.render(part)]
                    for c in chunks:
                        if gen != self._generation:
                            return
                        audio_q.put((c, part))
                    audio_q.put((np.zeros(int(sr * 0.12), np.float32), part))  # breath between generations
            except Exception:
                log.exception("synthesis failed")
            finally:
                audio_q.put(None)

        self._render.submit(produce)
        chunk = int(sr * CHUNK_S)
        first = True
        drained = False
        stream = None            # opened at the first real audio, so quiet mode never touches the output device
        aborted = False
        try:
            quiet_since: Optional[float] = None
            while True:
                try:
                    item = audio_q.get(timeout=0.2)
                except queue.Empty:
                    if gen != self._generation:
                        break      # stopped while waiting for more text; the finally unblocks the producer
                    # Silence longer than a sentence break: Friday said something ("let me check") and is
                    # now working. Report it, so the island shows the step instead of a frozen caption.
                    quiet_since = quiet_since or time.time()
                    if self.audible and time.time() - quiet_since > 0.8:
                        self.line = None
                        self._set_audible(False, utt.source, None)
                    continue
                if item is None:
                    drained = True
                    break
                quiet_since = None
                audio, self.line = item
                if first:   # "speaking" from the first sound: a streamed answer waits here while Friday works
                    self.speaking = utt.text or "(streaming)"
                    self.source = utt.source
                    log.info("first %s after %.0f ms (%s)", "caption" if audio is None else "audio",
                             (time.time() - started) * 1000, utt.source)
                    first = False
                self._set_audible(True, utt.source, self.line)
                if not self.recent or self.recent[-1][1] != self.line:
                    self.recent.append((time.time(), self.line or ""))
                    del self.recent[:-40]
                if audio is None:   # a caption: up long enough to read (about 3 words a second)
                    until = time.time() + min(12.0, max(1.5, len((self.line or "").split()) / 3.2))
                    while time.time() < until:
                        if gen != self._generation:
                            return
                        time.sleep(0.05)
                    continue
                if self.muted:      # quiet mode switched on mid-sentence: drop the rest of this audio
                    continue
                if stream is None:
                    stream = self._open_output(sr)
                for j in range(0, len(audio), chunk):
                    if gen != self._generation or self.muted:
                        stream.abort()
                        aborted = True
                        if gen != self._generation:
                            return
                        break
                    stream.write(audio[j:j + chunk].reshape(-1, 1))
                if aborted and self.muted:
                    stream.close()
                    stream, aborted = None, False
        finally:
            abort.set()
            if stream is not None:
                try:
                    if not aborted:
                        stream.stop()      # let the last chunk finish playing
                    stream.close()
                except Exception:
                    log.debug("closing the audio stream failed", exc_info=True)
            self._set_audible(False, utt.source, None)
            self.speaking = None
            self.line = None
            if utt.feed is not None and gen != self._generation:
                utt.feed.put(None)   # wake a producer blocked waiting for text
            while not drained and audio_q.get() is not None:  # unblock the producer so the next utterance can run
                pass
