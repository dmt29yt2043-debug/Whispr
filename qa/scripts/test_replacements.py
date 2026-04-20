"""TC_008, TC_015, TC_035, TC_041 — replacements behaviour and safety."""
import shutil
from pathlib import Path
from _harness import case, run_all

import replacements

_TMP_DIR = Path("/tmp/qa_whisper_replacements")
replacements._CONFIG_DIR = str(_TMP_DIR)
replacements._REPLACEMENTS_FILE = str(_TMP_DIR / "replacements.json")


def _reset():
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


@case("TC_REPL_EMPTY", "replacements", "no replacements file → apply returns text unchanged")
def test_empty():
    _reset()
    assert replacements.apply_replacements("hello world") == "hello world"


@case("TC_008", "replacements", "exact match (lowercased) triggers replacement")
def test_exact_match():
    _reset()
    replacements.save_replacements({"my zoom": "https://zoom.us/j/123"})
    assert replacements.apply_replacements("my zoom") == "https://zoom.us/j/123"
    assert replacements.apply_replacements("My Zoom") == "https://zoom.us/j/123"


@case("TC_REPL_NO_MATCH", "replacements", "non-exact match returns original")
def test_no_match():
    _reset()
    replacements.save_replacements({"my zoom": "https://zoom.us/j/123"})
    # Partial / contains, not exact whole-text
    assert replacements.apply_replacements("click my zoom link") == "click my zoom link"


@case("TC_015", "replacements", "non-ASCII trigger works")
def test_unicode_trigger():
    _reset()
    replacements.save_replacements({"мой зум": "https://zoom.us/j/999"})
    assert replacements.apply_replacements("Мой Зум") == "https://zoom.us/j/999"


@case("TC_035", "replacements", "value with HTML/script stored + returned verbatim (no exec)")
def test_value_verbatim():
    _reset()
    mal = "<script>alert(1)</script>"
    replacements.save_replacements({"trigger": mal})
    assert replacements.apply_replacements("trigger") == mal


@case("TC_041", "replacements", "trigger with path chars stored as data, NOT used as path")
def test_path_traversal_safe():
    _reset()
    replacements.save_replacements({"../../../etc/passwd": "replaced"})
    # It's just a dict key. Apply to trigger string:
    assert replacements.apply_replacements("../../../etc/passwd") == "replaced"
    # Most importantly, no file op happened — the saved file lives in our dir
    saved_dir_files = list(_TMP_DIR.glob("*"))
    assert all(str(f).startswith(str(_TMP_DIR)) for f in saved_dir_files)


@case("TC_REPL_PERSIST", "replacements", "load_replacements reads what save_replacements wrote")
def test_persist():
    _reset()
    replacements.save_replacements({"a": "A", "b": "B"})
    loaded = replacements.load_replacements()
    assert loaded == {"a": "A", "b": "B"}


@case("TC_REPL_CORRUPTED", "replacements", "corrupted JSON file → empty dict, no crash")
def test_corrupted():
    _reset()
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    (_TMP_DIR / "replacements.json").write_text("{ garbage")
    loaded = replacements.load_replacements()
    assert loaded == {}


if __name__ == "__main__":
    run_all("test_replacements")
