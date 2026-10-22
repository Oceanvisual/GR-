"""Is the performer the model named mentioned in its quote? (R-11, FR-025)

Russian declines nouns and adjectives, so words are compared by their stems: the first few
characters, a shorter word losing its last letter. «должностное лицо, ответственное за прием»
is found in «должностным лицом, ответственным за прием». Prepositions and conjunctions do not
count; short abbreviations («МФЦ») must appear as whole words. A heuristic without a
morphological analyser: its quality is measured by eval.
"""

import re
import tomllib
from dataclasses import dataclass
from functools import cache
from importlib import resources

from gr.grounding.normalize import normalize_for_match

_WORD_RE = re.compile(r"[а-яa-z0-9]+")
_FUNCTION_WORDS = frozenset(
    (
        "и", "в", "во", "за", "по", "на", "с", "со", "к", "ко", "о", "об", "от", "до", "из",
        "для", "при", "или", "либо", "а", "также", "не", "его", "ее", "их",
    )
)  # fmt: skip


@dataclass(frozen=True)
class RoleConfig:
    stem_chars: int = 5
    min_share: float = 0.75


@cache
def load_role_config() -> RoleConfig:
    raw = tomllib.loads(resources.files("gr.config").joinpath("grounding.toml").read_text("utf-8"))
    return RoleConfig(**raw["role"])


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(normalize_for_match(text))


def _stem(word: str, cfg: RoleConfig) -> str:
    return word[: max(3, min(cfg.stem_chars, len(word) - 1))]


def _found(word: str, quote_words: list[str], cfg: RoleConfig) -> bool:
    if len(word) < 4:  # an abbreviation or a number: the whole word
        return word in quote_words
    stem = _stem(word, cfg)
    # No fuzzy stems: one letter apart, «заяви(тель)» and «заявл(ение)» are different roles.
    return any(candidate.startswith(stem) for candidate in quote_words)


def role_score(role: str, quote: str, config: RoleConfig | None = None) -> float:
    """Share of the role's meaningful words found in the quote, 0–1."""
    cfg = config or load_role_config()
    role_words = [w for w in _words(role) if w not in _FUNCTION_WORDS]
    if not role_words:
        return 0.0
    quote_words = _words(quote)
    return sum(_found(w, quote_words, cfg) for w in role_words) / len(role_words)


def role_in_quote(role: str, quote: str, config: RoleConfig | None = None) -> bool:
    cfg = config or load_role_config()
    return role_score(role, quote, cfg) >= cfg.min_share
