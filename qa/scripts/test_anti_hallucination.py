"""TC_019, TC_020, TC_021 — anti-hallucination filter correctness."""
from _harness import case, run_all
from anti_hallucination import filter_transcription


@case("TC_019", "anti_hallucination", "phrase 'Thank you very much' → ''")
def test_thank_you():
    assert filter_transcription("Thank you very much.") == ""
    assert filter_transcription("thanks for watching!") == ""


@case("TC_020a", "anti_hallucination", "'[BLANK_AUDIO]' stripped to ''")
def test_blank_audio():
    assert filter_transcription("[BLANK_AUDIO]") == ""


@case("TC_020b", "anti_hallucination", "'(Music playing)' stripped to ''")
def test_music():
    assert filter_transcription("(Music playing)") == ""


@case("TC_020c", "anti_hallucination", "mixed noise + text → noise stripped, text kept")
def test_mixed_noise():
    out = filter_transcription("Hello world [Music]")
    assert "Hello world" in out
    assert "[Music]" not in out


@case("TC_021a", "anti_hallucination", "'you you you you' detected as repetition")
def test_repetition_single_word():
    assert filter_transcription("you you you you you you you") == ""


@case("TC_021b", "anti_hallucination", "'um um um um um um' detected as repetition")
def test_repetition_um():
    assert filter_transcription("um um um um um um uh uh") == ""


@case("TC_021c", "anti_hallucination", "short text is NOT flagged as repetition (<4 words)")
def test_short_not_repetition():
    assert filter_transcription("yes yes") == "yes yes"


@case("TC_019b", "anti_hallucination", "Russian 'Спасибо за просмотр' filtered")
def test_russian_phrase():
    assert filter_transcription("Спасибо за просмотр") == ""


@case("TC_NOISE_NORMAL", "anti_hallucination", "clean sentence passes through unchanged")
def test_clean_passthrough():
    txt = "Это обычный текст без галлюцинаций"
    assert filter_transcription(txt) == txt


@case("TC_NOISE_EMPTY", "anti_hallucination", "empty string → empty string")
def test_empty():
    assert filter_transcription("") == ""


if __name__ == "__main__":
    run_all("test_anti_hallucination")
