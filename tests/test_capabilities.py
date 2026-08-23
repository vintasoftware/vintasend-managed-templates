"""The capability vocabulary, and the negotiation that acts on it.

A capability report nothing acts on is decoration. These tests cover both halves: the map
itself (one key per thing a backend can decline, and the right default for each), and
``prune_unsupported_filters``, which is what makes a declared limitation change what the
backend is actually asked for.

The rule the pruning tests are written against is one sentence: **dropping only ever widens.**
A pruned filter must match every row the original matched, and may match more. Each corner of
the implementation -- why an ``or`` goes whole, why a ``not`` over nothing goes -- is a
consequence of that rule rather than a separate decision, so it is asserted as that rule
rather than as an expected output shape.
"""

import datetime

import pytest

from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.dataclasses import ManagedTemplate, ManagedTemplateTag
from vintasend_managed_templates.filters import (
    DEFAULT_TEMPLATE_BACKEND_FILTER_CAPABILITIES,
    KNOWN_FILTER_FIELDS,
    MANAGED_TEMPLATE_ORDER_BY_FIELDS,
    ManagedTemplateFilterFields,
    field_capability_key,
    is_empty_filter,
    order_by_capability_key,
    prune_unsupported_filters,
    supports_capability,
)

from .fakes import InMemoryTemplateManagerBackend


NOW = datetime.datetime(2026, 1, 1, 12, 0, 0)


# --- the vocabulary ------------------------------------------------------------------


def test_every_filter_field_has_a_capability_key():
    """A field with no key is one no backend can decline, so it can never be negotiated."""
    for field in KNOWN_FILTER_FIELDS:
        assert field_capability_key(field) in DEFAULT_TEMPLATE_BACKEND_FILTER_CAPABILITIES


def test_the_field_capability_keys_cover_the_vocabulary_and_nothing_else():
    """Guards the map in both directions: a stale key is as wrong as a missing one."""
    documented = {
        key.split(".", 1)[1]
        for key in DEFAULT_TEMPLATE_BACKEND_FILTER_CAPABILITIES
        if key.startswith("fields.")
    }
    assert documented == {field_capability_key(f).split(".", 1)[1] for f in KNOWN_FILTER_FIELDS}


def test_every_orderable_field_has_a_capability_key():
    for field in MANAGED_TEMPLATE_ORDER_BY_FIELDS:
        assert order_by_capability_key(field) in DEFAULT_TEMPLATE_BACKEND_FILTER_CAPABILITIES


def test_the_order_by_keys_are_camel_case():
    """They go on the wire, so they are spelled the way the TypeScript sibling spells them."""
    assert order_by_capability_key("created_at") == "orderBy.createdAt"
    assert order_by_capability_key("updated_at") == "orderBy.updatedAt"


def test_ordering_defaults_to_unsupported_and_everything_else_to_supported():
    """The one asymmetry in the map, and the reason it exists.

    ``orderBy.*`` is new vocabulary. A ``True`` default would have every backend that shipped
    before ordering existed claim an order it silently ignores -- the same rule
    ``fields.readAtRange`` was introduced under on the notification side.
    """
    for key, supported in DEFAULT_TEMPLATE_BACKEND_FILTER_CAPABILITIES.items():
        assert supported is not key.startswith("orderBy."), key


def test_there_is_no_pagination_key():
    """Unlike the notification seam, this one is 1-indexed throughout -- nothing to negotiate."""
    assert not [
        k for k in DEFAULT_TEMPLATE_BACKEND_FILTER_CAPABILITIES if k.startswith("pagination.")
    ]


def test_an_unknown_capability_reads_as_supported():
    """Backends declare only what they cannot do, so a key added later must not read as False."""
    assert supports_capability({}, "fields.somethingAddedLater") is True


def test_a_declared_capability_is_read_as_declared():
    assert supports_capability({"fields.key": False}, "fields.key") is False


def test_an_unorderable_field_has_no_capability_key():
    with pytest.raises(KeyError):
        order_by_capability_key("tags")


def test_an_unknown_filter_field_has_no_capability_key():
    with pytest.raises(KeyError):
        field_capability_key("tags")


# --- the widening invariant ----------------------------------------------------------


