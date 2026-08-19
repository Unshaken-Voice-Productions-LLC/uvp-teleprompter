from __future__ import annotations

import os
import queue
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, font as tkfont

try:
    import sounddevice as sd
except Exception as exc:  # pragma: no cover - environment dependent
    sd = None
    SOUNDDEVICE_IMPORT_ERROR = exc
else:
    SOUNDDEVICE_IMPORT_ERROR = None

try:
    import soundfile as sf
except Exception as exc:  # pragma: no cover - environment dependent
    sf = None
    SOUNDFILE_IMPORT_ERROR = exc
else:
    SOUNDFILE_IMPORT_ERROR = None

try:
    from pydub import AudioSegment
except Exception as exc:  # pragma: no cover - environment dependent
    AudioSegment = None
    PYDUB_IMPORT_ERROR = exc
else:
    PYDUB_IMPORT_ERROR = None


APP_NAME = "UVP Teleprompter"
DEFAULT_TEXT = (
    "Welcome to UVP Teleprompter.\n\n"
    "Paste your script on the Edit tab, then press Space to start or pause.\n"
    "Use the speed and font controls while you rehearse or record.\n"
)
DEFAULT_TIMER_MINUTES = "5"
PRESET_SPEEDS = {
    "Very Slow": 18,
    "Slow": 40,
    "Medium": 75,
    "Fast": 120,
}


def format_hms(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


@dataclass
class TeleprompterState:
    script_text: str = DEFAULT_TEXT
    running: bool = False
    scroll_px: float = 0.0
    speed_px_per_sec: float = 75.0
    font_size: int = 44
    mirror: bool = False
    always_on_top: bool = False
    fullscreen: bool = False


@dataclass
class AudioSupport:
    available: bool
    message: str
    can_mp3: bool
    input_device_name: str = ""


class AudioRecorder:
    def __init__(self, output_dir: Path, support: AudioSupport):
        self.output_dir = output_dir
        self.support = support
        self.sample_rate = 44100
        self.channels = 1
        self._queue: queue.Queue | None = None
        self._queue_maxsize = 256
        self._stream = None
        self._writer_thread: threading.Thread | None = None
        self._file: sf.SoundFile | None = None
        self._stop_event = threading.Event()
        self._active = False
        self._paused = False
        self._segment_started_at = 0.0
        self._accumulated_seconds = 0.0
        self._status = "Idle"
        self._last_message = ""
        self._last_saved_path: Path | None = None
        self._format = "WAV"
        self._base_path: Path | None = None
        self._temp_wav_path: Path | None = None
        self._final_path: Path | None = None
        self._dropped_blocks = 0

    @property
    def active(self) -> bool:
        return self._active

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def status(self) -> str:
        return self._status

    @property
    def last_message(self) -> str:
        return self._last_message

    @property
    def last_saved_path(self) -> Path | None:
        return self._last_saved_path

    @property
    def output_folder(self) -> Path:
        return self.output_dir

    @property
    def output_format(self) -> str:
        return self._format

    def elapsed_seconds(self, now: float | None = None) -> float:
        if not self._active:
            return self._accumulated_seconds
        if self._paused:
            return self._accumulated_seconds
        if now is None:
            now = time.perf_counter()
        return self._accumulated_seconds + (now - self._segment_started_at)

    def _resolve_sample_rate(self) -> int:
        if sd is None:
            return 44100
        try:
            device = sd.query_devices(kind="input")
            rate = int(device.get("default_samplerate") or 44100)
            return rate if rate > 0 else 44100
        except Exception:
            return 44100

    def _recording_callback(self, indata, frames, time_info, status):  # pragma: no cover - callback
        if status:
            self._last_message = f"Audio status: {status}"
        if not self._active or self._paused or self._stop_event.is_set():
            return
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(indata.copy())
        except queue.Full:
            self._dropped_blocks += 1
            self._last_message = "Audio buffer overflow; some blocks were dropped."

    def _writer_loop(self):  # pragma: no cover - thread behavior
        while not self._stop_event.is_set() or (self._queue is not None and not self._queue.empty()):
            if self._queue is None:
                time.sleep(0.05)
                continue
            try:
                block = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if block is None:
                continue
            if self._file is not None:
                self._file.write(block)

    def _probe_mp3_export(self) -> bool:
        if AudioSegment is None:
            return False
        return bool(shutil.which("ffmpeg") or shutil.which("ffmpeg.exe") or getattr(AudioSegment, "converter", None))

    def start(self, output_format: str) -> tuple[bool, str]:
        if not self.support.available:
            return False, self.support.message
        if self._active:
            return False, "Recording is already active."
        if sf is None or sd is None:
            return False, "Recording libraries are not installed."

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._format = output_format.upper().strip() if output_format else "WAV"
        if self._format not in {"WAV", "MP3"}:
            self._format = "WAV"

        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        base_name = f"UVP_recording_{timestamp}"
        self._base_path = self.output_dir / base_name
        self._temp_wav_path = self._base_path.with_suffix(".wav")
        self._final_path = self._temp_wav_path if self._format == "WAV" else self._base_path.with_suffix(".mp3")
        self._queue = queue.Queue(maxsize=self._queue_maxsize)
        self._stop_event.clear()
        self._accumulated_seconds = 0.0
        self._segment_started_at = time.perf_counter()
        self._paused = False
        self._dropped_blocks = 0
        self._status = "Recording"
        self._last_message = ""

        try:
            self.sample_rate = self._resolve_sample_rate()
            self._file = sf.SoundFile(
                str(self._temp_wav_path),
                mode="w",
                samplerate=self.sample_rate,
                channels=self.channels,
                subtype="PCM_16",
            )
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                callback=self._recording_callback,
            )
            self._stream.start()
            self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
            self._writer_thread.start()
            self._active = True
            return True, f"Recording started: {self._format}"
        except Exception as exc:
            self._cleanup_partial()
            self._status = "Error"
            return False, f"Recording could not start: {exc}"

    def _cleanup_partial(self):
        self._active = False
        self._paused = False
        self._stop_event.set()
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None
        try:
            if self._file is not None:
                self._file.close()
        except Exception:
            pass
        self._file = None
        self._queue = None
        self._writer_thread = None

    def pause(self) -> tuple[bool, str]:
        if not self._active:
            return False, "No active recording to pause."
        if self._paused:
            return False, "Recording is already paused."
        self._accumulated_seconds += time.perf_counter() - self._segment_started_at
        self._paused = True
        self._status = "Paused"
        self._last_message = "Recording paused."
        return True, self._last_message

    def resume(self) -> tuple[bool, str]:
        if not self._active:
            return False, "No active recording to resume."
        if not self._paused:
            return False, "Recording is already running."
        self._segment_started_at = time.perf_counter()
        self._paused = False
        self._status = "Recording"
        self._last_message = "Recording resumed."
        return True, self._last_message

    def toggle_pause(self) -> tuple[bool, str]:
        return self.resume() if self._paused else self.pause()

    def stop(self) -> tuple[bool, str]:
        if not self._active:
            return False, "No recording is active."

        if not self._paused:
            self._accumulated_seconds += time.perf_counter() - self._segment_started_at

        self._stop_event.set()
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None

        try:
            if self._writer_thread is not None:
                self._writer_thread.join(timeout=5)
        except Exception:
            pass
        self._writer_thread = None

        try:
            if self._file is not None:
                self._file.close()
        except Exception:
            pass
        self._file = None

        self._active = False
        self._paused = False

        if self._temp_wav_path is None:
            self._status = "Error"
            return False, "Recording stopped, but no audio file was created."

        final_message = ""
        saved_path = self._temp_wav_path

        if self._format == "MP3":
            can_convert = self._probe_mp3_export()
            if can_convert:
                try:
                    audio = AudioSegment.from_wav(str(self._temp_wav_path))
                    audio.export(str(self._final_path), format="mp3")
                    try:
                        self._temp_wav_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    saved_path = self._final_path if self._final_path is not None else self._temp_wav_path
                    final_message = f"MP3 recording saved: {saved_path}"
                except Exception as exc:
                    saved_path = self._temp_wav_path
                    final_message = f"MP3 export failed. WAV recording was saved successfully. ({exc})"
            else:
                saved_path = self._temp_wav_path
                final_message = "MP3 export requires ffmpeg. WAV recording was saved successfully."
        else:
            final_message = f"WAV recording saved: {saved_path}"

        self._last_saved_path = saved_path
        self._status = "Saved"
        self._last_message = final_message
        return True, final_message


