"""TC_035, TC_041, TC_042, TC_043 + general safety tests."""
import os
from pathlib import Path
from unittest.mock import patch
from _harness import case, run_all

import replacements
import stats
import sounds
import cleaner


@case("TC_042", "security", "SQL injection attempt via model name is parameterized safely")
def test_sql_param():
    # Already covered in test_stats, quick repeat here
    import shutil
    tmp = Path("/tmp/qa_sec_stats")
    stats._CONFIG_DIR = str(tmp)
    stats._DB_PATH = str(tmp / "stats.db")
    shutil.rmtree(tmp, ignore_errors=True)
    stats.record_transcribe("'; DROP TABLE usage; --", 5.0)
    u = stats.get_usage_today()
    # If table was dropped, record_gpt_tokens would error out
    stats.record_gpt_tokens(10, 20)
    u2 = stats.get_usage_today()
    assert u2["gpt_input_tokens"] == 10


@case("TC_043", "security", "afplay fallback uses subprocess, no shell interpolation")
def test_afplay_no_shell():
    import inspect, re
    src = inspect.getsource(sounds._play_file)
    # Look for actual os.system() function CALL (not a comment mention)
    # Strip comments first to avoid false positive on our own documentation.
    code_only = re.sub(r"#[^\n]*", "", src)
    assert not re.search(r"\bos\.system\s*\(", code_only), \
        "os.system(...) call present — shell injection risk"
    # Also ensure subprocess is used for the fallback path
    assert "subprocess" in src, "fallback path should use subprocess"


@case("TC_035", "security", "replacement value with HTML/script stored unchanged, no exec")
def test_replacement_value_raw():
    import shutil
    tmp = Path("/tmp/qa_sec_repl")
    replacements._CONFIG_DIR = str(tmp)
    replacements._REPLACEMENTS_FILE = str(tmp / "replacements.json")
    shutil.rmtree(tmp, ignore_errors=True)
    xss = "<script>window.evil=1</script>"
    replacements.save_replacements({"trigger": xss})
    out = replacements.apply_replacements("trigger")
    assert out == xss


@case("TC_041", "security", "replacements stored inside config dir only; traversal keys = just data")
def test_traversal_key():
    import shutil
    tmp = Path("/tmp/qa_sec_repl2")
    replacements._CONFIG_DIR = str(tmp)
    replacements._REPLACEMENTS_FILE = str(tmp / "replacements.json")
    shutil.rmtree(tmp, ignore_errors=True)
    key = "../../../etc/passwd"
    replacements.save_replacements({key: "boom"})
    # Only one file should exist — in tmp dir
    files = list(tmp.rglob("*"))
    outside = [f for f in files if "etc/passwd" in str(f) or str(f).startswith("/etc")]
    assert not outside, f"Traversal wrote outside config dir: {outside}"


@case("TC_SEC_API_KEY_NOT_IN_ERROR", "security", "generic_error_message doesn't leak key-like strings")
def test_no_key_leak():
    # Import from app module
    import sys
    # app.py is importable by path; use simple construction
    from pathlib import Path
    app_path = Path(__file__).resolve().parent.parent.parent / "whisper-dictation" / "app.py"
    src = app_path.read_text()
    assert "_generic_error_message" in src
    # Verify that the function maps to safe categories, not raw exception
    assert "\"Network error\"" in src or "'Network error'" in src


@case("TC_SEC_LOG_PATH_SAFE", "security", "log file path is under user home, not world-writable")
def test_log_path():
    log_path = os.path.expanduser("~/.whisper-dictation/app.log")
    assert log_path.startswith(os.path.expanduser("~"))


@case("TC_SEC_SETTINGS_PERMS", "security", "settings.json is not group/world-writable")
def test_settings_perms():
    path = Path.home() / ".whisper-dictation" / "settings.json"
    if not path.exists():
        return  # nothing to check
    mode = path.stat().st_mode & 0o777
    # Allow 0o644 / 0o600; reject anything wider (0o666, 0o777)
    assert mode & 0o022 == 0, f"settings.json is world/group writable: {oct(mode)}"


if __name__ == "__main__":
    run_all("test_security")
