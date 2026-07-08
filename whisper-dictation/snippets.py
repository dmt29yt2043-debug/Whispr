"""Voice snippets — a spoken trigger phrase expands into a text template.

Wispr Flow's "Snippets" feature: say "моя подпись" and the full email
signature is pasted instead of the literal words.

Configuration lives in settings.json under "snippets":

    "snippets": {
        "моя подпись": "С уважением,\nМаксим Снигирев\n+7 ...",
        "ссылка на календарь": "https://cal.com/maxim"
    }

Matching: the ENTIRE dictation (after transcription cleanup) must equal
the trigger phrase, ignoring case, surrounding whitespace and trailing
punctuation. Full-match only — a trigger inside a longer sentence is
left alone, so normal dictations can't accidentally explode into a
template.
"""

import logging
import re
from typing import Optional

import settings as S

log = logging.getLogger(__name__)

# Strip trailing punctuation the transcriber tends to append, and any
# leading/trailing whitespace. Internal punctuation stays significant.
_EDGE_PUNCT_RE = re.compile(r"^[\s.,!?…:;\"'«»]+|[\s.,!?…:;\"'«»]+$")


def _normalize(text: str) -> str:
    return _EDGE_PUNCT_RE.sub("", (text or "")).lower()


def expand(text: str) -> Optional[str]:
    """Return the snippet template if `text` is exactly a trigger, else None."""
    snippets = S.get("snippets", {}) or {}
    if not snippets:
        return None
    needle = _normalize(text)
    if not needle:
        return None
    for trigger, template in snippets.items():
        if _normalize(trigger) == needle:
            log.info("Snippet triggered: %r → %d chars", trigger, len(template))
            return template
    return None
