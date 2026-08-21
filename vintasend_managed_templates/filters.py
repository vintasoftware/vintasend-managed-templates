import datetime
import sys
from decimal import Decimal
from enum import Enum
from typing import Generic, Literal, TypeAlias, TypeGuard, TypeVar


# Generic ``TypedDict`` (``class Foo(TypedDict, Generic[T])``) only became legal in 3.11;
# on 3.10 ``typing.TypedDict`` raises ``TypeError: cannot inherit from both a TypedDict type
# and a non-TypedDict base class`` at import time. ``typing_extensions`` is a hard runtime
# dependency of vintasend, so the fallback is always importable. Use a ``sys.version_info``
# guard rather than try/except ImportError so mypy evaluates the branch statically.
if sys.version_info >= (3, 11):
    from typing import TypedDict
else:
    from typing_extensions import TypedDict

from .constants import ManagedTemplateStatus


# ``from`` is a Python keyword, so the wire key can only be expressed with the functional
# ``TypedDict`` syntax. The key stays ``from`` for cross-language parity with the TS sibling.
DateRange = TypedDict(
    "DateRange", {"from": datetime.datetime, "to": datetime.datetime}, total=False
)


class StringFilterLookup(TypedDict):
    # ``lookup`` and ``value`` are required in practice; ``total=False`` only because a JSON
    # payload might omit them and we validate at evaluation time rather than at typing time.
    lookup: Literal["exact", "starts_with", "ends_with", "includes"]
    value: str
    case_sensitive: bool


StringFieldFilter: TypeAlias = str | StringFilterLookup


class NumericFilterLookup(TypedDict):
    lookup: Literal["gt", "gte", "lt", "lte"]
    value: int


IntegerFieldFilter: TypeAlias = int | NumericFilterLookup
FloatFieldFilter: TypeAlias = float | NumericFilterLookup
DecimalFieldFilter: TypeAlias = Decimal | NumericFilterLookup


class StringMembershipLookupInFilter(TypedDict):
    lookup: Literal["in"]
    value: list[str] | tuple[str] | set[str]


class StringMembershipLookupExactFilter(TypedDict):
    lookup: Literal["exact"]
    value: list[str] | tuple[str] | set[str]


StringMembershipFilter: TypeAlias = (
    str | StringMembershipLookupInFilter | StringMembershipLookupExactFilter
)


# A tag filter is a plain collection of tag slugs -- no lookup wrapper, because which of the
# two matches is meant is already said by the field name (``includes_all_tags`` /
# ``includes_any_of_tags``) rather than by a ``lookup`` key.
#
# Values are matched against ``ManagedTemplateTag.slug``. Backends slugify what they are given
# first, so a caller may pass either the slug or the text it came from and get the same result.
TagsFieldFilter: TypeAlias = list[str] | tuple[str, ...] | set[str]


# A flag field takes the bare boolean and nothing else: there is no lookup to choose, since
# the only two questions it can answer are "yes" and "no".
BooleanFieldFilter: TypeAlias = bool


ChoiceType = TypeVar("ChoiceType", bound=Enum)


class ChoiceLookupInFilter(TypedDict, Generic[ChoiceType]):
    lookup: Literal["in"]
    value: list[ChoiceType] | tuple[ChoiceType] | set[ChoiceType]


class ChoiceLookupExactFilter(TypedDict, Generic[ChoiceType]):
    lookup: Literal["exact"]
    value: ChoiceType


ManagedTemplateStatusFilter: TypeAlias = (
    ManagedTemplateStatus
    | ChoiceLookupExactFilter[ManagedTemplateStatus]
    | ChoiceLookupInFilter[ManagedTemplateStatus]
)


ManagedTemplateFilterOrderByField = Literal["created_at", "updated_at"]
ManagedTemplateFilterOrderDirection = Literal["asc", "desc"]


class ManagedTemplateOrderBy(TypedDict):
    field: ManagedTemplateFilterOrderByField
    direction: ManagedTemplateFilterOrderDirection


class ManagedTemplateFilterFields(TypedDict, total=False):
    name: StringFieldFilter
    description: StringFieldFilter
    key: StringFieldFilter
    version: IntegerFieldFilter
    template_managed_backend: StringFieldFilter
    status: ManagedTemplateStatusFilter
    created_at_range: DateRange
    updated_at_range: DateRange
    # Tag membership. ``includes_all_tags`` matches a template carrying every listed tag,
    # ``includes_any_of_tags`` one carrying at least one of them. Both follow Python's own
    # ``all()`` / ``any()`` on an empty list: an empty ``includes_all_tags`` constrains
    # nothing, an empty ``includes_any_of_tags`` matches nothing.
    includes_all_tags: TagsFieldFilter
    includes_any_of_tags: TagsFieldFilter
    # Bases, or templates to send. ``True`` keeps only the templates that declare a
    # ``{% managed_children %}`` hole or blocks without extending anything; ``False`` keeps
    # only the ones that do not.
    #
    # Answered against the stored ``ManagedTemplate.is_abstract`` -- a column a backend writes
    # on every write -- rather than by parsing sources at query time, which is the whole
    # reason the flag is denormalized: a picker that has to exclude bases would otherwise read
    # and parse every row in the store to draw one page.
    is_abstract: BooleanFieldFilter
    # One row per key instead of one row per version. ``True`` keeps, for each key, only the
    # highest-numbered version whose status is in
    # ``MOST_RECENT_ACTIVE_VERSION_STATUSES`` (ACTIVE or DRAFT), and drops every key with no
    # such version. ``False`` is its exact complement -- every other row, retired keys
    # included -- so it is what ``{"not": {"most_recent_active_version": True}}`` means.
    #
    # This is the one field whose answer depends on the *other* rows in the store rather than
    # on the row being tested, so a backend evaluates it against the whole key, not the row.
    most_recent_active_version: BooleanFieldFilter


