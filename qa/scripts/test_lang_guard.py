"""TC_LANG_* — foreign-language detection that triggers batch re-transcribe."""
from _harness import case, run_all

import lang_guard as g


@case("TC_LANG_CLEAN_RU", "lang_guard", "pure Russian → not suspicious")
def test_clean_russian():
    assert g.foreign_letters("Покажи мои задачи на сегодня") == []
    assert not g.is_suspicious("Покажи мои задачи на сегодня")


@case("TC_LANG_CLEAN_EN", "lang_guard", "pure English → not suspicious")
def test_clean_english():
    assert g.foreign_letters("show me my tasks for today") == []
    assert not g.is_suspicious("show me my tasks for today")


@case("TC_LANG_MIXED_RU_EN", "lang_guard",
      "legit Russian+English mix (definition of done) → not suspicious")
def test_mixed_ru_en():
    txt = "в карточке дописали definition of done, напиши комментарий"
    assert g.foreign_letters(txt) == []
    assert not g.is_suspicious(txt)


@case("TC_LANG_SERBIAN", "lang_guard",
      "Serbian drift 'Кажи моји задатак' → suspicious (ј)")
def test_serbian():
    assert "ј" in g.foreign_letters("Кажи моји задатак")
    assert g.is_suspicious("Кажи моји задатак")


@case("TC_LANG_SERBIAN_CHAR2", "lang_guard",
      "'Покажима и задаћан' → suspicious (ћ)")
def test_serbian2():
    assert g.is_suspicious("Покажима и задаћан")


@case("TC_LANG_BELARUSIAN_LATIN_MIX", "lang_guard",
      "'Tegni дашоў в этой задачы' → suspicious (ў); triggers batch that also fixes 'Tegni'")
def test_belarusian_mix():
    txt = "Tegni дашоў в этой задачы и напиши вопрос"
    foreign = g.foreign_letters(txt)
    assert "ў" in foreign
    assert g.is_suspicious(txt)


@case("TC_LANG_LATIN_HACEK", "lang_guard",
      "Latin-with-háček 'Duša' → suspicious (š)")
def test_latin_hacek():
    assert g.is_suspicious("Duša после смерти")


@case("TC_LANG_UKRAINIAN", "lang_guard",
      "Ukrainian letters і ї є ґ → suspicious")
def test_ukrainian():
    assert g.is_suspicious("Привіт, як справи")   # і is Ukrainian, not Russian


@case("TC_LANG_DIGITS_PUNCT_OK", "lang_guard",
      "digits, punctuation, emoji don't count as foreign letters")
def test_digits_punct():
    assert g.foreign_letters("Задача №5: оплатить 1000 руб. 👍") == []
    assert not g.is_suspicious("Задача №5: оплатить 1000 руб. 👍")


@case("TC_LANG_YO_OK", "lang_guard", "Russian ё/Ё is allowed, not foreign")
def test_yo():
    assert g.foreign_letters("Ещё раз всё проверь") == []
    assert not g.is_suspicious("Ещё раз всё проверь")


if __name__ == "__main__":
    run_all("test_lang_guard")