def make_template(
    key: str,
    version: int = 1,
    *,
    name: str = "Welcome",
    description: str = "Sent on signup",
    status: ManagedTemplateStatus = ManagedTemplateStatus.ACTIVE,
    tags: tuple[str, ...] = (),
    is_abstract: bool = False,
    template_managed_backend: str = "in-memory",
    created: datetime.datetime = NOW,
) -> ManagedTemplate:
    return ManagedTemplate(
        id=f"{key}-{version}",
        name=name,
        description=description,
        key=key,
        template_managed_backend=template_managed_backend,
        body_template="Hi",
        version=version,
        status=status,
        created=created,
        updated=created,
        tags=[
            ManagedTemplateTag(
                id=slug, text=slug, slug=slug, status="active", created=NOW, updated=NOW
            )
            for slug in tags
        ],
        is_abstract=is_abstract,
    )


@pytest.fixture
def store() -> InMemoryTemplateManagerBackend:
    """A store deliberately varied along every axis a filter can test.

    The invariant is only meaningful against rows that some filters match and others do not,
    so keys, versions, statuses, tags and the two flags all differ across it.
    """
    backend = InMemoryTemplateManagerBackend()
    backend.templates = [
        make_template("welcome", 1, name="Welcome", status=ManagedTemplateStatus.INACTIVE),
        make_template("welcome", 2, name="Welcome", tags=("billing",)),
        make_template("receipt", 1, name="Receipt", tags=("billing", "urgent")),
        make_template("receipt", 2, name="receipt lowercase", status=ManagedTemplateStatus.DRAFT),
        make_template("base", 1, name="Base layout", is_abstract=True),
        make_template(
            "archived", 1, name="Old", status=ManagedTemplateStatus.ARCHIVED, tags=("urgent",)
        ),
        make_template(
            "other",
            1,
            name="Other",
            template_managed_backend="django",
            created=NOW.replace(year=2027),
        ),
    ]
    return backend


def matching(store: InMemoryTemplateManagerBackend, filters) -> set[tuple[str, int]]:
    """The ``(key, version)`` of every row the filter selects, evaluated by the fake backend."""
    return {(t.key, t.version) for t in store.templates if store._matches(t, filters)}


def assert_pruning_widens(store, filters, capabilities) -> None:
    """The whole rule, asserted directly: nothing the original matched may be lost.

    Stated as a subset rather than an equality on purpose -- a pruned filter is *allowed* to
    match more, and usually does. What it may never do is match less, because a caller cannot
    see a row that was never returned.
    """
    pruned = prune_unsupported_filters(filters, capabilities)
    before = matching(store, filters)
    after = matching(store, pruned)

    assert before <= after, (
        f"pruning narrowed the filter: {sorted(before - after)} were dropped from the result"
    )


def capabilities(**declined: bool) -> dict[str, bool]:
    """A merged report with the named keys declined and everything else at its default."""
    return {**DEFAULT_TEMPLATE_BACKEND_FILTER_CAPABILITIES, **declined}


DECLINE_KEY = {"fields.key": False}
DECLINE_OR = {"logical.or": False}
DECLINE_NOT = {"logical.not": False}
DECLINE_NOT_NESTED = {"logical.notNested": False}


WIDENING_CASES = [
    pytest.param({"key": "welcome"}, DECLINE_KEY, id="a-single-unsupported-field"),
    pytest.param(
        {"key": "welcome", "status": ManagedTemplateStatus.ACTIVE},
        DECLINE_KEY,
        id="one-of-two-fields-unsupported",
    ),
    pytest.param(
        {"and": [{"key": "welcome"}, {"status": ManagedTemplateStatus.ACTIVE}]},
        DECLINE_KEY,
        id="an-and-branch-dropped",
    ),
    pytest.param(
        {"or": [{"key": "welcome"}, {"status": ManagedTemplateStatus.ACTIVE}]},
        DECLINE_KEY,
        id="an-or-with-one-branch-emptied",
    ),
    pytest.param(
        {"or": [{"key": "welcome"}, {"key": "receipt"}]},
        DECLINE_OR,
        id="an-or-the-backend-cannot-compose",
    ),
    pytest.param({"not": {"key": "welcome"}}, DECLINE_KEY, id="a-not-over-nothing"),
    pytest.param({"not": {"key": "welcome"}}, DECLINE_NOT, id="a-not-the-backend-cannot-negate"),
    pytest.param(
        {"not": {"or": [{"key": "welcome"}, {"key": "receipt"}]}},
        DECLINE_NOT_NESTED,
        id="a-not-wrapping-a-group",
    ),
    pytest.param(
        {"and": [{"or": [{"key": "welcome"}, {"key": "base"}]}, {"is_abstract": False}]},
        DECLINE_KEY,
        id="an-or-nested-in-an-and",
    ),
    pytest.param(
        {"most_recent_active_version": True},
        {"fields.mostRecentActiveVersion": False},
        id="the-default-listings-own-filter",
    ),
    pytest.param({"name": "Welcome"}, {"stringLookups.exact": False}, id="a-bare-string-lookup"),
    pytest.param(
        {"name": "Welcome"},
        {"stringLookups.caseSensitive": False},
        id="a-bare-string-against-a-case-folding-store",
    ),
    pytest.param(
        {"name": {"lookup": "includes", "value": "elcome", "case_sensitive": False}},
        {"stringLookups.caseInsensitive": False},
        id="a-case-insensitive-lookup-a-store-cannot-fold",
    ),
    pytest.param(
        {"name": {"lookup": "ends_with", "value": "come", "case_sensitive": True}},
        {"stringLookups.endsWith": False},
        id="a-lookup-the-store-does-not-offer",
    ),
    pytest.param(
        {"includes_all_tags": ["billing"]},
        {"fields.includesAllTags": False},
        id="a-tag-filter-the-store-cannot-answer",
    ),
]


