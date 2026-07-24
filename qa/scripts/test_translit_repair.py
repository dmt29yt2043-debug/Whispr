"""TC_TRANSLIT_* — reverse-transliteration of mis-romanized Russian words."""
from _harness import case, run_all

import translit_repair as t
from anti_hallucination import filter_transcription


@case("TC_TRANSLIT_DUSA", "translit_repair",
      "the reported case: 'Duša' → 'Душа'")
def test_dusa():
    assert t.repair("Duša") == "Душа"


@case("TC_TRANSLIT_IN_SENTENCE", "translit_repair",
      "only the broken word is fixed, rest of the Russian sentence untouched")
def test_in_sentence():
    src = "когда Duša после смерти попадает на некий совет"
    out = t.repair(src)
    assert out == "когда Душа после смерти попадает на некий совет", out


@case("TC_TRANSLIT_HACEK_WORDS", "translit_repair",
      "various háček words reverse-transliterate correctly")
def test_hacek_words():
    assert t.repair("čaša") == "чаша"
    assert t.repair("žiznь") == "жизнь"
    assert t.repair("Čto") == "Что"


@case("TC_TRANSLIT_ENGLISH_UNTOUCHED", "translit_repair",
      "plain-ASCII English words are never converted (no trigger diacritic)")
def test_english_untouched():
    assert t.repair("Это practice и test") == "Это practice и test"
    assert t.repair("hello world") == "hello world"
    # English word that happens to sit next to a broken Russian one
    assert t.repair("open the Duša now") == "open the Душа now"


@case("TC_TRANSLIT_CYRILLIC_UNTOUCHED", "translit_repair",
      "already-Cyrillic Russian passes through unchanged")
def test_cyrillic_untouched():
    txt = "обычный русский текст без ошибок"
    assert t.repair(txt) == txt


@case("TC_TRANSLIT_NO_TRIGGER_NOOP", "translit_repair",
      "text without any trigger diacritic is returned as-is (fast path)")
def test_no_trigger():
    assert t.repair("just regular text 123") == "just regular text 123"
    assert t.repair("") == ""


@case("TC_TRANSLIT_PRESERVES_PUNCT", "translit_repair",
      "punctuation and capitalization around the fixed word are preserved")
def test_preserves_punct():
    assert t.repair("«Duša»!") == "«Душа»!"
    assert t.repair("Duša, čto?") == "Душа, что?"


@case("TC_TRANSLIT_VIA_FILTER", "translit_repair",
      "repair runs inside filter_transcription (both streaming and batch paths)")
def test_via_filter():
    out = filter_transcription("когда Duša после смерти")
    assert "Душа" in out
    assert "Duša" not in out


if __name__ == "__main__":
    run_all("test_translit_repair")
