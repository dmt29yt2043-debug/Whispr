# Whisper Dictation

macOS voice dictation app — hold Fn to record, release to transcribe and paste. A lightweight Whispr Flow alternative.

## Features

- **Hold Fn** to record, release to transcribe and paste
- **Double-tap Fn** for hands-free toggle mode (tap again to stop)
- Transcription via OpenAI Whisper API (with offline fallback to local faster-whisper)
- Text cleanup via GPT-4o-mini (removes filler words, fixes grammar)
- Text replacements (e.g., say "my zoom" → pastes your Zoom link)
- Word count statistics (today / week / month)
- Lives in the macOS menu bar — no windows, no distractions

## Installation

```bash
cd whisper-dictation
pip3 install -r requirements.txt
```

## Set up OpenAI API Key

Create a `.env` file in the project directory (or parent directory):

```
OPENAI_API_KEY=sk-your-key-here
```

Or set it as an environment variable:

```bash
export OPENAI_API_KEY=sk-your-key-here
```

## Download Local Model (optional, for offline mode)

```bash
python3 -c "from faster_whisper import WhisperModel; WhisperModel('medium', device='cpu', compute_type='int8')"
```

This downloads ~1.5 GB to `~/.cache/huggingface/`. Only needs to be done once.

## Grant macOS Permissions

The app requires two permissions in **System Settings > Privacy & Security**:

1. **Accessibility** — required for global Fn key detection and simulated Cmd+V paste
2. **Microphone** — required for audio recording

Add your terminal app (Terminal, iTerm, etc.) or Python to both lists.

## Run

```bash
python3 app.py
```

The mic icon 🎙 appears in the menu bar.

## Usage

| Action | What happens |
|--------|-------------|
| Hold Fn | Records while held, transcribes on release |
| Double-tap Fn | Starts recording (hands-free), tap Fn again to stop |
| Click menu bar icon | Shows statistics and text replacement settings |

## Text Replacements

Click "🔄 Text Replacements" in the menu bar dropdown to add/remove replacements.

Example: add trigger "my zoom" → replacement "https://zoom.us/j/123456"

Now when you say "my zoom", it pastes your Zoom link instead.

Replacements are stored in `~/.whisper-dictation/replacements.json`.

## Offline Mode

If the OpenAI API is unreachable, the app automatically falls back to the local faster-whisper model. Text cleanup (GPT) is skipped in offline mode — raw transcription is pasted directly.