class PrompterView:
    def __init__(self, app: "TeleprompterApp", parent: tk.Widget, title: str, is_popup: bool = False):
        self.app = app
        self.parent = parent
        self.title = title
        self.is_popup = is_popup
        self.container = ttk.Frame(parent)
        self.container.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(self.container, bg="#050505", highlightthickness=0, bd=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar = ttk.Scrollbar(self.container, orient="vertical", command=self._on_scrollbar)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<Enter>", lambda _e: self.canvas.focus_set())
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", self._on_mousewheel)
        self.canvas.bind("<Button-5>", self._on_mousewheel)
        self._text_item = None
        self._guide_line = None
        self._rendered_key: tuple | None = None
        self._text_bbox_height = 0
        self._wrap_width = 0
        self._font: tkfont.Font | None = None
        self._scroll_update_guard = False

    def _on_resize(self, _event=None):
        self.redraw(force=True)

    def _display_text(self) -> str:
        text = self.app.state.script_text
        if self.app.state.mirror:
            lines = text.splitlines()
            return "\n".join(line[::-1] for line in lines)
        return text

    def _current_font(self) -> tkfont.Font:
        size = max(8, int(self.app.state.font_size))
        if self._font is None or int(self._font.cget("size")) != size:
            family = "Segoe UI"
            try:
                family = tkfont.nametofont("TkDefaultFont").cget("family")
            except Exception:
                pass
            self._font = tkfont.Font(family=family, size=size, weight="normal")
        return self._font

    def redraw(self, force: bool = False):
        width = max(320, self.canvas.winfo_width())
        height = max(240, self.canvas.winfo_height())
        key = (
            self.app.state.script_text,
            self.app.state.font_size,
            self.app.state.mirror,
            width,
            height,
        )
        if not force and key == self._rendered_key:
            self._update_position()
            return

        self.canvas.delete("all")
        self._text_item = None
        self._guide_line = None
        self._wrap_width = max(200, width - 80)
        font = self._current_font()
        text = self._display_text().strip("\n") or " "
        self._text_item = self.canvas.create_text(
            width // 2,
            height + 20,
            text=text,
            fill="#F7F7F7",
            font=font,
            width=self._wrap_width,
            anchor="n",
            justify="center",
        )
        self._guide_line = self.canvas.create_line(0, height // 2, width, height // 2, fill="#2a2a2a", width=1)
        self.canvas.tag_lower(self._guide_line)
        self._rendered_key = key
        self.canvas.update_idletasks()
        bbox = self.canvas.bbox(self._text_item)
        self._text_bbox_height = 0 if bbox is None else max(0, bbox[3] - bbox[1])
        self.canvas.configure(scrollregion=(0, 0, width, max(height, self._text_bbox_height + 40)))
        self._update_position()

    def _update_position(self):
        if not self._text_item:
            return
        width = max(320, self.canvas.winfo_width())
        height = max(240, self.canvas.winfo_height())
        y = max(20, height + 20 - self.app.state.scroll_px)
        self.canvas.coords(self._text_item, width // 2, y)
        if self._guide_line is not None:
            self.canvas.coords(self._guide_line, 0, height // 2, width, height // 2)
        self._sync_scrollbar()

    def tick(self):
        self._update_position()

    def scroll_limit(self) -> float:
        height = max(240, self.canvas.winfo_height())
        return float(self._text_bbox_height + height + 80)

    def viewport_height(self) -> int:
        return max(240, self.canvas.winfo_height())

    def _sync_scrollbar(self):
        if self._scroll_update_guard:
            return
        limit = max(1.0, self.scroll_limit())
        height = float(self.viewport_height())
        top = min(max(self.app.state.scroll_px / limit, 0.0), 1.0)
        visible = min(height / limit, 1.0)
        bottom = min(1.0, top + visible)
        self.scrollbar.set(top, bottom)

    def _on_scrollbar(self, *args):
        if not args:
            return
        cmd = args[0]
        if cmd == "moveto" and len(args) >= 2:
            try:
                fraction = float(args[1])
            except ValueError:
                return
            self.app.set_scroll_fraction(fraction)
        elif cmd == "scroll" and len(args) >= 3:
            try:
                units = int(args[1])
            except ValueError:
                return
            kind = args[2]
            if kind == "units":
                self.app.adjust_scroll(units * 45)
            elif kind == "pages":
                self.app.adjust_scroll(units * self.viewport_height() * 0.85)

    def _on_mousewheel(self, event):
        delta = getattr(event, "delta", 0) or 0
        if delta == 0 and getattr(event, "num", None) == 4:
            delta = 120
        elif delta == 0 and getattr(event, "num", None) == 5:
            delta = -120
        if delta == 0:
            return "break"
        self.app.adjust_scroll(-delta / 120 * 55)
        return "break"


class TeleprompterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.configure(bg="#121212")
        self.root.minsize(1200, 780)

        self.project_root = Path(__file__).resolve().parent.parent
        self.scripts_dir = self.project_root / "scripts"
        self.recordings_dir = self.project_root / "recordings"
        self.recordings_dir.mkdir(parents=True, exist_ok=True)

        self.state = TeleprompterState()
        self.current_file: Path | None = None
        self.popup: tk.Toplevel | None = None
        self.popup_view: PrompterView | None = None
        self.views: list[PrompterView] = []
        self._last_tick = time.perf_counter()
        self._editor_update_job = None

        self.audio_support = self._detect_audio_support()
        self.recorder = AudioRecorder(self.recordings_dir, self.audio_support)

        self.timer_running = False
        self.timer_paused = False
        self.timer_countdown_var = tk.BooleanVar(value=False)
        self.timer_target_minutes_var = tk.StringVar(value=DEFAULT_TIMER_MINUTES)
        self.timer_accumulated_seconds = 0.0
        self.timer_segment_started_at = 0.0
        self.timer_alerted = False

        self.speed_preset_var = tk.StringVar(value="Medium")
        self.speed_var = tk.DoubleVar(value=self.state.speed_px_per_sec)
        self.font_var = tk.IntVar(value=self.state.font_size)
        self.mirror_var = tk.BooleanVar(value=self.state.mirror)
        self.topmost_var = tk.BooleanVar(value=self.state.always_on_top)
        self.fullscreen_var = tk.BooleanVar(value=self.state.fullscreen)
        self.recording_format_var = tk.StringVar(value="WAV")

        self.timer_display_var = tk.StringVar(value="00:00:00")
        self.timer_mode_var = tk.StringVar(value="Elapsed")
        self.timer_status_var = tk.StringVar(value="Timer idle")

        self.recording_status_var = tk.StringVar(value="Idle")
        self.recording_elapsed_var = tk.StringVar(value="00:00:00")
        self.recording_folder_var = tk.StringVar(value=str(self.recordings_dir))
        self.recording_last_saved_var = tk.StringVar(value="None")
        self.recording_hint_var = tk.StringVar(value=self.audio_support.message)
        self.speed_display_var = tk.StringVar(value=f"Current speed: {int(self.state.speed_px_per_sec)} px/s")

        self._setup_style()
        self._build_ui()
        self._bind_shortcuts()
        self._load_state_into_editor()
        self._sync_from_editor()
        self._apply_window_flags()
        self._refresh_timer_display(time.perf_counter())
        self._refresh_recording_display(time.perf_counter())
        self._animate()

    def _detect_audio_support(self) -> AudioSupport:
        if sd is None or sf is None:
            pieces = []
            if SOUNDDEVICE_IMPORT_ERROR is not None:
                pieces.append("sounddevice")
            if SOUNDFILE_IMPORT_ERROR is not None:
                pieces.append("soundfile")
            return AudioSupport(
                available=False,
                message=(
                    "Recording unavailable. Install audio dependencies: "
                    "pip install sounddevice soundfile pydub"
                ),
                can_mp3=False,
            )

        try:
            input_device = sd.query_devices(kind="input")
            device_name = str(input_device.get("name", "Default input device"))
        except Exception as exc:
            return AudioSupport(
                available=False,
                message=f"Recording unavailable. No usable microphone/input device was detected. ({exc})",
                can_mp3=self._mp3_export_available(),
            )

        can_mp3 = self._mp3_export_available()
        message = (
            "Recording ready."
            if can_mp3
            else "Recording ready. MP3 export requires ffmpeg; WAV recording will always work."
        )
        return AudioSupport(available=True, message=message, can_mp3=can_mp3, input_device_name=device_name)

    def _mp3_export_available(self) -> bool:
        if AudioSegment is None:
            return False
        return bool(shutil.which("ffmpeg") or shutil.which("ffmpeg.exe") or getattr(AudioSegment, "converter", None))

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("TFrame", background="#121212")
        style.configure("TLabel", background="#121212", foreground="#F2F2F2")
        style.configure("TButton", padding=6)
        style.configure("TCheckbutton", background="#121212", foreground="#F2F2F2")
        style.configure("TLabelframe", background="#121212", foreground="#F2F2F2")
        style.configure("TLabelframe.Label", background="#121212", foreground="#F2F2F2")
        style.configure("TNotebook", background="#121212", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(12, 8))

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x", pady=(0, 8))

        ttk.Button(toolbar, text="Open .txt", command=self.open_script).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="Save", command=self.save_script).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="Save As", command=self.save_script_as).pack(side="left", padx=(0, 12))
        ttk.Button(toolbar, text="Pop Out Window", command=self.toggle_popup_window).pack(side="left", padx=(0, 12))
        ttk.Button(toolbar, text="Start / Pause", command=self.toggle_running).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="Reset", command=self.reset_scrolling).pack(side="left", padx=(0, 6))
        ttk.Label(toolbar, text="UVP Teleprompter v2.1").pack(side="right")

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)

        self.dashboard_tab = ttk.Frame(self.notebook)
        self.edit_tab = ttk.Frame(self.notebook)
        self.prompter_tab = ttk.Frame(self.notebook)
        self.help_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.dashboard_tab, text="Dashboard")
        self.notebook.add(self.edit_tab, text="Edit Script")
        self.notebook.add(self.prompter_tab, text="Teleprompter")
        self.notebook.add(self.help_tab, text="Help / About")

        self._build_dashboard_tab()
        self._build_edit_tab()
        self.main_view = PrompterView(self, self.prompter_tab, "Main View")
        self.views.append(self.main_view)
        self._build_help_tab()

        self.notebook.select(self.dashboard_tab)

        self.status_var = tk.StringVar(value="Ready")
        status = ttk.Label(outer, textvariable=self.status_var, anchor="w")
        status.pack(fill="x", pady=(8, 0))

    def _build_dashboard_tab(self):
        shell = ttk.Frame(self.dashboard_tab, padding=10)
        shell.pack(fill="both", expand=True)

        canvas = tk.Canvas(shell, bg="#121212", highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        inner = ttk.Frame(canvas)
        self.dashboard_window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _sync_scrollregion(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(self.dashboard_window, width=canvas.winfo_width())

        inner.bind("<Configure>", _sync_scrollregion)
        canvas.bind("<Configure>", _sync_scrollregion)
        canvas.bind("<MouseWheel>", lambda _e: canvas.yview_scroll(int(-(_e.delta / 120)), "units"))

        self.controls_frame = inner

        top_row = ttk.Frame(self.controls_frame)
        top_row.pack(fill="x", pady=(0, 8))

        self.script_frame = ttk.LabelFrame(top_row, text="Script Controls", padding=10)
        self.script_frame.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.teleprompter_frame = ttk.LabelFrame(top_row, text="Teleprompter Controls", padding=10)
        self.teleprompter_frame.pack(side="left", fill="both", expand=True)
        self._build_script_controls()

        middle_row = ttk.Frame(self.controls_frame)
        middle_row.pack(fill="x", pady=(0, 8))

        self.timer_frame = ttk.LabelFrame(middle_row, text="Session Timer", padding=10)
        self.timer_frame.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.recording_frame = ttk.LabelFrame(middle_row, text="Audio Recording", padding=10)
        self.recording_frame.pack(side="left", fill="both", expand=True)

        self.speed_frame = ttk.LabelFrame(self.controls_frame, text="Scroll & Display Controls", padding=10)
        self.speed_frame.pack(fill="x", pady=(0, 8))

        self._build_timer_controls()
        self._build_recording_controls()
        self._build_teleprompter_controls()
        self._build_speed_controls()

        if self.recording_hint_var.get():
            self.recording_hint_var.set(self.recording_hint_var.get())

    def _build_script_controls(self):
        row = self.script_frame
        ttk.Button(row, text="Open .txt", command=self.open_script).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Save", command=self.save_script).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Save As", command=self.save_script_as).pack(side="left", padx=(0, 12))
        ttk.Button(row, text="Edit Mode", command=lambda: self.notebook.select(self.edit_tab)).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Teleprompter Mode", command=lambda: self.notebook.select(self.prompter_tab)).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Pop Out Window", command=self.toggle_popup_window).pack(side="left", padx=(0, 6))
        ttk.Label(row, text="UVP Teleprompter v2.1").pack(side="right")

    def _build_teleprompter_controls(self):
        row = self.teleprompter_frame
        ttk.Button(row, text="Start / Pause", command=self.toggle_running).grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=(0, 8))
        ttk.Button(row, text="Reset", command=self.reset_scrolling).grid(row=0, column=1, sticky="ew", padx=(0, 6), pady=(0, 8))
        ttk.Button(row, text="Pop Out Window", command=self.toggle_popup_window).grid(row=0, column=2, sticky="ew", pady=(0, 8))
        ttk.Label(row, text="Teleprompter mode").grid(row=1, column=0, sticky="w", pady=(0, 6))
        ttk.Button(row, text="Open Teleprompter", command=lambda: self.notebook.select(self.prompter_tab)).grid(row=1, column=1, sticky="ew", pady=(0, 6))
        ttk.Button(row, text="Open Editor", command=lambda: self.notebook.select(self.edit_tab)).grid(row=1, column=2, sticky="ew", pady=(0, 6))
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)
        row.columnconfigure(2, weight=1)

    def _build_timer_controls(self):
        self.timer_display_label = tk.Label(
            self.timer_frame,
            textvariable=self.timer_display_var,
            bg="#050505",
            fg="#F7F7F7",
            font=("Consolas", 24, "bold"),
            padx=10,
            pady=8,
        )
        self.timer_display_label.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 8))

        self.timer_mode_check = ttk.Checkbutton(
            self.timer_frame,
            text="Countdown mode",
            variable=self.timer_countdown_var,
            command=self._update_timer_mode_label,
        )
        self.timer_mode_check.grid(row=1, column=0, sticky="w", padx=(0, 8))

        ttk.Label(self.timer_frame, text="Target minutes").grid(row=1, column=1, sticky="e")
        self.timer_target_entry = ttk.Entry(self.timer_frame, textvariable=self.timer_target_minutes_var, width=8)
        self.timer_target_entry.grid(row=1, column=2, sticky="w", padx=(6, 8))

        self.timer_mode_label = ttk.Label(self.timer_frame, textvariable=self.timer_mode_var)
        self.timer_mode_label.grid(row=1, column=3, sticky="w")

        self.timer_start_btn = ttk.Button(self.timer_frame, text="Start Timer", command=self.start_timer)
        self.timer_start_btn.grid(row=2, column=0, sticky="ew", pady=(8, 0), padx=(0, 6))
        self.timer_pause_btn = ttk.Button(self.timer_frame, text="Pause Timer", command=self.pause_timer)
        self.timer_pause_btn.grid(row=2, column=1, sticky="ew", pady=(8, 0), padx=(0, 6))
        self.timer_reset_btn = ttk.Button(self.timer_frame, text="Reset Timer", command=self.reset_timer)
        self.timer_reset_btn.grid(row=2, column=2, sticky="ew", pady=(8, 0), padx=(0, 6))

        self.timer_status_label = ttk.Label(self.timer_frame, textvariable=self.timer_status_var)
        self.timer_status_label.grid(row=2, column=3, sticky="w", pady=(8, 0))

        self.timer_frame.columnconfigure(0, weight=1)
        self.timer_frame.columnconfigure(1, weight=1)
        self.timer_frame.columnconfigure(2, weight=1)
        self.timer_frame.columnconfigure(3, weight=1)

    def _build_recording_controls(self):
        self.recording_status_row = ttk.Label(self.recording_frame, textvariable=self.recording_status_var)
        self.recording_status_row.grid(row=0, column=0, columnspan=4, sticky="w")

        ttk.Label(self.recording_frame, text="Elapsed recording time").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.recording_elapsed_label = tk.Label(
            self.recording_frame,
            textvariable=self.recording_elapsed_var,
            bg="#050505",
            fg="#F7F7F7",
            font=("Consolas", 18, "bold"),
            padx=10,
            pady=4,
        )
        self.recording_elapsed_label.grid(row=1, column=1, columnspan=3, sticky="ew", pady=(6, 0), padx=(8, 0))

        ttk.Label(self.recording_frame, text="Output format").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.format_menu = ttk.OptionMenu(self.recording_frame, self.recording_format_var, self.recording_format_var.get(), "WAV", "MP3")
        self.format_menu.grid(row=2, column=1, sticky="w", pady=(8, 0), padx=(8, 0))

        self.record_btn = ttk.Button(self.recording_frame, text="Record", command=self.start_recording)
        self.record_btn.grid(row=3, column=0, sticky="ew", pady=(10, 0), padx=(0, 6))
        self.pause_record_btn = ttk.Button(self.recording_frame, text="Pause Recording", command=self.pause_or_resume_recording)
        self.pause_record_btn.grid(row=3, column=1, sticky="ew", pady=(10, 0), padx=(0, 6))
        self.stop_record_btn = ttk.Button(self.recording_frame, text="Stop Recording", command=self.stop_recording)
        self.stop_record_btn.grid(row=3, column=2, sticky="ew", pady=(10, 0), padx=(0, 6))
        self.open_recordings_btn = ttk.Button(self.recording_frame, text="Open Recordings Folder", command=self.open_recordings_folder)
        self.open_recordings_btn.grid(row=3, column=3, sticky="ew", pady=(10, 0))

        ttk.Label(self.recording_frame, text="Output folder").grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.recording_folder_label = ttk.Label(self.recording_frame, textvariable=self.recording_folder_var)
        self.recording_folder_label.grid(row=4, column=1, columnspan=3, sticky="w", pady=(8, 0))

        ttk.Label(self.recording_frame, text="Last saved file").grid(row=5, column=0, sticky="w", pady=(6, 0))
        self.recording_last_saved_label = ttk.Label(self.recording_frame, textvariable=self.recording_last_saved_var)
        self.recording_last_saved_label.grid(row=5, column=1, columnspan=3, sticky="w", pady=(6, 0))

        self.recording_hint_label = ttk.Label(self.recording_frame, textvariable=self.recording_hint_var, wraplength=540)
        self.recording_hint_label.grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 0))

        self.recording_frame.columnconfigure(1, weight=1)
        self.recording_frame.columnconfigure(2, weight=1)
        self.recording_frame.columnconfigure(3, weight=1)

        if not self.audio_support.available:
            for widget in (self.record_btn, self.pause_record_btn, self.stop_record_btn, self.format_menu):
                widget.state(["disabled"])

    def _build_speed_controls(self):
        preset_row = ttk.Frame(self.speed_frame)
        preset_row.pack(fill="x", pady=(0, 8))
        ttk.Label(preset_row, text="Speed presets").pack(side="left", padx=(0, 8))
        for name, value in PRESET_SPEEDS.items():
            ttk.Button(
                preset_row,
                text=name,
                command=lambda v=value, n=name: self.set_speed_preset(n, v),
            ).pack(side="left", padx=(0, 6))
        ttk.Label(preset_row, textvariable=self.speed_display_var).pack(side="right")

        row = ttk.Frame(self.speed_frame)
        row.pack(fill="x")
        ttk.Label(row, text="Auto-scroll speed").grid(row=0, column=0, sticky="w")
        self.speed_slider = ttk.Scale(
            row,
            from_=5,
            to=220,
            variable=self.speed_var,
            command=lambda _v: self._on_speed_change(),
        )
        self.speed_slider.grid(row=0, column=1, sticky="ew", padx=(8, 18))

        ttk.Label(row, text="Font size").grid(row=0, column=2, sticky="w")
        self.font_slider = ttk.Scale(
            row,
            from_=18,
            to=84,
            variable=self.font_var,
            command=lambda _v: self._on_font_change(),
        )
        self.font_slider.grid(row=0, column=3, sticky="ew", padx=(8, 18))

        self.mirror_check = ttk.Checkbutton(row, text="Mirror text", variable=self.mirror_var, command=self._on_display_change)
        self.mirror_check.grid(row=0, column=4, sticky="w", padx=(0, 14))
        self.topmost_check = ttk.Checkbutton(row, text="Keep Window On Top", variable=self.topmost_var, command=self._apply_window_flags)
        self.topmost_check.grid(row=0, column=5, sticky="w", padx=(0, 14))
        ttk.Label(row, text="Keeps this teleprompter window above other windows while recording.").grid(row=1, column=5, columnspan=2, sticky="w", pady=(4, 0))
        self.fullscreen_check = ttk.Checkbutton(row, text="Fullscreen", variable=self.fullscreen_var, command=self._apply_window_flags)
        self.fullscreen_check.grid(row=0, column=6, sticky="w")

        row.columnconfigure(1, weight=1)
        row.columnconfigure(3, weight=1)

    def _build_edit_tab(self):
        frame = ttk.Frame(self.edit_tab, padding=10)
        frame.pack(fill="both", expand=True)

        self.editor = tk.Text(
            frame,
            wrap="word",
            undo=True,
            bg="#090909",
            fg="#F5F5F5",
            insertbackground="#FFFFFF",
            selectbackground="#0C5A9E",
            relief="flat",
            padx=12,
            pady=12,
            font=("Segoe UI", 14),
        )
        editor_scroll = ttk.Scrollbar(frame, orient="vertical", command=self.editor.yview)
        self.editor.configure(yscrollcommand=editor_scroll.set)
        self.editor.pack(side="left", fill="both", expand=True)
        editor_scroll.pack(side="right", fill="y")
        self.editor.bind("<<Modified>>", self._on_editor_modified)
        self.editor.bind("<KeyRelease>", self._on_editor_key_release)

    def _build_help_tab(self):
        frame = ttk.Frame(self.help_tab, padding=18)
        frame.pack(fill="both", expand=True)
        help_text = (
            "UVP Teleprompter\n\n"
            "Keyboard shortcuts:\n"
            "  Space      = Start / Pause teleprompter\n"
            "  R          = Reset teleprompter scroll\n"
            "  Up / Down  = Increase / decrease speed\n"
            "  + or =     = Increase font size\n"
            "  -          = Decrease font size\n"
            "  F11        = Toggle fullscreen\n"
            "  Esc        = Exit fullscreen or close popup\n"
            "  Mouse wheel= Manual scroll while in teleprompter mode\n"
            "  Page Up/Down = Jump larger sections\n"
            "  Home / End = Jump to top or bottom\n\n"
            "Timer and recording:\n"
            "  - Use Session Timer for elapsed or countdown timing.\n"
            "  - Recording saves to the recordings folder.\n"
            "  - MP3 export needs ffmpeg. WAV always works.\n\n"
            "Tips:\n"
            "  - Use the Edit tab to paste or type your script.\n"
            "  - Open .txt files or save your current script for later.\n"
            "  - Use Pop Out Window to place the prompter on another monitor.\n"
            "  - Mirror Text is useful when reading through teleprompter glass.\n"
        )
        lbl = tk.Label(
            frame,
            text=help_text,
            justify="left",
            anchor="nw",
            bg="#121212",
            fg="#F2F2F2",
            font=("Segoe UI", 12),
        )
        lbl.pack(fill="both", expand=True)

    def _bind_shortcuts(self):
        self.root.bind_all("<space>", self._shortcut_toggle_running, add="+")
        self.root.bind_all("<KeyPress-r>", self._shortcut_reset, add="+")
        self.root.bind_all("<KeyPress-R>", self._shortcut_reset, add="+")
        self.root.bind_all("<Up>", self._shortcut_speed_up, add="+")
        self.root.bind_all("<Down>", self._shortcut_speed_down, add="+")
        self.root.bind_all("<KeyPress-plus>", self._shortcut_font_up, add="+")
        self.root.bind_all("<KeyPress-equal>", self._shortcut_font_up, add="+")
        self.root.bind_all("<KeyPress-minus>", self._shortcut_font_down, add="+")
        self.root.bind_all("<F11>", self._shortcut_fullscreen_toggle, add="+")
        self.root.bind_all("<Escape>", self._shortcut_escape, add="+")
        self.root.bind_all("<Control-s>", self._shortcut_save, add="+")
        self.root.bind_all("<Control-o>", self._shortcut_open, add="+")
        self.root.bind_all("<Prior>", self._shortcut_page_up, add="+")
        self.root.bind_all("<Next>", self._shortcut_page_down, add="+")
        self.root.bind_all("<Home>", self._shortcut_home, add="+")
        self.root.bind_all("<End>", self._shortcut_end, add="+")

    def _focus_is_editor(self) -> bool:
        widget = self.root.focus_get()
        if widget is None:
            return False
        try:
            if widget == self.editor or str(widget).startswith(str(self.editor)):
                return True
        except Exception:
            pass
        return False

    def _prompter_context_active(self) -> bool:
        return self.notebook.index(self.notebook.select()) == 1 or self.popup is not None

    def _shortcut_allows_prompter_action(self) -> bool:
        return self._prompter_context_active() and not self._focus_is_editor()

    def _shortcut_toggle_running(self, _event):
        if not self._shortcut_allows_prompter_action():
            return
        self.toggle_running()
        return "break"

    def _shortcut_reset(self, _event):
        if not self._shortcut_allows_prompter_action():
            return
        self.reset_scrolling()
        return "break"

    def _shortcut_speed_up(self, _event):
        if not self._shortcut_allows_prompter_action():
            return
        self.speed_var.set(min(220, self.speed_var.get() + 10))
        self._on_speed_change()
        return "break"

    def _shortcut_speed_down(self, _event):
        if not self._shortcut_allows_prompter_action():
            return
        self.speed_var.set(max(5, self.speed_var.get() - 10))
        self._on_speed_change()
        return "break"

    def _shortcut_font_up(self, _event):
        if not self._shortcut_allows_prompter_action():
            return
        self.font_var.set(min(84, self.font_var.get() + 2))
        self._on_font_change()
        return "break"

    def _shortcut_font_down(self, _event):
        if not self._shortcut_allows_prompter_action():
            return
        self.font_var.set(max(18, self.font_var.get() - 2))
        self._on_font_change()
        return "break"

    def _shortcut_fullscreen_toggle(self, _event):
        if not self._shortcut_allows_prompter_action():
            return
        self.fullscreen_var.set(not self.fullscreen_var.get())
        self._apply_window_flags()
        return "break"

    def _shortcut_escape(self, _event):
        if self.fullscreen_var.get():
            self.fullscreen_var.set(False)
            self._apply_window_flags()
            return "break"
        if self.popup is not None:
            self.close_popup_window()
            return "break"

    def _shortcut_save(self, _event):
        if self._focus_is_editor() or self._prompter_context_active():
            self.save_script()
            return "break"

    def _shortcut_open(self, _event):
        if self._focus_is_editor() or self._prompter_context_active():
            self.open_script()
            return "break"

    def _shortcut_page_up(self, _event):
        if not self._shortcut_allows_prompter_action():
            return
        self.adjust_scroll(-self._page_scroll_amount())
        return "break"

    def _shortcut_page_down(self, _event):
        if not self._shortcut_allows_prompter_action():
            return
        self.adjust_scroll(self._page_scroll_amount())
        return "break"

    def _shortcut_home(self, _event):
        if not self._shortcut_allows_prompter_action():
            return
        self.jump_to_start()
        return "break"

    def _shortcut_end(self, _event):
        if not self._shortcut_allows_prompter_action():
            return
        self.jump_to_end()
        return "break"

    def _page_scroll_amount(self) -> float:
        if not self.views:
            return 500.0
        return float(max(view.viewport_height() for view in self.views) * 0.85)

    def _on_editor_modified(self, _event=None):
        if self.editor.edit_modified():
            self.editor.edit_modified(False)
            self._schedule_editor_sync()

    def _on_editor_key_release(self, _event=None):
        self._schedule_editor_sync()

    def _schedule_editor_sync(self):
        if self._editor_update_job is not None:
            self.root.after_cancel(self._editor_update_job)
        self._editor_update_job = self.root.after(120, self._sync_from_editor)

    def _load_state_into_editor(self):
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", self.state.script_text)
        self.editor.edit_modified(False)

    def _sync_from_editor(self):
        self._editor_update_job = None
        self.state.script_text = self.editor.get("1.0", "end-1c")
        self._refresh_views(force=True)

    def _on_speed_change(self):
        self.state.speed_px_per_sec = float(self.speed_var.get())
        preset_name = self._nearest_speed_preset(self.state.speed_px_per_sec)
        self.speed_preset_var.set(preset_name)
        self.speed_display_var.set(f"Current speed: {int(self.state.speed_px_per_sec)} px/s")
        self._refresh_views(force=False)

    def _nearest_speed_preset(self, speed: float) -> str:
        return min(PRESET_SPEEDS, key=lambda name: abs(PRESET_SPEEDS[name] - speed))

    def _on_font_change(self):
        self.state.font_size = int(float(self.font_var.get()))
        self._refresh_views(force=True)

    def _on_display_change(self):
        self.state.mirror = bool(self.mirror_var.get())
        self._refresh_views(force=True)

    def set_speed_preset(self, preset_name: str, speed: int):
        self.speed_preset_var.set(preset_name)
        self.speed_var.set(speed)
        self._on_speed_change()

    def _refresh_views(self, force: bool = False):
        self.state.script_text = self.editor.get("1.0", "end-1c")
        for view in self.views:
            view.redraw(force=force)
        if self.popup_view is not None:
            self.popup_view.redraw(force=force)

    def _set_status(self, message: str):
        self.status_var.set(message)

    def _apply_window_flags(self):
        self.state.always_on_top = bool(self.topmost_var.get())
        self.state.fullscreen = bool(self.fullscreen_var.get())
        target_windows = [self.root]
        if self.popup is not None:
            target_windows.append(self.popup)
        for window in target_windows:
            try:
                window.attributes("-topmost", self.state.always_on_top)
                window.attributes("-fullscreen", self.state.fullscreen)
            except tk.TclError:
                pass

    def open_script(self):
        path = filedialog.askopenfilename(
            title="Open script text file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = Path(path).read_text(encoding="utf-8-sig")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open file:\n{exc}")
            return
        self.current_file = Path(path)
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", text)
        self.editor.edit_modified(False)
        self.state.script_text = text
        self._refresh_views(force=True)
        self._set_status(f"Loaded {self.current_file.name}")

    def save_script(self):
        if self.current_file is None:
            self.save_script_as()
            return
        self._save_to_path(self.current_file)

    def save_script_as(self):
        path = filedialog.asksaveasfilename(
            title="Save script as",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile="script.txt",
        )
        if not path:
            return
        self._save_to_path(Path(path))

    def _save_to_path(self, path: Path):
        try:
            text = self.editor.get("1.0", "end-1c")
            path.write_text(text, encoding="utf-8")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not save file:\n{exc}")
            return
        self.current_file = path
        self._set_status(f"Saved {path.name}")

    def toggle_running(self):
        self.state.running = not self.state.running
        self._set_status("Teleprompter running" if self.state.running else "Teleprompter paused")

    def reset_scrolling(self):
        self.state.running = False
        self.state.scroll_px = 0.0
        self._refresh_views(force=False)
        self._set_status("Teleprompter reset to top")

    def _current_scroll_limit(self) -> float:
        if not self.views:
            return 0.0
        return max(view.scroll_limit() for view in self.views)

    def _clamp_scroll(self, value: float) -> float:
        return max(0.0, min(value, self._current_scroll_limit()))

    def adjust_scroll(self, delta_px: float):
        self.state.scroll_px = self._clamp_scroll(self.state.scroll_px + delta_px)
        self._refresh_views(force=False)

    def set_scroll_fraction(self, fraction: float):
        fraction = max(0.0, min(1.0, fraction))
        self.state.scroll_px = self._clamp_scroll(self._current_scroll_limit() * fraction)
        self._refresh_views(force=False)

    def jump_to_start(self):
        self.state.scroll_px = 0.0
        self._refresh_views(force=False)

    def jump_to_end(self):
        self.state.scroll_px = self._current_scroll_limit()
        self._refresh_views(force=False)

    def _update_timer_mode_label(self):
        self.timer_mode_var.set("Countdown" if self.timer_countdown_var.get() else "Elapsed")

    def _parse_target_minutes(self) -> float:
        raw = self.timer_target_minutes_var.get().strip()
        if not raw:
            return 0.0
        try:
            value = float(raw)
        except ValueError:
            raise ValueError("Target minutes must be a number.")
        if value < 0:
            raise ValueError("Target minutes cannot be negative.")
        return value

    def start_timer(self):
        now = time.perf_counter()
        try:
            target_minutes = self._parse_target_minutes()
        except Exception as exc:
            self.timer_status_var.set(str(exc))
            self._set_status(str(exc))
            return

        if self.timer_countdown_var.get() and self.timer_alerted and self.timer_accumulated_seconds >= target_minutes * 60:
            self.timer_accumulated_seconds = 0.0
            self.timer_alerted = False

        if not self.timer_running:
            self.timer_segment_started_at = now
        elif self.timer_paused:
            self.timer_segment_started_at = now

        self.timer_running = True
        self.timer_paused = False
        self.timer_status_var.set("Timer running")
        self._set_status("Timer started")

    def pause_timer(self):
        if not self.timer_running:
            self.timer_status_var.set("Timer is not running.")
            return
        if not self.timer_paused:
            self.timer_accumulated_seconds += time.perf_counter() - self.timer_segment_started_at
            self.timer_paused = True
            self.timer_status_var.set("Timer paused")
            self._set_status("Timer paused")
        else:
            self.timer_segment_started_at = time.perf_counter()
            self.timer_paused = False
            self.timer_status_var.set("Timer running")
            self._set_status("Timer resumed")

    def reset_timer(self):
        self.timer_running = False
        self.timer_paused = False
        self.timer_accumulated_seconds = 0.0
        self.timer_segment_started_at = 0.0
        self.timer_alerted = False
        self.timer_status_var.set("Timer reset")
        self._refresh_timer_display(time.perf_counter())
        self._set_status("Timer reset")

    def _refresh_timer_display(self, now: float):
        try:
            target_minutes = self._parse_target_minutes()
        except Exception:
            target_minutes = 0.0
        elapsed = self.timer_accumulated_seconds
        if self.timer_running and not self.timer_paused:
            elapsed += now - self.timer_segment_started_at
        if self.timer_countdown_var.get():
            remaining = max(0.0, target_minutes * 60 - elapsed)
            self.timer_display_var.set(format_hms(remaining))
            if self.timer_running and not self.timer_paused and target_minutes > 0 and elapsed >= target_minutes * 60 and not self.timer_alerted:
                self.timer_alerted = True
                self.timer_running = False
                self.timer_status_var.set("Countdown reached zero")
                self._set_status("Countdown reached zero")
                self.root.bell()
                self.timer_display_label.configure(fg="#FF6B6B")
            else:
                self.timer_display_label.configure(fg="#F7F7F7")
        else:
            self.timer_display_var.set(format_hms(elapsed))
            self.timer_display_label.configure(fg="#F7F7F7")
        self._update_timer_mode_label()

    def start_recording(self):
        success, message = self.recorder.start(self.recording_format_var.get())
        self.recording_status_var.set(self.recorder.status)
        self.recording_hint_var.set(message)
        if success:
            self._set_status(message)
        else:
            messagebox.showwarning(APP_NAME, message)
            self._set_status(message)

    def pause_or_resume_recording(self):
        if not self.recorder.active:
            self.recording_status_var.set("Idle")
            self.recording_hint_var.set("No recording is active.")
            return
        success, message = self.recorder.toggle_pause()
        if success:
            self.recording_status_var.set(self.recorder.status)
            self.recording_hint_var.set(message)
            self._set_status(message)
            self.pause_record_btn.configure(text="Resume Recording" if self.recorder.paused else "Pause Recording")
        else:
            self.recording_hint_var.set(message)
            self._set_status(message)

    def stop_recording(self):
        success, message = self.recorder.stop()
        self.recording_status_var.set(self.recorder.status)
        self.recording_hint_var.set(message)
        self.pause_record_btn.configure(text="Pause Recording")
        if self.recorder.last_saved_path is not None:
            self.recording_last_saved_var.set(str(self.recorder.last_saved_path))
        if success:
            self._set_status(message)
            messagebox.showinfo(APP_NAME, message)
        else:
            self._set_status(message)
            messagebox.showwarning(APP_NAME, message)

    def open_recordings_folder(self):
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(self.recordings_dir))
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open recordings folder:\n{exc}")

    def toggle_popup_window(self):
        if self.popup is not None:
            self.close_popup_window()
            return
        self.popup = tk.Toplevel(self.root)
        self.popup.title(f"{APP_NAME} - Teleprompter")
        self.popup.configure(bg="#050505")
        self.popup.protocol("WM_DELETE_WINDOW", self.close_popup_window)
        self.popup_view = PrompterView(self, self.popup, "Popup View", is_popup=True)
        self.views.append(self.popup_view)
        self._apply_window_flags()
        self._refresh_views(force=True)
        self._set_status("Popup teleprompter opened")

    def close_popup_window(self):
        if self.popup is None:
            return
        if self.popup_view is not None:
            try:
                self.views.remove(self.popup_view)
            except ValueError:
                pass
        try:
            self.popup.destroy()
        except Exception:
            pass
        self.popup = None
        self.popup_view = None
        self._apply_window_flags()
        self._refresh_views(force=True)
        self._set_status("Popup teleprompter closed")

    def _refresh_recording_display(self, now: float):
        self.recording_status_var.set(self.recorder.status)
        self.recording_elapsed_var.set(format_hms(self.recorder.elapsed_seconds(now)))
        self.recording_folder_var.set(str(self.recordings_dir))
        if self.recorder.last_saved_path is not None:
            self.recording_last_saved_var.set(str(self.recorder.last_saved_path))
        else:
            self.recording_last_saved_var.set("None")

        if self.recorder.active:
            if self.recorder.paused:
                self.pause_record_btn.configure(text="Resume Recording")
            else:
                self.pause_record_btn.configure(text="Pause Recording")
        else:
            self.pause_record_btn.configure(text="Pause Recording")

        if not self.audio_support.available:
            self.recording_status_var.set("Recording unavailable")
            self.recording_hint_var.set(self.audio_support.message)
        elif self.recorder.output_format == "MP3" and not self.audio_support.can_mp3:
            self.recording_hint_var.set("MP3 export requires ffmpeg. WAV recording will still work.")
        elif not self.recorder.active and self.recorder.status == "Idle":
            self.recording_hint_var.set(self.audio_support.message)

    def _animate(self):
        now = time.perf_counter()
        dt = now - self._last_tick
        self._last_tick = now

        if self.state.running:
            self.state.scroll_px = self._clamp_scroll(self.state.scroll_px + self.state.speed_px_per_sec * dt)
            if self.state.scroll_px >= self._current_scroll_limit() > 0:
                self.state.running = False
                self._set_status("Reached end of script")
            self._refresh_views(force=False)
        else:
            for view in self.views:
                view.tick()

        self._refresh_timer_display(now)
        self._refresh_recording_display(now)
        self.root.after(20, self._animate)


