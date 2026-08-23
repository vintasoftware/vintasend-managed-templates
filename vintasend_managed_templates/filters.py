import datetime
import functools
import sys
from collections.abc import Callable, Iterable
from decimal import Decimal
from enum import Enum
from typing import Any, Generic, Literal, TypeAlias, TypeGuard, TypeVar, cast


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
from .dataclasses import ManagedTemplate


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


# The fields a template listing can be ordered by.
#
# Deliberately narrow: each one is a scalar the backend already stores per row, so a store can
# answer it from an index rather than with a computed sort. ``tags`` is absent because ordering
# by a many-to-many has no single value to compare, and ``most_recent_active_version`` because
# it is a filter rather than a field.
#
# Field names are snake_case here and camelCase in the capability keys that report them --
# ``created_at`` is asked for as ``orderBy.createdAt`` -- for the same reason the filter fields
# are: this is an in-process Python API, while a capability report goes on the wire.
ManagedTemplateFilterOrderByField = Literal[
    "key", "name", "version", "status", "created_at", "updated_at"
]
ManagedTemplateFilterOrderDirection = Literal["asc", "desc"]


class ManagedTemplateOrderBy(TypedDict):
    field: ManagedTemplateFilterOrderByField
    direction: ManagedTemplateFilterOrderDirection


# Every orderable field, in the order a report and an error message list them. A tuple rather
# than a set so ``get_supported_order_by_fields`` has one stable order to answer in.
MANAGED_TEMPLATE_ORDER_BY_FIELDS: tuple[ManagedTemplateFilterOrderByField, ...] = (
    "key",
    "name",
    "version",
    "status",
    "created_at",
    "updated_at",
)

# Orderable field -> the attribute of ``ManagedTemplate`` holding its value. Two of the six
# differ: the filter vocabulary says ``created_at`` / ``updated_at`` while the dataclass fields
# are ``created`` / ``updated``.
_ORDER_FIELD_TO_ATTR: dict[str, str] = {
    "key": "key",
    "name": "name",
    "version": "version",
    "status": "status",
    "created_at": "created",
    "updated_at": "updated",
}

# Orderable field -> the camelCase capability key reporting whether a backend can sort by it.
_ORDER_BY_CAPABILITY_KEYS: dict[str, str] = {
    "key": "orderBy.key",
    "name": "orderBy.name",
    "version": "orderBy.version",
    "status": "orderBy.status",
    "created_at": "orderBy.createdAt",
    "updated_at": "orderBy.updatedAt",
}


def order_by_capability_key(field: str) -> str:
    """The capability key reporting whether a backend can order by ``field``.

    Raises KeyError for a field that is not orderable, so a typo is a loud failure here
    rather than a capability lookup that misses and falls through to a default.
    """
    return _ORDER_BY_CAPABILITY_KEYS[field]


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

# The filter fields whose value is a string match rather than a token, a range or a flag --
# the only ones the ``stringLookups.*`` capabilities have anything to say about.
_STRING_LOOKUP_FIELDS = frozenset({"name", "description", "key", "template_managed_backend"})

# String lookup -> the camelCase suffix of the capability key reporting it.
_STRING_LOOKUP_CAPABILITY_SUFFIX: dict[str, str] = {
    "exact": "exact",
    "starts_with": "startsWith",
    "ends_with": "endsWith",
    "includes": "includes",
}


# The fields ``ManagedTemplateFilterFields`` accepts, in declaration order.
#
# Read off the TypedDict rather than hand-listed, so adding a field to the type cannot leave a
# filter the negotiation below silently drops for want of a name.
KNOWN_FILTER_FIELDS: tuple[str, ...] = tuple(ManagedTemplateFilterFields.__annotations__)

# Filter field -> the camelCase capability key guarding it. Spelled out rather than converted
# from the snake_case name, so the wire vocabulary is greppable and a rename cannot quietly
# produce a key no backend has ever declared.
_FIELD_CAPABILITY_KEYS: dict[str, str] = {
    "name": "fields.name",
    "description": "fields.description",
    "key": "fields.key",
    "version": "fields.version",
    "template_managed_backend": "fields.templateManagedBackend",
    "status": "fields.status",
    "created_at_range": "fields.createdAtRange",
    "updated_at_range": "fields.updatedAtRange",
    "includes_all_tags": "fields.includesAllTags",
    "includes_any_of_tags": "fields.includesAnyOfTags",
    "is_abstract": "fields.isAbstract",
    "most_recent_active_version": "fields.mostRecentActiveVersion",
}


