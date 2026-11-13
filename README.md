# UVP Teleprompter

A local, offline teleprompter for rehearsing and recording on Windows. Edit a text script, adjust scrolling and font size, open a second-monitor view, and use a session timer. Optional microphone recording stays on your computer.

## Start

Install Python 3.10+ with Tcl/Tk (Python 3.11 or 3.12 is recommended for optional audio), then double-click `run_teleprompter.bat`, or run:

```bash
python app/uvp_teleprompter.py
```

The base app needs no pip packages. A sample script is in `scripts/sample_script.txt`. Open and save UTF-8 text files; a UTF-8 byte order mark is accepted.

## Controls

| Action | Shortcut |
| --- | --- |
| Start / pause | Space |
| Reset scroll | R |
| Faster / slower | Up / Down |
| Larger / smaller font | + / - |
| Fullscreen | F11 |
| Leave fullscreen / close popup | Escape |
| Manual scroll | Mouse wheel, Page Up / Page Down |
| Jump to beginning / end | Home / End |
| Open / save script | Ctrl+O / Ctrl+S |

Reading shortcuts act in the prompter view or popup; they do not consume typing in the script editor. The editor shows word count and estimated reading time at 150 words per minute. This estimate is independent of pixel-based scroll speed.

Long scripts continue scrolling beyond the first screen. The main and popup views share scroll state. Reverse text reverses character order; it does not optically mirror glyphs for a glass teleprompter.

## Optional local audio

In a virtual environment, install only the recording features you need:

```bash
python -m pip install -r requirements-audio.txt
```

WAV needs `sounddevice` and `soundfile` plus a working microphone/PortAudio. MP3 additionally needs `pydub` and an executable `ffmpeg` on PATH. MP3 conversion keeps the WAV when conversion fails. The app reports dropped blocks and write errors; do not treat an error as a complete recording. Recording requires a separate explicit click and does not start automatically.

Recordings are saved under `recordings/` beside the checkout, with a unique timestamp. Scripts and recordings are not uploaded. That directory must be writable; avoid installing into a protected system folder. Local files are not automatically encrypted or anonymized.

## Development and limitations

```bash
python -m unittest discover -s tests -v
python -m compileall -q app tests
```

Regression tests use controlled canvas and audio doubles, including long-scroll, text decoding, invalid timers, missing ffmpeg, writer failures and safe writer finalization. They do not replace a Windows GUI, actual microphone, long recording, second-monitor or MP3 acceptance test. Optional audio compatibility on Python 3.13+ is not established.

Before replacing an unsaved script or closing the app, save changes. There is no automatic script recovery. Hardware and environment failures can leave a partial WAV that needs inspection.

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) and [LICENSE](LICENSE). The source is public; an open-source license has not yet been granted.