@pytest.mark.parametrize(("filters", "declined"), WIDENING_CASES)
def test_pruning_only_ever_widens(store, filters, declined):
    assert_pruning_widens(store, filters, capabilities(**declined))


@pytest.mark.parametrize(("filters", "declined"), WIDENING_CASES)
def test_a_fully_capable_backend_is_handed_the_filter_untouched(store, filters, declined):
    """The other direction: negotiation must not narrow, and must not fire when it need not.

    Without this, a pruner that dropped everything would pass every widening test above.
    """
    assert prune_unsupported_filters(filters, capabilities()) == filters


# --- the shapes the rule produces ----------------------------------------------------
#
# The invariant above is the contract. These pin the specific shapes it produces, because a
# backend has to translate them and "something wider" is not a shape.


def test_an_or_is_dropped_whole_rather_than_partially():
    """Removing one branch of a disjunction narrows it, which is the one thing pruning may not do."""
    pruned = prune_unsupported_filters(
        {"or": [{"key": "welcome"}, {"status": ManagedTemplateStatus.ACTIVE}]},
        capabilities(**DECLINE_KEY),
    )

    assert pruned == {}


def test_an_and_keeps_the_branches_that_survive():
    pruned = prune_unsupported_filters(
        {"and": [{"key": "welcome"}, {"status": ManagedTemplateStatus.ACTIVE}, {"version": 2}]},
        capabilities(**DECLINE_KEY),
    )

    assert pruned == {"and": [{"status": ManagedTemplateStatus.ACTIVE}, {"version": 2}]}


def test_a_one_branch_and_collapses_to_the_branch():
    """Fewer groups for a backend to translate, and the same rows either way."""
    pruned = prune_unsupported_filters(
        {"and": [{"key": "welcome"}, {"version": 2}]}, capabilities(**DECLINE_KEY)
    )

    assert pruned == {"version": 2}


def test_a_not_whose_inside_pruned_away_is_dropped():
    """Negating "everything" is "nothing" -- a narrowing, so the group goes instead."""
    assert prune_unsupported_filters({"not": {"key": "x"}}, capabilities(**DECLINE_KEY)) == {}


def test_a_not_over_a_group_survives_when_the_backend_can_nest():
    filters = {"not": {"or": [{"version": 1}, {"version": 2}]}}

    assert prune_unsupported_filters(filters, capabilities()) == filters


def test_a_bare_string_needs_both_exact_and_case_sensitive():
    """A bare string means exact *and* case-sensitive, so a store lacking either cannot answer it."""
    assert (
        prune_unsupported_filters(
            {"name": "Welcome"}, capabilities(**{"stringLookups.exact": False})
        )
        == {}
    )
    assert (
        prune_unsupported_filters(
            {"name": "Welcome"}, capabilities(**{"stringLookups.caseSensitive": False})
        )
        == {}
    )


