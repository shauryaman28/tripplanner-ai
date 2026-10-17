"""What text the API takes, and how much of it (Phase 35).

Everything a traveller types ends up somewhere that reads it: a destination and interests go into
the prompts the models are given and into the providers' searches; a request in their own words is
the planner's instruction. SQL is never a risk — every statement is parameterised — and what a
model writes is parsed and checked, never run. What can be kept out is what has no business in a
trip and is how text is usually smuggled into a prompt or a log:

    control characters        a line break in a destination starts a new line of the prompt; a
                              carriage return or a terminal escape rewrites a log line
    format characters         zero-width spaces and joiners, bidirectional overrides (Cf): text that
                              reads one way and is another
    private-use, surrogates,  nothing a person types
    unassigned code points

Three kinds of field:

    a place or a few words    (destination)  letters and marks of any script, digits, spaces and a
                              little punctuation — no braces, brackets, quotes, backslashes, `<` `>`
    an interest, a name       Phase 25's rule (group.is_words): shorter, simpler still
    a traveller's own words   (a request, an answer, a change)  anything a person writes, line breaks
                              included — but none of the characters above

Each has a length a person needs and not much more: what was asked for, not what can be sent.
"""

from __future__ import annotations

import unicodedata
from typing import Annotated

from pydantic import AfterValidator, Field, StringConstraints

PLACE_CHARS = 200  # "a relaxed week somewhere in Kerala, near the backwaters" with room to spare
FREE_TEXT_CHARS = 2000  # a request in the traveller's own words — a few paragraphs
SHORT_TEXT_CHARS = 1000  # an answer to a question, a change to a plan
EMAIL_CHARS = 254  # RFC 5321's longest address
PASSWORD_MIN_CHARS = 8  # NIST SP 800-63B's minimum for a password a person chose
PASSWORD_MAX_BYTES = 72  # bcrypt reads no further: a longer password would be checked by its first 72 bytes
TOKEN_CHARS = 2048  # a login token in an address or a header — ours are under 300

_PLACE_PUNCTUATION = frozenset(" .,'’&()/+-:;!?")
_NEVER = frozenset({"Cc", "Cf", "Co", "Cs", "Cn"})


def _category(char: str) -> str:
    return unicodedata.category(char)


def is_place(text: str) -> bool:
    """A destination: letters, marks, digits, spaces, dashes and a little punctuation; starting with a letter or digit."""
    if not text or not text[0].isalnum():
        return False
    return all(char in _PLACE_PUNCTUATION or _category(char)[0] in "LMN" or _category(char) == "Pd" for char in text)


def is_free_text(text: str) -> bool:
    """A traveller's own words: anything but control characters (a line break is fine), format characters,
    private-use and unassigned code points."""
    return all(char in "\n\t" or _category(char) not in _NEVER for char in text)


def _place(text: str) -> str:
    if not is_place(text):
        raise ValueError(
            "a destination is letters, digits, spaces and simple punctuation (. , ' & ( ) / + - : ; ! ?) — "
            "no line breaks, quotes, brackets or braces"
        )
    return text


def _free(text: str) -> str:
    if not is_free_text(text):
        raise ValueError("this text contains a control or formatting character (zero-width, a direction override, …)")
    return text


def _password(text: str) -> str:
    if len(text) < PASSWORD_MIN_CHARS:
        raise ValueError(f"a password is at least {PASSWORD_MIN_CHARS} characters")
    if len(text.encode()) > PASSWORD_MAX_BYTES:
        raise ValueError(f"a password is at most {PASSWORD_MAX_BYTES} bytes (that many letters of the Latin alphabet)")
    return text


Place = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=PLACE_CHARS), AfterValidator(_place)
]
FreeText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=FREE_TEXT_CHARS), AfterValidator(_free)
]
ShortText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=SHORT_TEXT_CHARS), AfterValidator(_free)
]
NewPassword = Annotated[str, Field(max_length=PASSWORD_MAX_BYTES), AfterValidator(_password)]
