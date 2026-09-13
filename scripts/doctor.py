#!/usr/bin/env python3
"""Read-only checks for the desktop, voice stack, and configured inference models."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import time
import tomllib
import urllib.request

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", action="store_true", help="Time one short local-model reply (no tools or screen capture)")
    args = parser.parse_args()
    with (ROOT / "config/buddy.toml").open("rb") as f:
        cfg = tomllib.load(f)
    failures = []
    def check(name, ok, detail):
        print(f"{'OK  ' if ok else 'FAIL'} {name}: {detail}")
        if not ok:
            failures.append(name)
    check("Wayland", bool(os.environ.get("WAYLAND_DISPLAY")), os.environ.get("WAYLAND_DISPLAY", "start in your Hyprland session"))
    for executable in ("hyprctl", "piper", "aplay", "ffmpeg", "pw-record"):
        found = shutil.which(executable) or (shutil.which("piper-tts") if executable == "piper" else None)
        check(executable, bool(found), found or "not installed / not on PATH")
    for module in ("httpx", "aiosqlite", "whisper", "torch", "numpy"):
        check(module, importlib.util.find_spec(module) is not None, f"Python: {sys.executable}")
    model = Path(cfg.get("tts", {}).get("piper_model", "")).expanduser()
    check("Piper voice", model.is_file() and Path(str(model) + ".json").is_file(), str(model))
    llm = cfg.get("llm", {})
    url = llm.get("ollama_url", "http://localhost:11434").rstrip("/")
    if llm.get("provider", "ollama") == "ollama":
        try:
            with urllib.request.urlopen(url + "/api/tags", timeout=5) as r:
                names = {m["name"] for m in json.load(r).get("models", [])}
            for purpose, table in (("text", llm), ("vision", cfg.get("vision", {}))):
                if table.get("provider", "ollama") != "ollama" or table.get("enabled", True) is False:
                    continue
                wanted = table.get("model", "")
                present = wanted in names or wanted + ":latest" in names
                check(purpose + " model", present, wanted if present else f"missing {wanted}; installed: {', '.join(sorted(names)) or 'none'}. Run ollama pull {wanted}")
            if args.benchmark:
                start = time.monotonic()
                body = {"model": llm["model"], "messages": [{"role": "user", "content": "Say hello in one short sentence."}], "stream": False, "think": llm.get("think", False), "keep_alive": llm.get("keep_alive", "5m"), "options": {"num_predict": 40, "num_ctx": llm.get("num_ctx", 4096)}}
                req = urllib.request.Request(url + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    result = json.load(r)
                elapsed = time.monotonic() - start
                tokens = result.get("eval_count", 0)
                duration = result.get("eval_duration", 0) / 1e9
                print(f"BENCH {elapsed:.2f}s total, {tokens / duration if duration else 0:.1f} tokens/s, load {result.get('load_duration', 0)/1e9:.2f}s")
                content = result.get("message", {}).get("content", "").strip()
                check("benchmark reply", bool(content), content or "empty response; inspect thinking/token limits")
        except Exception as e:
            check("Ollama", False, str(e))
    else:
        provider = llm.get("provider")
        key = Path(llm.get(provider + "_api_key_file", "")).expanduser()
        check(provider + " key file", key.is_file(), str(key))
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())
