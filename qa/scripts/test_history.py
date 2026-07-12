"""TC_HIST_* — dictation history storage."""
import os
import tempfile
from _harness import case, run_all

import history


def _fresh_db():
    history._DB_PATH = os.path.join(tempfile.mkdtemp(), "history.db")


@case("TC_HIST_ADD_AND_RECENT", "history",
      "added dictations come back newest-first with timestamps")
def test_add_and_recent():
    _fresh_db()
    history.add("первая диктовка", app_bundle="com.apple.Notes")
    history.add("вторая диктовка")
    history.add("третья диктовка")
    rows = history.get_recent(limit=10)
    texts = [r[2] for r in rows]
    assert texts == ["третья диктовка", "вторая диктовка", "первая диктовка"]
    assert all(r[1] for r in rows), "timestamps must be present"


@case("TC_HIST_EMPTY_IGNORED", "history", "empty/whitespace text is not stored")
def test_empty_ignored():
    _fresh_db()
    history.add("")
    history.add("   ")
    history.add(None)
    assert history.count() == 0


@case("TC_HIST_CAP", "history", "history is capped — oldest entries pruned")
def test_cap():
    _fresh_db()
    old_cap = history._MAX_ENTRIES
    history._MAX_ENTRIES = 20
    try:
        for i in range(30):
            history.add(f"запись {i}")
        assert history.count() == 20
        rows = history.get_recent(limit=50)
        assert rows[0][2] == "запись 29", "newest must survive"
        texts = {r[2] for r in rows}
        assert "запись 0" not in texts, "oldest must be pruned"
    finally:
        history._MAX_ENTRIES = old_cap


@case("TC_HIST_CLEAR", "history", "clear() wipes everything")
def test_clear():
    _fresh_db()
    history.add("что-то")
    assert history.count() == 1
    history.clear()
    assert history.count() == 0
    assert history.get_recent() == []


@case("TC_HIST_PERSISTS_ACROSS_CONNECTIONS", "history",
      "entries survive reopening the DB (app restart scenario)")
def test_persists():
    _fresh_db()
    history.add("выживу после рестарта")
    # Same path, fresh connections happen per-call already — just re-read
    rows = history.get_recent()
    assert rows[0][2] == "выживу после рестарта"


@case("TC_HIST_MENU_TITLE", "history",
      "menu_title collapses newlines and ellipsizes long text")
def test_menu_title():
    assert history.menu_title("короткий текст") == "короткий текст"
    long = "очень " * 30
    t = history.menu_title(long, max_chars=48)
    assert len(t) <= 48
    assert t.endswith("…")
    multi = "первая строка\nвторая строка"
    assert "\n" not in history.menu_title(multi)


if __name__ == "__main__":
    run_all("test_history")
