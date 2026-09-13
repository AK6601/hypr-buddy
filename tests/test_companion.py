import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from brain import chat as chat_module
from brain.chat import ChatHandler
from brain import main as brain_main
from brain.tts import TTSEngine
from daemon.geometry import logical_monitor, GeometryMonitor
from brain.providers.ollama import OllamaProvider
from brain.providers.base import ChatTurn
from brain.llm import LLMBackend
from brain.personality import Personality
import httpx

spec = importlib.util.spec_from_file_location("voice_chat", Path(__file__).resolve().parents[1] / "scripts/voice_chat.py")
voice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voice)


class GeometryTests(unittest.TestCase):
    def test_scaled_rotated_monitor_with_reserved_panel(self):
        self.assertEqual(logical_monitor({"id": 1, "width": 3840, "height": 2160, "scale": 2, "transform": 1, "x": -1080, "y": 20, "reserved": [0, 30, 0, 0]}), {"id": 1, "x": -1080, "y": 50, "w": 1080, "h": 1890})

    def test_monitor_reconfiguration_emits_geometry(self):
        m = GeometryMonitor(None, {})
        m._last_geo = {"address": "1", "x": 0, "y": 0, "w": 500, "h": 500, "monitor": {"w": 1920}}
        self.assertTrue(m._geo_changed({**m._last_geo, "monitor": {"w": 1280}}))

    def test_clamping_negative_origin_and_small_monitor(self):
        with patch.object(brain_main, "_pos", brain_main.BuddyPos()):
            geo = {"monitor": {"x": -1000, "y": -300, "w": 100, "h": 100}}
            self.assertEqual(brain_main._clamp_to_monitor(5000, 5000, geo), (-1000, -300))


class VoiceTests(unittest.TestCase):
    def test_noise_and_single_impulse_are_not_speech(self):
        d = voice.EndpointDetector()
        for rms in [30]*100 + [3000] + [30]*100:
            self.assertFalse(d.feed(rms))
        self.assertFalse(d.started)

    def test_speech_requires_onset_and_trailing_silence(self):
        d = voice.EndpointDetector(silence_seconds=.7)
        for _ in range(10):
            self.assertFalse(d.feed(1000))
        self.assertTrue(d.started)
        for _ in range(34):
            self.assertFalse(d.feed(20))
        self.assertTrue(d.feed(20))


class TTSTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_tracks_playback_and_interrupt_drains_queue(self):
        tts = TTSEngine({})
        entered, finish = asyncio.Event(), asyncio.Event()
        async def playback(text):
            entered.set()
            await finish.wait()
        tts._speak_now = playback
        task = asyncio.create_task(tts.speak("hello", wait=True, conversation=True))
        await entered.wait()
        self.assertFalse(task.done())
        await tts.speak("queued")
        await tts.interrupt()
        await asyncio.wait_for(task, 1)
        await asyncio.wait_for(tts._queue.join(), 1)
        await tts.close()

    async def test_proactive_speech_is_suppressed_during_listening(self):
        tts = TTSEngine({})
        tts.listening = True
        await tts.speak("background")
        self.assertTrue(tts._queue.empty())


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock = str(Path(self.tmp.name) / "chat.sock")
        self.patch = patch.object(chat_module, "CHAT_SOCKET", self.sock)
        self.patch.start()
        self.llm = AsyncMock()
        self.llm.chat.return_value = "Hello\nthere."
        self.tts = TTSEngine({"enabled": False})
        self.overlay = AsyncMock()
        self.mood = unittest.mock.Mock(label="calm")
        self.whisper = AsyncMock()
        self.whisper.transcribe.return_value = "hello"
        self.handler = ChatHandler(self.llm, self.mood, self.overlay, self.tts, AsyncMock(), self.whisper)
        self.server = asyncio.create_task(self.handler.run())
        for _ in range(100):
            if Path(self.sock).exists():
                break
            await asyncio.sleep(.01)
        self.clients = []

    async def connect(self):
        reader, writer = await asyncio.open_unix_connection(self.sock)
        self.clients.append(writer)
        return reader, writer

    async def asyncTearDown(self):
        for w in self.clients:
            w.close()
            await w.wait_closed()
        self.server.cancel()
        await asyncio.gather(self.server, return_exceptions=True)
        await self.tts.close()
        self.patch.stop()
        self.tmp.cleanup()

    async def test_invalid_audio_never_reaches_llm_or_deletes_user_file(self):
        path = Path(self.tmp.name) / "keep.wav"
        path.write_bytes(b"private")
        r, w = await self.connect()
        w.write(f"AUDIO:{path}\n".encode()); await w.drain()
        self.assertTrue((await asyncio.wait_for(r.readline(), 1)).startswith(b"ERROR:"))
        self.assertTrue(path.exists())
        self.llm.chat.assert_not_called()

    async def test_voice_ack_waits_for_playback_and_is_one_json_line(self):
        r, w = await self.connect()
        w.write(b"/listen\n"); await w.drain()
        self.assertEqual(await r.readline(), b"Listening.\n")
        path = Path(self.tmp.name) / "recordings" / "turn.wav"
        path.parent.mkdir(); path.write_bytes(b"wav")
        started, finish = asyncio.Event(), asyncio.Event()
        async def speak(*args, **kwargs):
            self.assertTrue(kwargs["wait"])
            started.set(); await finish.wait()
        self.tts.speak = speak
        w.write(f"AUDIO:{path}\n".encode()); await w.drain()
        await asyncio.wait_for(started.wait(), 1)
        response = asyncio.create_task(r.readline())
        await asyncio.sleep(.02)
        self.assertFalse(response.done())
        finish.set()
        self.assertEqual(json.loads(await response), {"text": "Hello\nthere."})
        self.assertFalse(path.exists())

    async def test_stop_cancels_inference_from_another_connection(self):
        entered = asyncio.Event()
        async def slow(**kwargs):
            entered.set(); await asyncio.sleep(30)
        self.llm.chat.side_effect = slow
        r, w = await self.connect()
        w.write(b"hello\n"); await w.drain()
        await entered.wait()
        r2, w2 = await self.connect()
        w2.write(b"/stop\n"); await w2.drain()
        self.assertEqual(await asyncio.wait_for(r2.readline(), 1), b"Stopped.\n")
        self.assertEqual(await asyncio.wait_for(r.readline(), 1), b"")
        self.assertFalse(self.tts.conversing)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_loop_gets_final_response_and_inference_limits(self):
        calls = []
        def respond(request):
            body = json.loads(request.content); calls.append(body)
            if len(calls) == 1:
                msg = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "stats", "arguments": {}}}]}
            else:
                msg = {"role": "assistant", "content": "Everything looks good."}
            return httpx.Response(200, json={"message": msg})
        transport = httpx.MockTransport(respond)
        client = httpx.AsyncClient(transport=transport)
        provider = OllamaProvider("test", max_tokens=100, num_ctx=2048)
        with patch("brain.providers.ollama.httpx.AsyncClient", return_value=client):
            reply = await provider.chat("hello", [ChatTurn("user", "stats?")], [{"function": {"name": "stats"}}], AsyncMock(return_value="42"), max_tool_iters=1)
        self.assertEqual(reply, "Everything looks good.")
        self.assertEqual(calls[0]["options"], {"num_predict": 100, "num_ctx": 2048})
        self.assertNotIn("tools", calls[1])

    async def test_failed_provider_swap_keeps_previous_provider(self):
        llm = LLMBackend({"model": "test"}, Personality({}))
        previous = llm._text
        with self.assertRaises(ValueError):
            llm.set_text_provider("invalid")
        self.assertIs(llm._text, previous)
        self.assertEqual(llm._text_provider_name, "ollama")

    async def test_cancelled_inference_does_not_leave_orphan_user_turn(self):
        llm = LLMBackend({"model": "test"}, Personality({}))
        llm._text = AsyncMock()
        llm._text.chat.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await llm.chat("hello", "calm")
        self.assertEqual(list(llm._history), [])
