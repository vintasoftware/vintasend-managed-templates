"""Ordering: the comparator, and the seam that carries an order to the store.

Two things are being guarded here, and they fail differently.

``sort_templates`` has to produce a **total, stable** order. A comparator that leaves two rows
tied, or that reverses its tiebreak with the direction, does not look broken -- it looks like a
sort. What it actually does is let a page boundary move between two requests over unchanged
data, which drops a row from one page and repeats it on another.

``ManagedTemplateService`` has to **refuse** an order the backend cannot apply rather than pass
it on to be ignored. An unsupported filter is dropped and the caller sees extra rows; an
ignored order returns exactly the rows asked for in an arbitrary sequence, and nothing
downstream can tell that apart from a sort that happened.
"""

import datetime
import itertools

import pytest

from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.exceptions import ManagedTemplateUnsupportedOrderingError
from vintasend_managed_templates.filters import (
    MANAGED_TEMPLATE_ORDER_BY_FIELDS,
    order_by_capability_key,
    sort_templates,
)
from vintasend_managed_templates.managed_template_service import ManagedTemplateService

from .fakes import InMemoryTemplateManagerBackend
from .test_capabilities import make_template


NOW = datetime.datetime(2026, 1, 1, 12, 0, 0)


def keys(rows) -> list[tuple[str, int]]:
    return [(t.key, t.version) for t in rows]


# --- the comparator ------------------------------------------------------------------


def test_no_order_leaves_the_rows_as_they_came():
    """Omitted means the store's own order, which is what an unordered listing returns."""
    rows = [make_template("b"), make_template("a"), make_template("c")]

    assert sort_templates(rows, None) == rows


def test_no_order_still_hands_back_a_new_list():
    """The caller's list is not the backend's to alias -- the next write would reorder a result."""
    rows = [make_template("b"), make_template("a")]

    assert sort_templates(rows, None) is not rows


@pytest.mark.parametrize(
    ("field", "rows", "ascending"),
    [
        pytest.param(
            "key",
            [make_template("receipt"), make_template("welcome"), make_template("base")],
            [("base", 1), ("receipt", 1), ("welcome", 1)],
            id="key",
        ),
        pytest.param(
            "name",
            [
                make_template("a", name="Receipt"),
                make_template("b", name="Base"),
                make_template("c", name="Welcome"),
            ],
            [("b", 1), ("a", 1), ("c", 1)],
            id="name",
        ),
        pytest.param(
            "status",
            [
                make_template("a", status=ManagedTemplateStatus.DRAFT),
                make_template("b", status=ManagedTemplateStatus.ACTIVE),
                make_template("c", status=ManagedTemplateStatus.INACTIVE),
            ],
            [("b", 1), ("a", 1), ("c", 1)],
            id="status-orders-by-its-wire-value-not-its-declaration",
        ),
        pytest.param(
            "created_at",
            [
                make_template("a", created=NOW + datetime.timedelta(days=2)),
                make_template("b", created=NOW),
                make_template("c", created=NOW + datetime.timedelta(days=1)),
            ],
            [("b", 1), ("c", 1), ("a", 1)],
            id="created_at",
        ),
        pytest.param(
            "updated_at",
            [
                make_template("a", created=NOW + datetime.timedelta(days=2)),
                make_template("b", created=NOW),
            ],
            [("b", 1), ("a", 1)],
            id="updated_at",
        ),
    ],
)
def test_each_field_orders_both_ways(field, rows, ascending):
    assert keys(sort_templates(rows, {"field": field, "direction": "asc"})) == ascending
    assert keys(sort_templates(rows, {"field": field, "direction": "desc"})) == ascending[::-1]


def test_version_orders_numerically_rather_than_lexicographically():
    """v10 comes after v2. A store padding versions into strings gets this wrong silently."""
    rows = [make_template("k", v) for v in (10, 2, 3, 1, 11)]

    ordered = sort_templates(rows, {"field": "version", "direction": "asc"})

    assert [t.version for t in ordered] == [1, 2, 3, 10, 11]


def test_strings_compare_by_code_point_rather_than_by_locale():
    """Two machines serving two pages of one listing must not disagree about the boundary.

    Under a locale-aware collation ``a`` sorts before ``Z``; by code point it does not. Which
    answer is "nicer" is not the question -- the two must never differ by host.
    """
    rows = [make_template("a", name="apple"), make_template("b", name="Zebra")]

    ordered = sort_templates(rows, {"field": "name", "direction": "asc"})

    assert [t.name for t in ordered] == ["Zebra", "apple"]


def test_ties_break_on_key_and_version():
    rows = [
        make_template("welcome", 2, name="Same"),
        make_template("receipt", 1, name="Same"),
        make_template("welcome", 1, name="Same"),
    ]

    ordered = sort_templates(rows, {"field": "name", "direction": "asc"})

    assert keys(ordered) == [("receipt", 1), ("welcome", 1), ("welcome", 2)]