def test_a_status_filter_is_not_read_as_a_string_lookup():
    """``status`` carries an ``exact`` lookup too, but its value is a token, not a string match.

    Dropping it for want of ``stringLookups.exact`` would refuse a filter the backend can
    answer -- narrowing what a client can ask for, for no reason the backend gave.
    """
    filters: ManagedTemplateFilterFields = {
        "status": {"lookup": "exact", "value": ManagedTemplateStatus.ACTIVE}
    }

    assert (
        prune_unsupported_filters(filters, capabilities(**{"stringLookups.exact": False}))
        == filters
    )


def test_pruning_leaves_the_caller_s_filter_alone(store):
    """The input is a caller's dict, and a caller may reuse it for the next read."""
    filters = {"and": [{"key": "welcome"}, {"version": 2}]}
    before = repr(filters)

    prune_unsupported_filters(filters, capabilities(**DECLINE_KEY))

    assert repr(filters) == before


# --- is_empty_filter -----------------------------------------------------------------


@pytest.mark.parametrize(
    "filters",
    [
        pytest.param({}, id="no-fields"),
        pytest.param({"and": [{}, {}]}, id="an-and-of-nothing"),
        pytest.param({"or": [{}]}, id="an-or-of-nothing"),
        pytest.param({"not": {}}, id="a-not-of-nothing"),
    ],
)
def test_a_filter_that_constrains_nothing_is_empty(filters):
    assert is_empty_filter(filters) is True


@pytest.mark.parametrize(
    "filters",
    [
        pytest.param({"version": 1}, id="a-field"),
        pytest.param({"and": [{}, {"version": 1}]}, id="an-and-with-one-branch-left"),
        pytest.param({"not": {"version": 1}}, id="a-not-of-something"),
    ],
)
def test_a_filter_that_constrains_something_is_not_empty(filters):
    assert is_empty_filter(filters) is False


# --- the service acts on the report --------------------------------------------------
#
# Publishing a capability map and then sending the backend the filter it just declined is the
# failure these guard. It is not hypothetical: the default listing asks for
# ``most_recent_active_version`` on every unqualified read.


class RecordingBackend(InMemoryTemplateManagerBackend):
    """Remembers the filter it was handed, so a test can assert what actually reached it."""

    def __init__(self) -> None:
        super().__init__()
        self.received: list[dict] = []

    def get_filtered_templates(self, filters):
        self.received.append(filters)
        return super().get_filtered_templates(filters)

    def get_paginated_filtered_templates(self, filters, page, page_size, order_by=None):
        self.received.append(filters)
        return super().get_paginated_filtered_templates(filters, page, page_size, order_by)


@pytest.fixture
def recording(email_renderer):
    from vintasend_managed_templates.managed_template_service import ManagedTemplateService

    backend = RecordingBackend()
    backend.templates = [
        make_template("welcome", 1, status=ManagedTemplateStatus.INACTIVE),
        make_template("welcome", 2),
        make_template("receipt", 1),
    ]
    return ManagedTemplateService(backend, email_renderer), backend


def test_a_filtered_read_is_pruned_before_it_reaches_the_backend(recording):
    service, backend = recording
    backend.filter_capabilities = {"fields.key": False}

    service.get_filtered_templates({"key": "welcome", "version": 2})

    assert backend.received == [{"version": 2}]


def test_a_paginated_filtered_read_is_pruned_too(recording):
    service, backend = recording
    backend.filter_capabilities = {"fields.key": False}

    service.get_paginated_filtered_templates({"key": "welcome"}, 1, 10)

    assert backend.received == [{}]


def test_the_default_listing_does_not_ask_for_a_filter_the_backend_declined(recording):
    """``get_all_templates`` asks for ``most_recent_active_version`` on every unqualified read.

    A backend that declared it cannot answer that was previously handed it anyway. Now it is
    dropped -- and the listing widens to every version, which is visible in the rows.
    """
    service, backend = recording
    backend.filter_capabilities = {"fields.mostRecentActiveVersion": False}

    rows = service.get_all_templates()

    assert backend.received == [{}]
    assert len(rows) == 3


def test_a_capable_backend_still_gets_the_whole_filter(recording):
    service, backend = recording

    service.get_all_templates()

    assert backend.received == [{"most_recent_active_version": True}]


def test_validation_still_runs_before_negotiation(recording):
    """A typo must raise rather than be pruned away as "a field this backend cannot answer"."""
    from vintasend_managed_templates.exceptions import ManagedTemplateInvalidFilterError

    service, backend = recording

    with pytest.raises(ManagedTemplateInvalidFilterError, match="unknown field"):
        service.get_filtered_templates({"kye": "welcome"})

    assert backend.received == []
