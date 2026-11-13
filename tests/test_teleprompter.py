import importlib.util
from pathlib import Path
import queue
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("teleprompter", Path(__file__).resolve().parents[1] / "app" / "uvp_teleprompter.py")
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)


class TeleprompterTests(unittest.TestCase):
    def test_reading_estimate_uses_actual_word_count(self):
        self.assertIn("150 words", app.script_summary("word " * 150))
        self.assertIn("00:01:00", app.script_summary("word " * 150))

    def test_utf8_bom_script_loads_without_bom(self):
        instance = app.TeleprompterApp.__new__(app.TeleprompterApp)
        instance.editor = Mock()
        instance.state = app.TeleprompterState()
        instance.script_summary_var = Mock()
        instance._refresh_views = Mock()
        instance._set_status = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "script.txt"
            path.write_text("Hello world", encoding="utf-8-sig")
            with patch.object(app.filedialog, "askopenfilename", return_value=str(path)):
                instance.open_script()
            self.assertEqual(instance.state.script_text, "Hello world")

    def test_long_script_continues_above_top_edge(self):
        view = app.PrompterView.__new__(app.PrompterView)
        view.canvas = Mock()
        view.canvas.winfo_width.return_value = 900
        view.canvas.winfo_height.return_value = 600
        view.app = SimpleNamespace(state=SimpleNamespace(scroll_px=1800))
        view._text_item, view._guide_line = 1, None
        view._sync_scrollbar = Mock()
        view._update_position()
        self.assertEqual(view.canvas.coords.call_args.args, (1, 450, -1180))

    def test_mp3_requires_an_executable_not_a_converter_name(self):
        recorder = app.AudioRecorder(Path("unused"), app.AudioSupport(True, "", False))
        with patch.object(app, "AudioSegment", SimpleNamespace(converter="ffmpeg")), patch.object(app.shutil, "which", return_value=None):
            self.assertFalse(recorder._probe_mp3_export())

    def test_invalid_timer_values_are_rejected(self):
        instance = app.TeleprompterApp.__new__(app.TeleprompterApp)
        for value in ("nan", "inf", "-1", "bad"):
            instance.timer_target_minutes_var = SimpleNamespace(get=lambda: value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                instance._parse_target_minutes()

    def test_unreadable_script_preserves_current_state(self):
        instance = app.TeleprompterApp.__new__(app.TeleprompterApp)
        instance.state = app.TeleprompterState(script_text="keep me")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid.txt"
            path.write_bytes(b"\xff\xfe\xff")
            with patch.object(app.filedialog, "askopenfilename", return_value=str(path)), patch.object(app.messagebox, "showerror") as error:
                instance.open_script()
                error.assert_called_once()
                self.assertEqual(instance.state.script_text, "keep me")

    def test_writer_error_never_reports_success(self):
        recorder = app.AudioRecorder(Path("unused"), app.AudioSupport(True, "", False))
        recorder._active = True
        recorder._queue = queue.Queue()
        recorder._queue.put(object())
        recorder._file = Mock()
        recorder._file.write.side_effect = OSError("disk full")
        recorder._temp_wav_path = Path("partial.wav")
        recorder._writer_loop()
        success, message = recorder.stop()
        self.assertFalse(success)
        self.assertIn("disk full", message)
        self.assertEqual(recorder.status, "Error")

    def test_writer_failure_stops_capture_on_ui_poll(self):
        recorder = app.AudioRecorder(Path("unused"), app.AudioSupport(True, "", False))
        recorder._active = True
        recorder._queue = queue.Queue()
        recorder._queue.put(object())
        recorder._file = Mock()
        recorder._file.write.side_effect = OSError("disk full")
        recorder._stream = Mock()
        stream, file = recorder._stream, recorder._file
        recorder._writer_loop()
        self.assertFalse(recorder._active)
        self.assertEqual(recorder.status, "Error")
        recorder.poll_failure()
        stream.stop.assert_called_once()
        stream.close.assert_called_once()
        file.close.assert_called_once()
        self.assertIsNone(recorder._stream)
        self.assertIn("disk full", recorder.last_message)

    def test_writer_timeout_keeps_file_open_for_retry(self):
        recorder = app.AudioRecorder(Path("unused"), app.AudioSupport(True, "", False))
        recorder._active = True
        recorder._file = Mock()
        recorder._writer_thread = Mock()
        recorder._writer_thread.is_alive.return_value = True
        success, _ = recorder.stop()
        self.assertFalse(success)
        self.assertTrue(recorder.active)
        recorder._file.close.assert_not_called()

    def test_recording_directory_errors_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "not-a-directory"
            file.write_text("occupied")
            recorder = app.AudioRecorder(file, app.AudioSupport(True, "", False))
            with patch.object(app, "sd", Mock()), patch.object(app, "sf", Mock()):
                success, message = recorder.start("WAV")
            self.assertFalse(success)
            self.assertIn("not writable", message)


if __name__ == "__main__":
    unittest.main()