def test_the_tiebreak_does_not_reverse_with_the_direction():
    """The tiebreak exists to make the order total, not to be part of the sort the caller asked for.

    Flipping it with the direction would make ``asc`` and ``desc`` disagree about which of two
    equal rows comes first, which is how a page boundary starts dropping and repeating rows.
    """
    rows = [make_template("welcome", 1, name="Same"), make_template("receipt", 1, name="Same")]

    ascending = sort_templates(rows, {"field": "name", "direction": "asc"})
    descending = sort_templates(rows, {"field": "name", "direction": "desc"})

    assert keys(ascending) == keys(descending) == [("receipt", 1), ("welcome", 1)]


def test_absent_values_sort_last_in_both_directions():
    """Also settled before the direction sign, and for the same reason as the tiebreak.

    Nothing in ``ManagedTemplate`` is typed nullable for these six, but a backend hydrating
    from a store that allows null would otherwise put its nulls first descending.
    """
    present = make_template("a", name="Name")
    missing = make_template("b", name=None)  # type: ignore[arg-type]

    ascending = sort_templates([missing, present], {"field": "name", "direction": "asc"})
    descending = sort_templates([missing, present], {"field": "name", "direction": "desc"})

    assert keys(ascending) == keys(descending) == [("a", 1), ("b", 1)]


@pytest.mark.parametrize("field", MANAGED_TEMPLATE_ORDER_BY_FIELDS)
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_the_order_is_total_for_every_field(field, direction):
    """No two distinct rows may tie, or the order is not a page boundary anyone can trust.

    Asserted by sorting every permutation of a store whose rows share values on purpose: a
    total order gives the same answer from any starting arrangement.
    """
    rows = [
        make_template("welcome", 1, name="Same", status=ManagedTemplateStatus.ACTIVE),
        make_template("welcome", 2, name="Same", status=ManagedTemplateStatus.ACTIVE),
        make_template("receipt", 1, name="Same", status=ManagedTemplateStatus.ACTIVE),
        make_template("receipt", 2, name="Same", status=ManagedTemplateStatus.ACTIVE),
    ]
    order_by = {"field": field, "direction": direction}

    answers = {
        tuple(keys(sort_templates(list(arrangement), order_by)))
        for arrangement in itertools.permutations(rows)
    }

    assert len(answers) == 1, f"{field} {direction} depends on the order it was given"


# --- the service refuses what the backend cannot apply --------------------------------


@pytest.fixture
def sortable(service, backend):
    """The default fake declares all six orderable, being an in-memory store."""
    return service


def test_no_order_by_is_always_valid(service):
    assert service.validate_order_by(None) is None


def test_an_unorderable_field_is_refused(service):
    with pytest.raises(ManagedTemplateUnsupportedOrderingError, match="not an orderable field"):
        service.validate_order_by({"field": "tags", "direction": "asc"})


def test_an_unknown_direction_is_refused(service):
    with pytest.raises(ManagedTemplateUnsupportedOrderingError, match="not a sort direction"):
        service.validate_order_by({"field": "key", "direction": "sideways"})


def test_a_field_the_backend_declines_is_refused_by_its_capability_key(service, backend):
    """The message names the key, so a caller can look up what to ask the report for."""
    backend.filter_capabilities["orderBy.status"] = False

    with pytest.raises(ManagedTemplateUnsupportedOrderingError, match=r"orderBy\.status is False"):
        service.validate_order_by({"field": "status", "direction": "asc"})


def test_a_backend_that_declares_nothing_can_order_by_nothing(service, backend):
    """The default is False, so silence means unsortable rather than fully sortable."""
    backend.filter_capabilities = {}

    assert service.get_supported_order_by_fields() == []
    with pytest.raises(ManagedTemplateUnsupportedOrderingError):
        service.validate_order_by({"field": "created_at", "direction": "asc"})


def test_the_supported_fields_are_the_ones_declared(service, backend):
    backend.filter_capabilities = {
        "orderBy.key": True,
        "orderBy.createdAt": True,
        "orderBy.status": False,
    }

    assert service.get_supported_order_by_fields() == ["key", "created_at"]


def test_the_supported_fields_come_back_in_vocabulary_order(service):
    assert service.get_supported_order_by_fields() == list(MANAGED_TEMPLATE_ORDER_BY_FIELDS)


def test_the_report_merges_the_backend_over_the_library_default(service, backend):
    backend.filter_capabilities = {"fields.key": False}

    report = service.get_backend_supported_filter_capabilities()

    assert report["fields.key"] is False
    assert report["fields.name"] is True
    # Not mentioned by the backend, so it falls to the default -- which for ordering is False.
    assert report["orderBy.createdAt"] is False


def test_a_truthy_non_boolean_report_is_coerced(service, backend):
    """The report goes on the wire as ``boolean``, so a backend cannot put a 1 there."""
    backend.filter_capabilities = {"fields.key": 1, "fields.name": 0}  # type: ignore[dict-item]

    report = service.get_backend_supported_filter_capabilities()

    assert report["fields.key"] is True
    assert report["fields.name"] is False


