"""Tests for the filter type guards.

A type guard must answer yes or no for any input, never raise -- callers use them to decide
whether a value has a shape, often on untrusted payloads. Each guard is therefore tested
against its documented shape, its near-misses, and inputs of the wrong type entirely.
"""

import datetime
from decimal import Decimal
from enum import Enum

import pytest

from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.filters import (
    is_choice_exact_filter_lookup,
    is_choice_in_filter_lookup,
    is_date_filter_lookup,
    is_field_filter,
    is_integer_filter_lookup,
    is_numeric_filter_lookup,
    is_string_filter_lookup,
    is_string_membership_exact_lookup,
    is_string_membership_in_lookup,
)


NOW = datetime.datetime(2026, 1, 1, 12, 0, 0)


class OtherEnum(Enum):
    SOMETHING = "something"


# ----------------------------------------------------------------------
# is_field_filter
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "filters",
    [{}, {"key": "welcome"}, {"name": "n", "version": 1}, {"status": ManagedTemplateStatus.DRAFT}],
)
def test_field_filters_are_recognised(filters):
    assert is_field_filter(filters) is True


@pytest.mark.parametrize(
    "filters",
    [
        {"and": [{"key": "a"}]},
        {"or": [{"key": "a"}]},
        {"not": {"key": "a"}},
        {"and": [], "key": "a"},
    ],
)
def test_logical_groups_are_not_field_filters(filters):
    assert is_field_filter(filters) is False


# ----------------------------------------------------------------------
# is_string_filter_lookup
# ----------------------------------------------------------------------


@pytest.mark.parametrize("lookup", ["exact", "starts_with", "ends_with", "includes"])
def test_every_string_lookup_is_accepted(lookup):
    assert is_string_filter_lookup({"lookup": lookup, "value": "x", "case_sensitive": True})


def test_case_insensitive_string_lookup_is_accepted():
    assert is_string_filter_lookup({"lookup": "exact", "value": "x", "case_sensitive": False})


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("plain string", "not a dict"),
        ({"value": "x", "case_sensitive": True}, "no lookup"),
        ({"lookup": "exact", "case_sensitive": True}, "no value"),
        ({"lookup": "exact", "value": "x"}, "no case_sensitive"),
        ({"lookup": "exact", "value": 1, "case_sensitive": True}, "non-string value"),
        ({"lookup": "matches", "value": "x", "case_sensitive": True}, "unknown lookup"),
        ({"lookup": 1, "value": "x", "case_sensitive": True}, "non-string lookup"),
        ({"lookup": "exact", "value": "x", "case_sensitive": "yes"}, "non-bool flag"),
    ],
)
def test_non_string_lookups_are_rejected(value, why):
    assert is_string_filter_lookup(value) is False, why


# ----------------------------------------------------------------------
# is_date_filter_lookup
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", [{"from": NOW}, {"to": NOW}, {"from": NOW, "to": NOW}, {"from": NOW, "to": "junk"}]
)
def test_date_ranges_with_at_least_one_bound_are_accepted(value):
    assert is_date_filter_lookup(value) is True


@pytest.mark.parametrize(
    "value", ["nope", {}, {"from": "2026-01-01"}, {"to": None}, {"other": NOW}]
)
def test_non_date_ranges_are_rejected(value):
    assert is_date_filter_lookup(value) is False


def test_a_date_is_not_a_datetime_bound():
    assert is_date_filter_lookup({"from": datetime.date(2026, 1, 1)}) is False


# ----------------------------------------------------------------------
# is_numeric_filter_lookup / is_integer_filter_lookup
# ----------------------------------------------------------------------


@pytest.mark.parametrize("lookup", ["gt", "gte", "lt", "lte"])
def test_every_numeric_lookup_is_accepted(lookup):
    assert is_numeric_filter_lookup({"lookup": lookup, "value": 1})