def field_capability_key(field: str) -> str:
    """The capability key guarding the filter field ``field``.

    Raises KeyError for a field that is not part of the vocabulary, so a typo fails here
    rather than producing a lookup that misses and falls through to the supported default.
    """
    return _FIELD_CAPABILITY_KEYS[field]


# Which filters and orders a template-manager backend can honour.
#
# Follows the same rule as ``vintasend``'s notification capabilities: a backend declares only
# what it *cannot* do, and its report is merged OVER this default, so a filter field added in a
# later release does not force every backend to re-declare support for it. Keys are camelCase
# dotted, byte-identical to the TS sibling.
#
# The ``orderBy.*`` keys are the exception, and default to ``False``. They are new vocabulary
# rather than behaviour backends already have -- the same rule ``fields.readAtRange`` was
# introduced under on the notification side -- so a ``True`` default would have every backend
# that shipped before ordering existed claim an order it never applies.
#
# There is deliberately no ``pagination.oneIndexed`` key here. Unlike the notification seam,
# every template read goes through ``ManagedTemplateService``'s own ``page >= 1`` validation,
# so 1-indexing is this seam's convention throughout and there is nothing to negotiate.
DEFAULT_TEMPLATE_BACKEND_FILTER_CAPABILITIES: dict[str, bool] = {
    # Composition. A backend that can evaluate field filters but not assemble them into
    # ``and`` / ``or`` / ``not`` groups declines these. ``notNested`` is the narrower question
    # of whether ``not`` may wrap a *group* rather than a single field filter
    # (``{"not": {"or": [...]}}``): a query builder able to negate one predicate cannot always
    # negate a whole subtree.
    "logical.and": True,
    "logical.or": True,
    "logical.not": True,
    "logical.notNested": True,
    # One key per field of ``ManagedTemplateFilterFields``.
    "fields.name": True,
    "fields.description": True,
    "fields.key": True,
    "fields.version": True,
    "fields.templateManagedBackend": True,
    "fields.status": True,
    "fields.createdAtRange": True,
    "fields.updatedAtRange": True,
    # Tag membership. A backend that stores no tags -- or stores them but cannot query across
    # them -- declines these, and a caller drops the filter rather than failing the request.
    # They are separate keys because "every tag" and "at least one tag" are different queries:
    # the first needs a per-template count over the tags asked for, the second only membership.
    "fields.includesAllTags": True,
    "fields.includesAnyOfTags": True,
    # Bases versus templates to send, answered from the stored ``is_abstract`` flag the backend
    # derives on every write. A backend that keeps no such flag declines this.
    "fields.isAbstract": True,
    # Its own key because it is a different question from the rest: a backend answers it by
    # comparing a row against the other versions of its key, which a store that keeps no
    # version history cannot do.
    "fields.mostRecentActiveVersion": True,
    "stringLookups.exact": True,
    "stringLookups.startsWith": True,
    "stringLookups.endsWith": True,
    "stringLookups.includes": True,
    # These two are independent capabilities, not a flag and its negation:
    #
    # * ``caseSensitive: False`` -- everything is forced case-insensitive, which is what a
    #   store on a case-insensitive collation (MySQL's ``*_ci``) does. It cannot honour
    #   ``case_sensitive: True``, nor a bare ``str`` filter, which means the same thing.
    # * ``caseInsensitive: False`` -- only exact-case matching is available, e.g. a store with
    #   ``LIKE`` but no ``ILIKE`` and no way to fold case.
    #
    # Deriving either from the other inverts the answer for exactly the backends that had a
    # constraint worth reporting. Read the key you actually mean.
    "stringLookups.caseSensitive": True,
    "stringLookups.caseInsensitive": True,
    # Built from the vocabulary rather than written out, so an orderable field can never be
    # added without the key that reports it. All False -- see the note above.
    **{order_by_capability_key(field): False for field in MANAGED_TEMPLATE_ORDER_BY_FIELDS},
}


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


# --------------------------------------------------------------------------------------
# Capability negotiation
# --------------------------------------------------------------------------------------