def ensure_support_files(project_root: Path):
    sample_path = project_root / "scripts" / "sample_script.txt"
    sample_path.parent.mkdir(parents=True, exist_ok=True)
    if not sample_path.exists():
        sample_path.write_text(
            (
                "UVP Sample Teleprompter Script\n\n"
                "Hello, and thank you for watching.\n"
                "This is a simple sample script you can edit, save, and load later.\n"
                "Use the Space bar to start or pause the scroll.\n"
                "Adjust speed and font size while you rehearse.\n"
                "Open the popup teleprompter on another monitor for recording.\n"
            ),
            encoding="utf-8",
        )
    (project_root / "recordings").mkdir(parents=True, exist_ok=True)
    (project_root / "backups").mkdir(parents=True, exist_ok=True)


def main():
    project_root = Path(__file__).resolve().parent.parent
    ensure_support_files(project_root)

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(
            f"{APP_NAME} could not start because Tk/Tcl is unavailable in this Python installation.\n"
            f"Error: {exc}\n\n"
            "Install a standard Windows Python build that includes Tcl/Tk, then run the app again.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    app = TeleprompterApp(root)

    try:
        root.iconname(APP_NAME)
    except Exception:
        pass

    def on_close():
        if messagebox.askokcancel(APP_NAME, "Exit UVP Teleprompter?"):
            try:
                if app.recorder.active:
                    app.recorder.stop()
            except Exception:
                pass
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