@pytest.mark.parametrize("value", [1, 1.5, Decimal("1.5")])
def test_numeric_lookup_accepts_int_float_and_decimal(value):
    assert is_numeric_filter_lookup({"lookup": "gt", "value": value})


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("nope", "not a dict"),
        ({"value": 1}, "no lookup"),
        ({"lookup": "gt"}, "no value"),
        ({"lookup": "gt", "value": "1"}, "non-numeric value"),
        ({"lookup": "exact", "value": 1}, "not a numeric lookup"),
        ({"lookup": 1, "value": 1}, "non-string lookup"),
    ],
)
def test_non_numeric_lookups_are_rejected(value, why):
    assert is_numeric_filter_lookup(value) is False, why


def test_integer_lookup_accepts_an_int():
    assert is_integer_filter_lookup({"lookup": "lte", "value": 3})


@pytest.mark.parametrize("value", [1.5, Decimal("1.5"), "3"])
def test_integer_lookup_rejects_non_integers(value):
    assert is_integer_filter_lookup({"lookup": "gt", "value": value}) is False


def test_integer_lookup_treats_a_bool_as_an_int():
    # `bool` subclasses `int`, so `isinstance(True, int)` holds. Documented rather than
    # asserted as desirable -- a bool version number is meaningless.
    assert is_integer_filter_lookup({"lookup": "gt", "value": True}) is True


@pytest.mark.parametrize(
    "value", ["nope", {"value": 1}, {"lookup": "gt"}, {"lookup": "in", "value": 1}]
)
def test_malformed_integer_lookups_are_rejected(value):
    assert is_integer_filter_lookup(value) is False


# ----------------------------------------------------------------------
# is_string_membership_exact_lookup
# ----------------------------------------------------------------------


def test_string_membership_exact_accepts_a_string_value():
    assert is_string_membership_exact_lookup({"lookup": "exact", "value": "a"})


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("nope", "not a dict"),
        ({"lookup": "in", "value": "a"}, "wrong lookup"),
        ({"lookup": "exact", "value": ["a"]}, "list value"),
        ({"lookup": "exact"}, "no value"),
        ({"value": "a"}, "no lookup"),
    ],
)
def test_malformed_string_membership_exact_lookups_are_rejected(value, why):
    assert is_string_membership_exact_lookup(value) is False, why


# ----------------------------------------------------------------------
# is_string_membership_in_lookup
# ----------------------------------------------------------------------


def test_string_membership_in_accepts_a_list_of_strings():
    assert is_string_membership_in_lookup({"lookup": "in", "value": ["a", "b"]}) is True


@pytest.mark.parametrize("collection", [["a"], ("a",), {"a"}])
def test_string_membership_in_accepts_any_collection_type(collection):
    assert is_string_membership_in_lookup({"lookup": "in", "value": collection}) is True


def test_string_membership_in_accepts_an_empty_collection():
    assert is_string_membership_in_lookup({"lookup": "in", "value": []}) is True


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("nope", "not a dict"),
        ({"lookup": "exact", "value": ["a"]}, "wrong lookup"),
        ({"lookup": "in", "value": "a"}, "non-collection value"),
        ({"lookup": "in"}, "no value"),
        ({"value": ["a"]}, "no lookup"),
        ({"lookup": "in", "value": ["a", 1]}, "a non-string element"),
        ({"lookup": "in", "value": [{"value": "a"}]}, "dict elements"),
    ],
)
def test_malformed_string_membership_in_lookups_are_rejected(value, why):
    assert is_string_membership_in_lookup(value) is False, why


# ----------------------------------------------------------------------
# is_choice_in_filter_lookup
# ----------------------------------------------------------------------


def test_choice_in_accepts_a_list_of_enum_members():
    value = {"lookup": "in", "value": [ManagedTemplateStatus.DRAFT, ManagedTemplateStatus.ACTIVE]}

    assert is_choice_in_filter_lookup(value) is True
    assert is_choice_in_filter_lookup(value, ManagedTemplateStatus) is True