# ``and`` / ``or`` / ``not`` are Python keywords, so these single-key groups can only be
# expressed with the functional syntax. The wire keys stay ``and`` / ``or`` / ``not``.
AndFilter = TypedDict("AndFilter", {"and": list["ManagedTemplateFilter"]})
OrFilter = TypedDict("OrFilter", {"or": list["ManagedTemplateFilter"]})
NotFilter = TypedDict("NotFilter", {"not": "ManagedTemplateFilter"})


FilterLookup: TypeAlias = (
    StringFieldFilter
    | IntegerFieldFilter
    | ManagedTemplateStatusFilter
    | DateRange
    | TagsFieldFilter
    | BooleanFieldFilter
)

ManagedTemplateFilter: TypeAlias = ManagedTemplateFilterFields | AndFilter | OrFilter | NotFilter

_LOGICAL_KEYS = ("and", "or", "not")
_STRING_LOOKUPS = ("exact", "starts_with", "ends_with", "includes")
_NUMERIC_LOOKUPS = ("gt", "gte", "lt", "lte")


def is_field_filter(filter: "ManagedTemplateFilter") -> TypeGuard["ManagedTemplateFilterFields"]:  # noqa: A002
    """Return ``True`` if ``filter`` is a field filter rather than an ``and``/``or``/``not`` group."""
    return not any(key in filter for key in _LOGICAL_KEYS)


def is_string_filter_lookup(value: dict) -> TypeGuard["StringFilterLookup"]:
    """Return ``True`` if a string-field filter is a ``StringFilterLookup`` and not a bare ``str``."""
    return (
        isinstance(value, dict)
        and "lookup" in value
        and "case_sensitive" in value
        and "value" in value
        and isinstance(value["value"], str)
        and (isinstance(value["lookup"], str) and value.get("lookup") in _STRING_LOOKUPS)
        and isinstance(value.get("case_sensitive"), bool)
    )


def is_date_filter_lookup(value: dict) -> TypeGuard["DateRange"]:
    return isinstance(value, dict) and (
        ("from" in value and isinstance(value.get("from"), datetime.datetime))
        or ("to" in value and isinstance(value.get("to"), datetime.datetime))
    )


def is_numeric_filter_lookup(value: dict) -> TypeGuard["NumericFilterLookup"]:
    return (
        isinstance(value, dict)
        and "lookup" in value
        and "value" in value
        and isinstance(value["value"], int | float | Decimal)
        and (isinstance(value["lookup"], str) and value.get("lookup") in _NUMERIC_LOOKUPS)
    )


def is_integer_filter_lookup(value: dict) -> TypeGuard["NumericFilterLookup"]:
    return (
        isinstance(value, dict)
        and "lookup" in value
        and "value" in value
        and isinstance(value["value"], int)
        and (isinstance(value["lookup"], str) and value.get("lookup") in _NUMERIC_LOOKUPS)
    )


def is_string_membership_in_lookup(value: dict) -> TypeGuard[StringMembershipLookupInFilter]:
    return (
        isinstance(value, dict)
        and "lookup" in value
        and "value" in value
        and value.get("lookup") == "in"
        and isinstance(value["value"], (list, tuple, set))
        and all(isinstance(v, str) for v in value["value"])
    )


def is_string_membership_exact_lookup(value: dict) -> TypeGuard[StringMembershipLookupExactFilter]:
    return (
        isinstance(value, dict)
        and "lookup" in value
        and "value" in value
        and value.get("lookup") == "exact"
        and isinstance(value["value"], str)
    )


def is_tags_filter(value: object) -> TypeGuard["TagsFieldFilter"]:
    """Return ``True`` if a value is a collection of tag slugs.

    A bare ``str`` is deliberately rejected: ``{"includes_all_tags": "welcome"}`` would
    otherwise iterate character by character and silently ask for the tags ``w``, ``e``, ``l``.
    Pass a one-element list instead.
    """
    return isinstance(value, (list, tuple, set)) and all(isinstance(v, str) for v in value)


def is_choice_in_filter_lookup(
    value: dict, enum_cls: type[Enum] | None = None
) -> TypeGuard["ChoiceLookupInFilter[Enum]"]:
    return (
        isinstance(value, dict)
        and "lookup" in value
        and "value" in value
        and value.get("lookup") == "in"
        and isinstance(value["value"], (list, tuple, set))
        and all(isinstance(v, enum_cls or Enum) for v in value["value"])
    )


def is_choice_exact_filter_lookup(
    value: dict, enum_cls: type[Enum] | None = None
) -> TypeGuard["ChoiceLookupExactFilter[Enum]"]:
    return (
        isinstance(value, dict)
        and "lookup" in value
        and "value" in value
        and value.get("lookup") == "exact"
        and isinstance(value["value"], enum_cls or Enum)
    )
