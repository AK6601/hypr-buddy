import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

from brain.audio_capture import capture_blocks


class CaptureTests(unittest.TestCase):
    def test_partial_reads_form_pcm_blocks_and_isolate_conda_libraries(self):
        code = "import os,sys,time; assert 'LD_LIBRARY_PATH' not in os.environ; sys.stdout.buffer.write(b'a'*300); sys.stdout.buffer.flush(); time.sleep(.05); sys.stdout.buffer.write(b'b'*340); sys.stdout.buffer.flush(); time.sleep(10)"
        with patch.dict(os.environ, {"LD_LIBRARY_PATH": "/fake/conda/lib"}):
            stream = capture_blocks(threading.Event(), command=[sys.executable, "-c", code])
            try:
                self.assertEqual(next(stream), b'a'*300 + b'b'*340)
            finally:
                stream.close()

    def test_real_capture_error_is_reported(self):
        stream = capture_blocks(threading.Event(), command=[sys.executable, "-c", "import sys; sys.stderr.write('No input source'); sys.exit(1)"])
        with self.assertRaisesRegex(RuntimeError, "No input source"):
            next(stream)

    def test_stalled_device_can_be_cancelled(self):
        stop = threading.Event()
        timer = threading.Timer(.15, stop.set)
        timer.start()
        started = time.monotonic()
        try:
            self.assertEqual(list(capture_blocks(stop, command=[sys.executable, "-c", "import time; time.sleep(10)"])), [])
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 2)

    def test_stalled_device_reports_timeout(self):
        with self.assertRaisesRegex(RuntimeError, "produced no audio"):
            list(capture_blocks(threading.Event(), command=[sys.executable, "-c", "import time; time.sleep(10)"], stall_timeout=.1))
