"""Turning free text into a tag slug, and keeping that slug unique.

Tags are typed by humans ("Black Friday", "black friday ", "Black-Friday") and searched by
machines, so every tag carries a normalized ``slug`` alongside the text it was written as.
The slug is the identity: it is what a filter matches on, what a URL carries, and what a
store enforces uniqueness over.

Slugging lives here rather than in a backend so every implementation of the storage seam
produces the same slug for the same text. A Django backend using ``django.utils.text.slugify``
and a SQLAlchemy one rolling its own would disagree on accents and on non-Latin scripts, and
the two stores would then hold different identities for the same tag.

The rules, in order:

1. Unicode-normalize (NFKD) and fold accents away, so ``Promoção`` and ``Promocao`` are the
   same tag.
2. Lowercase, then replace every run of non-alphanumeric characters with a single ``-``.
3. Trim leading and trailing ``-``.

Text that is entirely non-Latin (``日本語``) folds to nothing under step 1, so it falls back to
a Unicode-preserving pass that keeps alphanumeric characters as they are. Losing a whole tag to
transliteration would be worse than a slug that is not URL-clean.
"""

import re
import unicodedata
from collections.abc import Callable


# Longest slug this module will produce. Chosen to fit the 255-char columns backends use for
# it, leaving room for the ``-2`` / ``-3`` suffix uniqueness may need to append.
MAX_SLUG_LENGTH = 240

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


def normalize_tag_text(text: str) -> str:
    """Collapse a tag's whitespace and trim it, leaving the caller's casing intact.

    The text is what a UI displays, so this is deliberately gentle -- it fixes the artifacts
    of typing (a trailing space, a double space) and nothing else. ``slugify_tag`` handles the
    rest.

    param text: str
    return: str
    """
    return " ".join(text.split())


def slugify_tag(text: str) -> str:
    """Normalize free text into a tag slug. Returns ``""`` for text with nothing sluggable.

    An empty result is returned rather than raised on so callers can decide what it means:
    the service rejects it, while a filter treats it as a tag that matches nothing.

    param text: str
    return: str
    """
    folded = unicodedata.normalize("NFKD", text)
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    slug = _NON_ALPHANUMERIC.sub("-", ascii_only.lower()).strip("-")

    if not slug:
        slug = _slugify_unicode(text)

    return slug[:MAX_SLUG_LENGTH].strip("-")


def _slugify_unicode(text: str) -> str:
    """Slug for text ASCII folding empties out -- ``日本語``, ``Привет`` and the like.

    Alphanumeric characters survive as they are; everything else becomes a separator. Not
    URL-clean without percent-encoding, but a tag that exists beats a tag that vanished.
    """
    characters = [character if character.isalnum() else "-" for character in text.lower()]
    return re.sub(r"-+", "-", "".join(characters)).strip("-")


def next_available_slug(slug: str, is_taken: Callable[[str], bool]) -> str:
    """Return ``slug``, or the first ``slug-N`` that ``is_taken`` says is free.

    Backends call this while holding whatever lock they use for tag writes: ``is_taken`` is a
    read, so two concurrent creates can both be told the same slug is free. The unique
    constraint on the column is what actually decides, and this only keeps the common case
    from hitting it.

    param slug: str -- an already-slugified base.
    param is_taken: Callable[[str], bool] -- True when a tag with that slug already exists.
    return: str
    """
    if not is_taken(slug):
        return slug

    # Truncate the base first so appending the suffix cannot push the result over the limit.
    suffix_index = 2
    while True:
        suffix = f"-{suffix_index}"
        base = slug[: MAX_SLUG_LENGTH - len(suffix)].strip("-")
        candidate = f"{base}{suffix}"
        if not is_taken(candidate):
            return candidate
        suffix_index += 1
