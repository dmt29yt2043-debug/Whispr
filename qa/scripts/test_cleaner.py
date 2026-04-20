"""TC_002, TC_003, TC_024, TC_033, TC_034 — cleaner logic and resilience."""
from unittest.mock import patch, MagicMock
from _harness import case, run_all

import cleaner
import settings as S


def _apply_settings(**kwargs):
    # Apply overrides in-memory; reset afterwards
    S._cache = dict(S.DEFAULTS)
    for k, v in kwargs.items():
        S._cache[k] = v


@case("TC_002", "cleaner", "short phrase (<=4 words) skips GPT")
def test_short_phrase_skip():
    _apply_settings(mode=S.MODE_AUTO, cleanup_enabled=True, base_tone=S.TONE_NEUTRAL)
    with patch.object(cleaner, "OpenAI") as mock_openai:
        result = cleaner.clean_text("make a commit")
        assert result == "make a commit"
        mock_openai.assert_not_called()


@case("TC_003", "cleaner", "clean speech without fillers skips GPT")
def test_no_filler_skip():
    _apply_settings(mode=S.MODE_AUTO, cleanup_enabled=True)
    with patch.object(cleaner, "OpenAI") as mock_openai:
        result = cleaner.clean_text("this is a clean sentence without any filler words at all")
        assert result == "this is a clean sentence without any filler words at all"
        mock_openai.assert_not_called()


@case("TC_CLEANER_FILLER_CALLS", "cleaner", "speech with fillers triggers GPT call")
def test_filler_calls_gpt():
    _apply_settings(mode=S.MODE_AUTO, cleanup_enabled=True, base_tone=S.TONE_NEUTRAL)
    with patch.object(cleaner, "OpenAI") as mock_openai:
        instance = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "cleaned text"
        mock_response.usage.prompt_tokens = 20
        mock_response.usage.completion_tokens = 5
        instance.chat.completions.create.return_value = mock_response
        mock_openai.return_value = instance

        import os
        os.environ["OPENAI_API_KEY"] = "sk-test"
        result = cleaner.clean_text("ну типа короче это работает в общем как бы")
        assert result == "cleaned text"
        instance.chat.completions.create.assert_called_once()


@case("TC_024", "cleaner", "GPT returns None content → falls back to raw")
def test_none_content():
    _apply_settings(mode=S.MODE_AUTO, cleanup_enabled=True)
    with patch.object(cleaner, "OpenAI") as mock_openai:
        instance = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = None  # content filter
        instance.chat.completions.create.return_value = mock_response
        mock_openai.return_value = instance

        import os
        os.environ["OPENAI_API_KEY"] = "sk-test"
        raw = "ну типа короче это работает"
        result = cleaner.clean_text(raw)
        assert result == raw


@case("TC_033", "cleaner", "network error → raw text returned")
def test_network_error_fallback():
    _apply_settings(mode=S.MODE_AUTO, cleanup_enabled=True)
    with patch.object(cleaner, "OpenAI") as mock_openai:
        instance = MagicMock()
        instance.chat.completions.create.side_effect = ConnectionError("no net")
        mock_openai.return_value = instance

        import os
        os.environ["OPENAI_API_KEY"] = "sk-test"
        raw = "ну короче это работает"
        result = cleaner.clean_text(raw)
        assert result == raw


@case("TC_CLEANER_TONE_RAW", "cleaner", "tone=raw always skips GPT")
def test_tone_raw():
    _apply_settings(mode=S.MODE_AUTO, cleanup_enabled=True, base_tone=S.TONE_RAW)
    with patch.object(cleaner, "OpenAI") as mock_openai:
        raw = "ну типа короче это работает"
        result = cleaner.clean_text(raw)
        assert result == raw
        mock_openai.assert_not_called()


@case("TC_CLEANER_LOCAL_MODE", "cleaner", "mode=local skips GPT entirely")
def test_local_mode():
    _apply_settings(mode=S.MODE_LOCAL, cleanup_enabled=True)
    with patch.object(cleaner, "OpenAI") as mock_openai:
        raw = "ну типа короче это работает текст достаточно длинный чтобы не попасть в short phrase skip"
        result = cleaner.clean_text(raw)
        assert result == raw
        mock_openai.assert_not_called()


@case("TC_CLEANER_NO_API_KEY", "cleaner", "no API key → raw text (no crash)")
def test_no_api_key():
    _apply_settings(mode=S.MODE_AUTO, cleanup_enabled=True)
    import os
    prev = os.environ.pop("OPENAI_API_KEY", None)
    try:
        result = cleaner.clean_text("ну типа короче длинный текст с филлерами")
        assert "ну" in result  # returned verbatim
    finally:
        if prev:
            os.environ["OPENAI_API_KEY"] = prev


if __name__ == "__main__":
    run_all("test_cleaner")