# --- the order reaches the store ------------------------------------------------------


@pytest.fixture
def stocked(backend):
    """Five keys, deliberately not inserted in name order."""
    backend.templates = [
        make_template("e", name="Echo"),
        make_template("a", name="Alpha"),
        make_template("d", name="Delta"),
        make_template("b", name="Bravo"),
        make_template("c", name="Charlie"),
    ]
    return backend


def test_the_order_is_applied_before_the_page_is_chosen(service, stocked):
    """The failure this catches looks right on page 1 and is wrong on every page after it.

    A backend that sorts the page it already chose returns the store's first two rows in name
    order. One that sorts before paging returns the *globally* first two names.
    """
    page = service.get_paginated_templates(
        1, 2, include_all_versions=True, order_by={"field": "name", "direction": "asc"}
    )
    second = service.get_paginated_templates(
        2, 2, include_all_versions=True, order_by={"field": "name", "direction": "asc"}
    )

    assert [t.name for t in page] == ["Alpha", "Bravo"]
    assert [t.name for t in second] == ["Charlie", "Delta"]


def test_a_filtered_listing_orders_too(service, stocked):
    page = service.get_paginated_filtered_templates(
        {}, 1, 3, order_by={"field": "name", "direction": "desc"}
    )

    assert [t.name for t in page] == ["Echo", "Delta", "Charlie"]


def test_an_unsupported_order_is_refused_before_the_backend_is_touched(service, stocked):
    stocked.filter_capabilities["orderBy.name"] = False
    stocked.calls.clear()

    with pytest.raises(ManagedTemplateUnsupportedOrderingError):
        service.get_paginated_filtered_templates(
            {}, 1, 3, order_by={"field": "name", "direction": "asc"}
        )

    assert stocked.calls == []


class BackendWithoutOrdering(InMemoryTemplateManagerBackend):
    """A backend written against the seam before ordering existed.

    Its paginated methods take no ``order_by``, and it declares nothing -- so every
    ``orderBy.*`` key falls to the False default and it is never handed one.

    Dropping the argument narrows the seam's signature, so mypy flags both overrides. That
    narrowing is the thing under test -- the ignores keep it, rather than fixing it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.filter_capabilities = {}

    def get_paginated_templates(self, page: int, page_size: int):  # type: ignore[override]
        self.calls.append("get_paginated_templates")
        return self._page(self.templates, page, page_size)

    def get_paginated_filtered_templates(self, filters, page: int, page_size: int):  # type: ignore[override]
        self.calls.append("get_paginated_filtered_templates")
        return self._page([t for t in self.templates if self._matches(t, filters)], page, page_size)


def test_a_backend_written_before_ordering_keeps_working(email_renderer):
    """The compatibility guarantee: optional argument, never passed unasked.

    This backend would raise ``TypeError`` on an unexpected keyword, so an unconditional
    ``order_by=None`` would break every implementation that shipped before this release.
    """
    backend = BackendWithoutOrdering()
    backend.templates = [make_template("a"), make_template("b")]
    service = ManagedTemplateService(backend, email_renderer)

    assert keys(service.get_paginated_templates(1, 10, include_all_versions=True)) == [
        ("a", 1),
        ("b", 1),
    ]
    assert keys(service.get_paginated_filtered_templates({}, 1, 10)) == [("a", 1), ("b", 1)]


def test_a_backend_written_before_ordering_reports_nothing_orderable(email_renderer):
    service = ManagedTemplateService(BackendWithoutOrdering(), email_renderer)

    assert service.get_supported_order_by_fields() == []


def test_every_orderable_field_can_be_asked_for_end_to_end(service, stocked):
    """A field in the vocabulary that no read path accepts is a capability nobody can use."""
    for field in MANAGED_TEMPLATE_ORDER_BY_FIELDS:
        assert order_by_capability_key(field) in service.get_backend_supported_filter_capabilities()
        rows = service.get_paginated_filtered_templates(
            {}, 1, 10, order_by={"field": field, "direction": "asc"}
        )
        assert len(rows) == 5


def test_the_report_is_read_from_the_backend_once(email_renderer):
    """Static per implementation, and now read on every filtered call rather than on demand.

    Before negotiation existed the report was only read when a caller asked for it. It is
    now consulted before every filtered read, so asking the backend each time would put a
    call on the hot path for an answer that cannot change.
    """
    backend = InMemoryTemplateManagerBackend()
    reports: list[int] = []

    def counting_report() -> dict[str, bool]:
        reports.append(1)
        return {}

    backend.get_filter_capabilities = counting_report  # type: ignore[method-assign]
    service = ManagedTemplateService(backend, email_renderer)

    service.get_backend_supported_filter_capabilities()
    service.get_all_templates()
    service.get_paginated_filtered_templates({}, 1, 10)

    assert len(reports) == 1
