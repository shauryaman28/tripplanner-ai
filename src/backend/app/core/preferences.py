"""Pure helpers for normalising and merging user preferences — Phase 16.

Kept in core/ (like security.py) so it has zero FastAPI / SQLAlchemy
dependencies: the Pydantic schemas, the PUT route and the AI layer's
PreferenceExtractor all share one definition of "canonical form" and
of "additive merge" instead of each re-implementing them.
"""

import re
from collections.abc import Iterable

MAX_LIST_ITEMS = 20
MAX_ITEM_LENGTH = 50

_AIRLINE_CODE_RE = re.compile(r"[A-Z0-9]{2}")


def is_valid_airline_code(code: str) -> bool:
    """True for a 2-character IATA carrier code such as '6E' or 'AI'."""
    return _AIRLINE_CODE_RE.fullmatch(code.strip().upper()) is not None


def _clean(items: object) -> list[str]:
    """Strip and drop blanks. Non-list input (e.g. malformed LLM output) → []."""
    if not isinstance(items, (list, tuple, set)):
        return []
    return [s for s in (str(item).strip() for item in items) if s]


def normalise_dietary(items: object) -> list[str]:
    """Lower-case, de-duplicate (order-preserving) and length-cap dietary labels."""
    return list(dict.fromkeys(s.lower()[:MAX_ITEM_LENGTH] for s in _clean(items)))


def normalise_airlines(items: object) -> list[str]:
    """Upper-case, de-duplicate and drop anything that is not a 2-char IATA code."""
    return list(dict.fromkeys(s.upper() for s in _clean(items) if is_valid_airline_code(s)))


def merge_unique(existing: Iterable[str], new: Iterable[str], limit: int | None = None) -> list[str]:
    """Return *existing* followed by any items of *new* not already present.

    Comparison is case-insensitive; the first spelling wins. Existing items
    are never dropped or reordered — the merge is strictly additive. *limit*
    only stops new items from being appended, it never truncates *existing*.
    """
    merged = list(existing)
    seen = {item.casefold() for item in merged}
    for item in new:
        if limit is not None and len(merged) >= limit:
            break
        key = item.casefold()
        if key not in seen:
            merged.append(item)
            seen.add(key)
    return merged
