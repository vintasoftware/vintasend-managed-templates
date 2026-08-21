"""Slug normalization and uniqueness.

Slugging lives in the library rather than in a backend so every store agrees on what a given
text is called. These tests pin that agreement: what folds together, what stays apart, and
what happens to text with nothing sluggable in it.
"""

import pytest

from vintasend_managed_templates.tags import (
    MAX_SLUG_LENGTH,
    next_available_slug,
    normalize_tag_text,
    slugify_tag,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Black Friday", "black-friday"),
        ("black friday", "black-friday"),
        ("  Black   Friday  ", "black-friday"),
        ("Black-Friday", "black-friday"),
        ("Black_Friday", "black-friday"),
        ("BLACK FRIDAY!!!", "black-friday"),
        ("Promoção", "promocao"),
        ("Ação de Graças", "acao-de-gracas"),
        ("São Paulo", "sao-paulo"),
        ("---leading and trailing---", "leading-and-trailing"),
        ("v2.0 release", "v2-0-release"),
        ("50% off", "50-off"),
    ],
)
def test_slugify_normalizes_text_into_a_slug(text, expected):
    assert slugify_tag(text) == expected


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Black Friday", "black friday"),
        ("Promoção", "Promocao"),
        ("Black_Friday", "black friday"),
    ],
)
def test_texts_that_differ_only_in_case_accents_or_separators_share_a_slug(first, second):
    assert slugify_tag(first) == slugify_tag(second)


def test_distinct_words_do_not_collapse_onto_one_slug():
    assert slugify_tag("welcome") != slugify_tag("welcome back")


def test_a_separator_is_meaningful_rather_than_ignored():
    """``onboarding`` and ``on boarding`` are two tags, not one written two ways."""
    assert slugify_tag("Onboarding") != slugify_tag("on boarding")


@pytest.mark.parametrize("text", ["", "   ", "!!!", "---", "@#$%"])
def test_text_with_nothing_sluggable_slugs_to_empty(text):
    """Returned rather than raised on: the service rejects it, a filter treats it as no match."""
    assert slugify_tag(text) == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("日本語", "日本語"),
        ("Привет мир", "привет-мир"),
        ("한국어 태그", "한국어-태그"),
    ],
)
def test_non_latin_text_keeps_its_characters_rather_than_vanishing(text, expected):
    """ASCII folding empties these out, so the fallback pass keeps the tag alive."""
    assert slugify_tag(text) == expected


def test_a_slug_is_capped_at_the_column_length():
    slug = slugify_tag("word " * 200)

    assert len(slug) <= MAX_SLUG_LENGTH
    assert not slug.endswith("-")


def test_normalize_collapses_whitespace_but_keeps_the_authors_casing():
    assert normalize_tag_text("  Black   Friday \n") == "Black Friday"


def test_an_untaken_slug_is_returned_unchanged():
    assert next_available_slug("welcome", lambda slug: False) == "welcome"


def test_a_taken_slug_gets_the_first_free_numeric_suffix():
    taken = {"welcome", "welcome-2", "welcome-3"}

    assert next_available_slug("welcome", lambda slug: slug in taken) == "welcome-4"


def test_a_suffixed_slug_still_fits_the_column():
    base = "a" * MAX_SLUG_LENGTH
    taken = {base}

    result = next_available_slug(base, lambda slug: slug in taken)

    assert len(result) <= MAX_SLUG_LENGTH
    assert result.endswith("-2")
