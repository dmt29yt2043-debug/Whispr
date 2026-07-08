"""TC_DICT_* / TC_SNIP_* — personal dictionary and voice snippets."""
import os
import tempfile
from _harness import case, run_all

import dictionary
import snippets
import settings as S
import cleaner


def _fresh_dict(tmp_path: str):
    """Point the dictionary module at a temp file and reset its cache."""
    dictionary._DICT_PATH = tmp_path
    dictionary._cache = []
    dictionary._cache_mtime = -1.0


def _ensure_settings():
    """Settings cache starts as None until load(); tests need a dict."""
    if S._cache is None:
        S._cache = dict(S.DEFAULTS)


@case("TC_DICT_EMPTY", "dictionary", "no file → no terms, empty prompts")
def test_dict_empty():
    _fresh_dict(os.path.join(tempfile.mkdtemp(), "dict.txt"))
    assert dictionary.get_terms() == []
    assert dictionary.transcription_prompt() == ""
    assert dictionary.cleanup_instruction() == ""


@case("TC_DICT_ADD_AND_PROMPT", "dictionary",
      "added terms appear in transcription prompt and cleanup instruction")
def test_dict_add_and_prompt():
    _fresh_dict(os.path.join(tempfile.mkdtemp(), "dict.txt"))
    assert dictionary.add_term("Whispr Flow") is True
    assert dictionary.add_term("RIZY") is True
    terms = dictionary.get_terms()
    assert terms == ["Whispr Flow", "RIZY"]

    p = dictionary.transcription_prompt()
    assert "Whispr Flow" in p and "RIZY" in p
    assert p.startswith("Glossary:")

    c = dictionary.cleanup_instruction()
    assert "RIZY" in c and "EXACTLY" in c


@case("TC_DICT_DEDUP", "dictionary", "case-insensitive dedup on add")
def test_dict_dedup():
    _fresh_dict(os.path.join(tempfile.mkdtemp(), "dict.txt"))
    assert dictionary.add_term("RIZY") is True
    assert dictionary.add_term("rizy") is False, "case-insensitive duplicate must be rejected"
    assert dictionary.get_terms() == ["RIZY"]


@case("TC_DICT_COMMENTS_SKIPPED", "dictionary", "# comment lines are ignored")
def test_dict_comments():
    d = os.path.join(tempfile.mkdtemp(), "dict.txt")
    with open(d, "w", encoding="utf-8") as f:
        f.write("# names\nМаксим\n\n# brands\nRIZY\n")
    _fresh_dict(d)
    assert dictionary.get_terms() == ["Максим", "RIZY"]


@case("TC_DICT_IN_CLEANER_PROMPT", "dictionary",
      "cleanup system prompt embeds dictionary terms")
def test_dict_in_cleaner_prompt():
    d = os.path.join(tempfile.mkdtemp(), "dict.txt")
    with open(d, "w", encoding="utf-8") as f:
        f.write("Whispr Flow\n")
    _fresh_dict(d)
    prompt = cleaner._build_system_prompt("neutral", False, "")
    assert "Whispr Flow" in prompt


@case("TC_SNIP_EXACT_MATCH", "snippets", "exact trigger → template returned")
def test_snip_exact():
    _ensure_settings()
    S._cache["snippets"] = {"моя подпись": "С уважением,\nМаксим"}
    try:
        out = snippets.expand("Моя подпись.")
        assert out == "С уважением,\nМаксим", f"got {out!r}"
    finally:
        S._cache["snippets"] = {}


@case("TC_SNIP_INSIDE_SENTENCE_IGNORED", "snippets",
      "trigger inside a longer sentence does NOT expand")
def test_snip_inside_sentence():
    _ensure_settings()
    S._cache["snippets"] = {"моя подпись": "SIG"}
    try:
        out = snippets.expand("добавь сюда моя подпись пожалуйста")
        assert out is None
    finally:
        S._cache["snippets"] = {}


@case("TC_SNIP_PUNCT_AND_CASE_INSENSITIVE", "snippets",
      "trailing punctuation and case are ignored when matching")
def test_snip_punct_case():
    _ensure_settings()
    S._cache["snippets"] = {"Ссылка на календарь": "https://cal.com/max"}
    try:
        assert snippets.expand("ссылка на календарь!") == "https://cal.com/max"
        assert snippets.expand("«Ссылка на календарь»") == "https://cal.com/max"
    finally:
        S._cache["snippets"] = {}


@case("TC_SNIP_NO_SNIPPETS", "snippets", "empty config → always None")
def test_snip_empty():
    _ensure_settings()
    S._cache["snippets"] = {}
    assert snippets.expand("моя подпись") is None


@case("TC_CLEANER_SELF_CORRECTION_IN_PROMPT", "cleaner",
      "system prompt instructs resolving spoken self-corrections")
def test_self_correction_prompt():
    prompt = cleaner._build_system_prompt("neutral", False, "")
    assert "self-correction" in prompt.lower()


@case("TC_CLEANER_DEFAULT_APP_TONES", "cleaner",
      "known apps get default tones; user overrides win; unknown → base_tone")
def test_default_app_tones():
    _ensure_settings()
    S._cache["app_tones"] = {}
    S._cache["base_tone"] = S.TONE_NEUTRAL
    try:
        assert cleaner._resolve_tone("com.tinyspeck.slackmacgap") == S.TONE_CASUAL
        assert cleaner._resolve_tone("com.apple.mail") == S.TONE_PROFESSIONAL
        assert cleaner._resolve_tone("com.microsoft.VSCode") == S.TONE_RAW
        assert cleaner._resolve_tone("com.unknown.app") == S.TONE_NEUTRAL
        # user override beats the default
        S._cache["app_tones"] = {"com.apple.mail": S.TONE_CASUAL}
        assert cleaner._resolve_tone("com.apple.mail") == S.TONE_CASUAL
    finally:
        S._cache["app_tones"] = {}


if __name__ == "__main__":
    run_all("test_dictionary_snippets")