def supports_capability(capabilities: dict[str, bool], key: str) -> bool:
    """Read one capability, defaulting to supported.

    A missing key means "supported": backends declare only what they *cannot* do, so a
    capability added in a later release does not force every backend to re-declare it. The
    ``orderBy.*`` keys are the exception and are absent from no report, because
    ``DEFAULT_TEMPLATE_BACKEND_FILTER_CAPABILITIES`` states them all as ``False`` -- read a
    merged report rather than a backend's raw one, or an unsortable backend reads as sortable.
    """
    return capabilities.get(key, True)


def _operand(filter: "ManagedTemplateFilter", key: str) -> Any:  # noqa: A002
    """Read one logical group's payload without indexing the union by a literal key.

    ``ManagedTemplateFilter`` is a union of four TypedDicts, and ``is_field_filter`` is a
    ``TypeGuard`` -- which narrows only on its True branch -- so mypy still sees the field
    TypedDict among the alternatives here and rejects ``filter["and"]`` on it. Reading through
    the runtime dict is what ``ManagedTemplateService.validate_filter`` already does, for the
    same reason.
    """
    return cast("dict[str, Any]", filter)[key]


def is_empty_filter(filter: "ManagedTemplateFilter") -> bool:  # noqa: A002
    """Return ``True`` for a filter that constrains nothing, whatever shape it arrived in."""
    if is_field_filter(filter):
        return not filter
    if "and" in filter:
        return all(is_empty_filter(inner) for inner in _operand(filter, "and"))
    if "or" in filter:
        return all(is_empty_filter(inner) for inner in _operand(filter, "or"))
    return is_empty_filter(_operand(filter, "not"))


def prune_unsupported_filters(
    filter: "ManagedTemplateFilter",  # noqa: A002
    capabilities: dict[str, bool],
) -> "ManagedTemplateFilter":
    """Drop the parts of a filter the backend has declared it cannot answer.

    This is the other half of the capability report: reporting a limitation is only useful
    if something acts on it. Every caller that builds a filter would otherwise have to walk
    the capability map itself, and they would reach slightly different conclusions.

    **Dropping only ever widens.** A pruned filter matches everything the original matched
    and possibly more, so a caller sees extra rows rather than missing ones -- a listing that
    could not collapse to one row per key shows every version, which is visible in the
    result. That is the whole reason filters are negotiated by dropping while *ordering* is
    negotiated by refusing: an ignored order leaves no trace in the rows at all.

    Returns an empty filter -- which constrains nothing -- when everything has been dropped.

    param filter: ManagedTemplateFilter
    param capabilities: dict[str, bool] -- a report already merged over
        ``DEFAULT_TEMPLATE_BACKEND_FILTER_CAPABILITIES``.
    return: ManagedTemplateFilter
    """

    def can(key: str) -> bool:
        return supports_capability(capabilities, key)

    if is_field_filter(filter):
        return _prune_fields(filter, can)

    if "and" in filter:
        if not can("logical.and"):
            return {}
        kept = [
            pruned
            for pruned in (
                prune_unsupported_filters(inner, capabilities) for inner in _operand(filter, "and")
            )
            if not is_empty_filter(pruned)
        ]
        if not kept:
            return {}
        # A one-element ``and`` is the element: fewer groups for a backend to translate.
        return kept[0] if len(kept) == 1 else {"and": kept}

    if "or" in filter:
        # An ``or`` cannot be partially dropped. Removing one branch of a disjunction
        # *narrows* the result -- the opposite of what dropping is allowed to do -- so the
        # whole group goes, and with it the constraint.
        if not can("logical.or"):
            return {}
        kept = [prune_unsupported_filters(inner, capabilities) for inner in _operand(filter, "or")]
        # If any branch pruned down to "everything", the disjunction is satisfied by every row.
        if any(is_empty_filter(inner) for inner in kept):
            return {}
        return {"or": kept}

    if not can("logical.not"):
        return {}
    inner = prune_unsupported_filters(_operand(filter, "not"), capabilities)
    # Negating "everything" is "nothing", which is a narrowing rather than a widening.
    if is_empty_filter(inner):
        return {}
    if not is_field_filter(inner) and not can("logical.notNested"):
        return {}
    return {"not": inner}


