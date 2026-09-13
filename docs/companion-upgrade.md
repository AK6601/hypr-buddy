# Shiro companion upgrade

The previous prototype mixed physical monitor dimensions with logical window coordinates,
rendered text as blocks, switched between inconsistent generated poses, and captured voice
through a terminal loop that resumed before speech playback ended.

## What changed

- A transparent, registered Shiro illustration plus an eye-closed variant replace the default
  pose strips. Continuous breathing, tilt, greeting sway, small happy bounces, blinking, and
  a speech-state mouth aperture preserve the same character silhouette. Actual playback starts
  and stops the talking state. This is speech activity animation, not phoneme lip synchronization.
- Text uses bundled, licensed DejaVu glyphs. Bubble dimensions remain stable while revealing
  text, wrap Unicode and long words, and stay inside the output. Long replies scroll through
  eight visible lines; the chat socket retains the complete reply.
- Monitor geometry handles scale, rotated outputs, negative origins, panel reservations,
  empty workspaces, and layout changes. The least-obscuring corner wins, favoring lower corners
  on ties. The sprite size comes from overlay.toml. Unbounded focus-event geometry no longer
  fights the monitor-aware poller. Movement smoothing uses elapsed time.
- One 30 Hz clock drives all outputs; callback fan-out is removed. Unaffected outputs skip
  drawing, and color conversion only processes the drawn rectangle. Full-output shared-memory
  surfaces remain; this is still a software renderer.
- `voice_chat.sh` toggles a terminal-free voice session. Adaptive energy detection uses a short
  speech onset, 300ms pre-roll, and 700ms trailing silence. The microphone closes between turns;
  capture resumes after playback, avoiding self-transcription. Idle sessions stop after 20s.
  Device errors remain visible in logs. Recordings are private and removed after transcription.
- `/stop` cancels inference/playback. Concurrent conversation turns are serialized. Piper
  subprocesses are reaped on interruption; speech uses the model's sample rate. Background
  reactions do not interrupt listening or a conversation. Whisper imports and weights load
  lazily on CPU, leaving GPU memory to the conversational model.
- Ollama receives explicit output/context limits, a turn deadline, keep-alive, startup warmup,
  and `think = false`. Native tool calls replace printed JSON roleplay. After the tool budget,
  a final tool-free completion summarizes results. Invalid provider swaps retain the old provider.
- The launcher builds before starting processes and supervises failures. `doctor.py` checks
  dependencies, configured models, and optionally benchmarks a tool-free local reply.

## Run

```bash
./scripts/run.sh
./scripts/voice_chat.sh
```

Use a Hyprland binding for `scripts/voice_chat.sh`; a second invocation stops the session.
Text chat remains available. Configure language, CPU threads, silence/noise sensitivity, and PipeWire source target
under `[speech]` in `config/buddy.toml`. Both scripts prefer the existing `hypr-buddy` Conda
Python, then the repository venv, then system Python. Voice needs the `[voice]` dependencies,
PipeWire (`pw-record`), ffmpeg, Piper, aplay, and a Piper voice with its `.onnx.json` metadata.

This deliberately uses hotkey-activated half-duplex conversation. There is no wake-word
listener, acoustic echo cancellation, or spoken interruption while Shiro is talking.
Whisper's first use can take longer while loading or downloading its configured model.
Cloud providers retain their existing completion behavior; token streaming and streaming
TTS are not implemented in this pass. CPU transcription and 2D animation still need subjective
tuning with the user's microphone and desktop habits.

## Validation on 2026-09-13

- 13 Python regression tests: scaled/rotated geometry, monitor refresh, small negative-origin
  outputs, speech endpointing, playback completion, queue interruption, private audio paths,
  single-line voice framing, cross-client cancellation, provider rollback, tool finalization,
  and cancellation history cleanup.
- Four Rust tests: alpha-edge interpolation, non-square rendering, Unicode wrapping, and
  stable speech layout. Release build passes.
- A real isolated Hyprland overlay connected, configured to **1600×900 logical pixels** on
  the **2560×1440 / 1.6 scale** panel, accepted move/say commands, and exited cleanly.
- Read-only local-model checks confirmed installed `gemma4:e2b` supports completion, vision,
  tools, and thinking. The separately configured `qwen2.5vl:3b` was absent; vision now reuses
  the installed model instead of requiring a second model resident in memory.
- Initial short benchmark: 10.31s total, 7.11s loading, empty dialogue under a 40-token limit.
  With thinking disabled and a warm model: 1.24s total, 0.63s loading, 17.5 tokens/s, “Hello there.”
  These are single local measurements, not a controlled benchmark or an end-to-end voice latency
  promise; warm caches and the thinking change both affect the comparison.
- Synthetic audio round-trip: Piper generated 3.02s of speech in 0.49s; Whisper small
  transcribed it correctly in 5.36s including cold loading, using four CPU threads and
  English decoding. No microphone or speaker playback was used in this test. The initial
  sandboxed attempt stalled and was terminated; these timings use normal runtime access.
- All doctor dependency checks pass in the existing Conda environment. The actual microphone,
  end-to-end transcription/playback quality, and physical multi-monitor behavior have not been
  interactively verified. No existing user changes were committed or discarded.

Repeat checks with the project's Python dependencies installed:

```bash
python -m unittest discover -s tests -v
cargo test --manifest-path overlay/Cargo.toml
python scripts/doctor.py --benchmark
```

The implementation uses the documented [Ollama chat options](https://docs.ollama.com/api/chat),
[context/keep-alive controls](https://docs.ollama.com/faq), and
[Hyprland monitor coordinate rules](https://wiki.hypr.land/configuring/core/monitors/).

## PipeWire microphone fix

Voice capture now launches the system `pw-record` directly, avoiding PyAudio's
ALSA/JACK device enumeration and inherited Conda library paths. Set
`speech.pipewire_target` to a PipeWire source name/serial to override the default
microphone; PortAudio device indices are no longer used. Capture failures retain
the recorder's diagnostic text, and a stalled device can be cancelled.

Four capture regression tests pass (fragmented PCM reads, environment isolation,
error reporting, and stalled-device handling). A live 0.2-second default-input
check returned the expected 6,400 PCM bytes without ALSA/JACK probing errors;
the test audio was discarded.
