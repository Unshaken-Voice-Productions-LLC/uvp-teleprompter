# UVP Teleprompter

UVP Teleprompter is a local offline teleprompter for recording videos on Windows.

## What’s in v2.1

- Visible dashboard with:
  - Script Controls
  - Teleprompter Controls
  - Session Timer
  - Audio Recording
- Right-side scrollbars in teleprompter and pop-out views
- Mouse wheel scrolling on the teleprompter display
- Session timer with optional countdown mode
- Local audio recording to WAV or MP3
- MP3 export via `pydub` and `ffmpeg`
- Version label: `UVP Teleprompter v2.1`

## How to run

1. Make sure Python 3 and Tkinter are installed.
2. Double-click `run_teleprompter.bat`, or run:

```bat
python -u app\uvp_teleprompter.py
```

## How to test Tkinter

Run this from a terminal:

```bat
python -c "import tkinter as tk; root = tk.Tk(); root.destroy(); print('Tkinter works')"
```

If that fails, install a standard Windows Python build that includes Tcl/Tk support.

## Audio recording setup

Install the recommended audio packages:

```bat
pip install sounddevice soundfile pydub
```

If MP3 export is desired, make sure `ffmpeg` is installed and on `PATH`.

## How recordings are saved

- Recordings are saved in the local `recordings\` folder.
- Default filenames include the current date and time, such as:

```text
UVP_recording_2026-05-17_1430.wav
```

- WAV is the baseline format.
- If MP3 is selected, the app records WAV first and then converts to MP3 when possible.

## Troubleshooting

- If recording buttons are disabled, check that `sounddevice`, `soundfile`, and `pydub` are installed and that a microphone is available.
- If MP3 export fails, the WAV file is still kept and the app shows a friendly message.
- If no microphone is available, the app should stay open and avoid crashing.

## Keyboard shortcuts

- `Space` = start/pause teleprompter
- `R` = reset teleprompter scroll
- `Up` = increase speed
- `Down` = decrease speed
- `+` or `=` = increase font size
- `-` = decrease font size
- `F11` = fullscreen toggle
- `Escape` = exit fullscreen or close popup
- Mouse wheel = manual scroll in teleprompter mode or popup mode
- `Page Up` / `Page Down` = larger manual scroll jumps
- `Home` = jump to top
- `End` = jump to bottom

## Notes

- The app works fully offline.
- No paid APIs or cloud services are required.
- A sample script is included in `scripts/sample_script.txt`.