def test_choice_in_without_a_class_accepts_any_enum():
    assert is_choice_in_filter_lookup({"lookup": "in", "value": [OtherEnum.SOMETHING]}) is True


def test_choice_in_rejects_members_of_the_wrong_enum():
    value = {"lookup": "in", "value": [OtherEnum.SOMETHING]}

    assert is_choice_in_filter_lookup(value, ManagedTemplateStatus) is False


def test_choice_in_rejects_a_mixed_collection():
    value = {"lookup": "in", "value": [ManagedTemplateStatus.DRAFT, OtherEnum.SOMETHING]}

    assert is_choice_in_filter_lookup(value, ManagedTemplateStatus) is False


def test_choice_in_accepts_an_empty_collection():
    assert is_choice_in_filter_lookup({"lookup": "in", "value": []}) is True


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("nope", "not a dict"),
        ({"lookup": "exact", "value": []}, "wrong lookup"),
        ({"lookup": "in", "value": "a"}, "non-collection value"),
        ({"lookup": "in"}, "no value"),
        ({"value": []}, "no lookup"),
        ({"lookup": "in", "value": ["draft"]}, "raw strings, not members"),
        ({"lookup": "in", "value": [{"value": ManagedTemplateStatus.DRAFT}]}, "dict elements"),
    ],
)
def test_malformed_choice_in_lookups_are_rejected(value, why):
    assert is_choice_in_filter_lookup(value) is False, why


# ----------------------------------------------------------------------
# is_choice_exact_filter_lookup
# ----------------------------------------------------------------------


def test_choice_exact_accepts_a_matching_enum_member():
    value = {"lookup": "exact", "value": ManagedTemplateStatus.DRAFT}

    assert is_choice_exact_filter_lookup(value, ManagedTemplateStatus) is True
    assert is_choice_exact_filter_lookup(value) is True


def test_choice_exact_without_a_class_accepts_any_enum():
    assert is_choice_exact_filter_lookup({"lookup": "exact", "value": OtherEnum.SOMETHING}) is True


def test_choice_exact_rejects_a_member_of_the_wrong_enum():
    value = {"lookup": "exact", "value": OtherEnum.SOMETHING}

    assert is_choice_exact_filter_lookup(value, ManagedTemplateStatus) is False


@pytest.mark.parametrize("enum_cls", [None, ManagedTemplateStatus])
@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("nope", "not a dict"),
        (["nope"], "a list, not a dict"),
        ({"lookup": "in", "value": ManagedTemplateStatus.DRAFT}, "wrong lookup"),
        ({"lookup": "exact"}, "no value"),
        ({"value": ManagedTemplateStatus.DRAFT}, "no lookup"),
        ({"lookup": "exact", "value": "draft"}, "a raw string, not a member"),
        ({"lookup": "exact", "value": [ManagedTemplateStatus.DRAFT]}, "a list of members"),
    ],
)
def test_malformed_choice_exact_lookups_are_rejected(value, why, enum_cls):
    # Every rejection must hold whether or not an enum class is supplied -- the two arms of
    # the check share the dict and lookup validation.
    assert is_choice_exact_filter_lookup(value, enum_cls) is False, why


@pytest.mark.parametrize(
    "guard_input",
    ["nope", 42, None, ["a"], {"lookup": "exact"}, {"lookup": "in", "value": [1]}],
)
def test_no_guard_ever_raises(guard_input):
    """A type guard answers yes or no; it must not raise on unexpected input."""
    for guard in (
        is_string_membership_in_lookup,
        is_string_membership_exact_lookup,
        is_choice_in_filter_lookup,
        is_choice_exact_filter_lookup,
        is_string_filter_lookup,
        is_numeric_filter_lookup,
        is_integer_filter_lookup,
        is_date_filter_lookup,
    ):
        assert guard(guard_input) in (True, False)