def _prune_fields(
    fields: "ManagedTemplateFilterFields",
    can: "Callable[[str], bool]",
) -> "ManagedTemplateFilterFields":
    kept: dict[str, object] = {}

    for field in KNOWN_FILTER_FIELDS:
        if field not in fields:
            continue
        value = fields[field]  # type: ignore[literal-required]
        if not can(field_capability_key(field)):
            continue
        if field in _STRING_LOOKUP_FIELDS and not _supported_string_lookup(value, can):
            continue
        kept[field] = value

    return cast("ManagedTemplateFilterFields", kept)


def _supported_string_lookup(value: object, can: "Callable[[str], bool]") -> bool:
    """Whether the backend can answer one string-field filter.

    A bare ``str`` counts as ``exact`` *and* case-sensitive -- that is what the vocabulary
    says it means -- so a backend lacking either cannot answer it.

    Only reached for the four string fields. ``status`` also carries an ``exact`` lookup, but
    its value is an enum member matched as a token rather than a string, so the
    ``stringLookups.*`` keys have nothing to say about it.
    """
    if isinstance(value, str):
        return can("stringLookups.exact") and can("stringLookups.caseSensitive")
    if not isinstance(value, dict):
        return True

    lookup = value.get("lookup", "exact")
    if lookup not in _STRING_LOOKUPS:
        return True
    if not can(f"stringLookups.{_STRING_LOOKUP_CAPABILITY_SUFFIX[lookup]}"):
        return False
    # Absent ``case_sensitive`` reads as True, matching what a bare string means.
    if value.get("case_sensitive", True) is False:
        return can("stringLookups.caseInsensitive")
    return can("stringLookups.caseSensitive")


# --------------------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------------------


def sort_templates(
    templates: "Iterable[ManagedTemplate]",
    order_by: "ManagedTemplateOrderBy | None",
) -> list[ManagedTemplate]:
    """Order templates totally and stably, for a backend that reads a complete set anyway.

    Returns the rows untouched when ``order_by`` is None: no ordering was asked for, and the
    backend's own order is what an unordered listing has always returned.

    **Total**: every field breaks ties on ``(key, version)``, which is unique per row.
    **Stable across directions**: neither the tiebreak nor the placement of absent values
    flips with ``direction``. Either would let a page boundary move between two requests over
    the same data, which drops a row from one page and repeats it on another.

    Strings compare by code point rather than by locale, so two machines serving two pages of
    one listing cannot disagree about where the boundary falls.

    Note this differs from ``vintasend``'s ``sort_notifications``, which reverses its ``id``
    tiebreak with the primary direction. The two are independently total; this one is written
    the way the TypeScript sibling's ``sortTemplates`` is, so a page boundary is the same row
    in either ecosystem.

    param templates: Iterable[ManagedTemplate]
    param order_by: ManagedTemplateOrderBy | None
    return: list[ManagedTemplate]
    """
    rows = list(templates)
    if order_by is None:
        return rows

    attr = _ORDER_FIELD_TO_ATTR[order_by["field"]]
    sign = -1 if order_by["direction"] == "desc" else 1

    def compare(left: ManagedTemplate, right: ManagedTemplate) -> int:
        # Absent values sort last in *both* directions, so this is settled before the sign is
        # applied. Nothing in ``ManagedTemplate`` is typed nullable for these six, but a
        # backend hydrating from a store that allows null would otherwise put its nulls first
        # descending and last ascending.
        nullness = _compare_nullness(getattr(left, attr, None), getattr(right, attr, None))
        if nullness != 0:
            return nullness
        primary = _compare_values(getattr(left, attr), getattr(right, attr))
        if primary != 0:
            return primary * sign
        return _compare_values(left.key, right.key) or _compare_values(left.version, right.version)

    return sorted(rows, key=functools.cmp_to_key(compare))


def _compare_nullness(left: object, right: object) -> int:
    """-1, 0 or 1 according to which side is missing a value; 0 when both have one."""
    left_missing, right_missing = left is None, right is None
    if left_missing == right_missing:
        return 0
    return 1 if left_missing else -1


def _compare_values(left: object, right: object) -> int:
    """Order two present values of the same orderable field.

    Enums compare by their wire value, so a status order is the same order the TypeScript
    sibling produces from the same four strings rather than Python's declaration order.
    Everything else compares with ``<``, which for ``str`` is code point order.
    """
    if isinstance(left, Enum) and isinstance(right, Enum):
        left, right = left.value, right.value
    if left == right:
        return 0
    return -1 if left < right else 1  # type: ignore[operator]
