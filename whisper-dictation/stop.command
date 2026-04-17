#!/bin/bash
# Force-kill Whisper Dictation if it hangs
pkill -9 -f "app.py" 2>/dev/null
pkill -9 -f "whisper-dictation" 2>/dev/null
echo "Whisper Dictation stopped."
sleep 1
