import datetime

import pytest

from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.exceptions import (
    ManagedTemplateInvalidFilterError,
    ManagedTemplateNotFoundError,
    ManagedTemplateStatusTransitionError,
)
from vintasend_managed_templates.managed_template_service import ManagedTemplateService

from .fakes import EPOCH, make_create_input, make_notification, make_update_input


DRAFT = ManagedTemplateStatus.DRAFT
ACTIVE = ManagedTemplateStatus.ACTIVE
INACTIVE = ManagedTemplateStatus.INACTIVE
ARCHIVED = ManagedTemplateStatus.ARCHIVED


# ----------------------------------------------------------------------
# Construction
# ----------------------------------------------------------------------


def test_stores_both_seams_and_defaults_to_validating_transitions(backend, email_renderer):
    service = ManagedTemplateService(backend, email_renderer)

    assert service.template_manager_backend is backend
    assert service.template_renderer is email_renderer
    assert service.validate_status_transitions is True


def test_transition_validation_can_be_turned_off(backend, email_renderer):
    assert (
        ManagedTemplateService(
            backend, email_renderer, validate_status_transitions=False
        ).validate_status_transitions
        is False
    )


# ----------------------------------------------------------------------
# Versions
# ----------------------------------------------------------------------


def test_create_template_starts_at_version_one_in_draft(service):
    template = service.create_template(make_create_input("welcome"))

    assert (template.key, template.version, template.status) == ("welcome", 1, DRAFT)
    assert template.body_template == "Hi {name}"


def test_create_template_passes_every_input_field_through(service):
    template = service.create_template(
        make_create_input(
            "receipt",
            name="Receipt",
            description="After payment",
            template_managed_backend="in-memory",
            template_body="Body",
            template_subject="Subject",
            template_preheader="Preheader",
            tenant="acme",
        )
    )

    assert template.name == "Receipt"
    assert template.description == "After payment"
    assert template.template_managed_backend == "in-memory"
    assert template.subject_template == "Subject"
    assert template.preheader_template == "Preheader"
    assert template.tenant == "acme"


def test_get_template_without_version_returns_the_latest(service, welcome):
    service.update_template("welcome", make_update_input(template_body="v2 body"))

    assert service.get_template("welcome").version == 2


def test_get_template_with_version_pins_that_version(service, welcome):
    service.update_template("welcome", make_update_input(template_body="v2 body"))

    assert service.get_template("welcome", 1).body_template == "Hi {name}"


def test_get_template_raises_for_an_unknown_key(service):
    with pytest.raises(ManagedTemplateNotFoundError):
        service.get_template("nope")


def test_get_template_raises_for_an_unknown_version(service, welcome):
    with pytest.raises(ManagedTemplateNotFoundError):
        service.get_template("welcome", 99)


def test_update_template_creates_a_new_version_instead_of_mutating(service, welcome):
    updated = service.update_template("welcome", make_update_input(template_body="New body"))

    assert updated.version == 2
    assert updated.body_template == "New body"
    # v1 is untouched -- publishing history must stay reproducible.
    assert service.get_template("welcome", 1).body_template == "Hi {name}"


def test_update_template_carries_unset_fields_forward(service, welcome):
    updated = service.update_template("welcome", make_update_input(name="Renamed"))

    assert updated.name == "Renamed"
    assert updated.body_template == "Hi {name}"
    assert updated.description == "Sent on signup"


def test_update_template_raises_for_an_unknown_key(service):
    with pytest.raises(ManagedTemplateNotFoundError):
        service.update_template("nope", make_update_input(name="x"))


def test_delete_template_without_version_removes_the_latest(service, welcome):
    service.update_template("welcome", make_update_input(template_body="v2"))

    service.delete_template("welcome")

    assert [t.version for t in service.get_template_versions("welcome")] == [1]


def test_delete_template_with_version_removes_only_that_version(service, welcome):
    service.update_template("welcome", make_update_input(template_body="v2"))

    service.delete_template("welcome", 1)

    assert [t.version for t in service.get_template_versions("welcome")] == [2]


def test_delete_template_raises_for_an_unknown_key(service):
    with pytest.raises(ManagedTemplateNotFoundError):
        service.delete_template("nope")


def test_get_template_versions_returns_newest_first(service, welcome):
    service.update_template("welcome", make_update_input(template_body="v2"))
    service.update_template("welcome", make_update_input(template_body="v3"))

    assert [t.version for t in service.get_template_versions("welcome")] == [3, 2, 1]


def test_get_template_versions_ignores_other_keys(service, welcome):
    service.create_template(make_create_input("receipt"))

    assert [t.key for t in service.get_template_versions("welcome")] == ["welcome"]


def test_get_template_versions_is_empty_for_an_unknown_key(service):
    assert service.get_template_versions("nope") == []


# ----------------------------------------------------------------------
# Statuses
# ----------------------------------------------------------------------


def test_set_status_writes_through_and_returns_the_reread_state(service, welcome, backend):
    result = service.set_status("welcome", ACTIVE, version=1, changed_by="hugo")

    assert result.status is ACTIVE
    assert backend.templates[0].status is ACTIVE
    assert "create_template_status_update" in backend.calls


def test_set_status_defaults_to_the_latest_version(service, welcome):
    service.update_template("welcome", make_update_input(template_body="v2"))

    service.set_status("welcome", ACTIVE)

    assert service.get_template("welcome", 2).status is ACTIVE
    assert service.get_template("welcome", 1).status is DRAFT


def test_set_status_to_the_current_status_is_a_no_op(service, welcome, backend):
    service.activate("welcome", changed_by="hugo")
    backend.calls.clear()

    result = service.activate("welcome", changed_by="hugo")

    assert result.status is ACTIVE
    # No second history entry, and the backend never saw a write.
    assert "create_template_status_update" not in backend.calls
    assert len(backend.status_history) == 1


def test_set_status_raises_for_an_unknown_key(service):
    with pytest.raises(ManagedTemplateNotFoundError):
        service.set_status("nope", ACTIVE)


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (DRAFT, ACTIVE),
        (DRAFT, ARCHIVED),
        (ACTIVE, INACTIVE),
        (ACTIVE, ARCHIVED),
        (INACTIVE, ACTIVE),
        (INACTIVE, ARCHIVED),
    ],
)
def test_allowed_transitions_are_accepted(service, welcome, start, target):
    if start is not DRAFT:
        # Walk DRAFT -> ACTIVE -> (INACTIVE) to reach the starting status legally.
        service.set_status("welcome", ACTIVE)
        if start is INACTIVE:
            service.set_status("welcome", INACTIVE)

    assert service.set_status("welcome", target).status is target


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (DRAFT, INACTIVE),
        (ARCHIVED, ACTIVE),
        (ARCHIVED, DRAFT),
        (ARCHIVED, INACTIVE),
        (ACTIVE, DRAFT),
        (INACTIVE, DRAFT),
    ],
)
def test_disallowed_transitions_raise(service, welcome, start, target):
    if start is ACTIVE:
        service.set_status("welcome", ACTIVE)
    elif start is INACTIVE:
        service.set_status("welcome", ACTIVE)
        service.set_status("welcome", INACTIVE)
    elif start is ARCHIVED:
        service.set_status("welcome", ARCHIVED)

    with pytest.raises(ManagedTemplateStatusTransitionError):
        service.set_status("welcome", target)


def test_transition_error_names_the_template_and_the_allowed_moves(service, welcome):
    with pytest.raises(ManagedTemplateStatusTransitionError) as excinfo:
        service.set_status("welcome", INACTIVE)

    message = str(excinfo.value)
    assert "'welcome' v1" in message
    assert "from 'draft' to 'inactive'" in message
    assert "Allowed: active, archived" in message


def test_transition_error_says_nothing_is_allowed_from_a_terminal_status(service, welcome):
    service.set_status("welcome", ARCHIVED)

    with pytest.raises(ManagedTemplateStatusTransitionError) as excinfo:
        service.set_status("welcome", ACTIVE)

    assert "Allowed: nothing." in str(excinfo.value)


def test_a_rejected_transition_leaves_the_template_untouched(service, welcome, backend):
    with pytest.raises(ManagedTemplateStatusTransitionError):
        service.set_status("welcome", INACTIVE)

    assert service.get_template("welcome").status is DRAFT
    assert backend.status_history == []


def test_transition_validation_off_permits_any_move(backend, email_renderer):
    service = ManagedTemplateService(backend, email_renderer, validate_status_transitions=False)
    service.create_template(make_create_input("welcome"))
    service.set_status("welcome", ARCHIVED)

    assert service.set_status("welcome", ACTIVE).status is ACTIVE


def test_activate_publishes_a_version(service, welcome):
    assert service.activate("welcome", changed_by="hugo").status is ACTIVE


def test_deactivate_retires_a_version(service, welcome):
    service.activate("welcome")

    assert service.deactivate("welcome", changed_by="hugo").status is INACTIVE


def test_archive_retires_a_version_permanently(service, welcome):
    assert service.archive("welcome", changed_by="hugo").status is ARCHIVED


def test_activate_leaves_other_active_versions_of_the_same_key_alone(service, welcome):
    service.activate("welcome", version=1)
    service.update_template("welcome", make_update_input(template_body="v2"))

    service.activate("welcome", version=2)

    assert service.get_template("welcome", 1).status is ACTIVE
    assert service.get_template("welcome", 2).status is ACTIVE


def test_changed_by_is_recorded_verbatim(service, welcome, backend):
    service.activate("welcome", changed_by="hugo@example.com")

    assert backend.status_history[-1].created_by == "hugo@example.com"


def test_changed_by_none_is_passed_straight_through(service, welcome, backend):
    service.activate("welcome")

    assert backend.status_history[-1].created_by is None


def test_get_status_history_is_newest_first(service, welcome):
    service.activate("welcome")
    service.deactivate("welcome")
    service.archive("welcome")

    assert [r.status for r in service.get_status_history("welcome")] == [
        ARCHIVED,
        INACTIVE,
        ACTIVE,
    ]


def test_get_status_history_can_be_scoped_to_one_version(service, welcome):
    service.activate("welcome", version=1)
    service.update_template("welcome", make_update_input(template_body="v2"))
    service.activate("welcome", version=2)

    history = service.get_status_history("welcome", version=2)

    assert [r.version for r in history] == [2]


def test_get_status_history_is_empty_when_nothing_changed(service, welcome):
    assert service.get_status_history("welcome") == []


def test_get_templates_by_status_matches_any_of_the_given_statuses(service, welcome):
    service.create_template(make_create_input("receipt"))
    service.activate("receipt")

    by_status = service.get_templates_by_status([DRAFT, ACTIVE])

    assert {t.key for t in by_status} == {"welcome", "receipt"}
    assert service.get_templates_by_status([ARCHIVED]) == []


def test_can_transition_to_mirrors_the_table(service, welcome):
    template = service.get_template("welcome")

    assert service.can_transition_to(template, ACTIVE) is True
    assert service.can_transition_to(template, INACTIVE) is False


def test_can_transition_to_allows_a_no_op_move(service, welcome):
    assert service.can_transition_to(service.get_template("welcome"), DRAFT) is True


def test_can_transition_to_is_always_true_when_validation_is_off(backend, email_renderer):
    service = ManagedTemplateService(backend, email_renderer, validate_status_transitions=False)
    template = service.create_template(make_create_input("welcome"))

    assert service.can_transition_to(template, INACTIVE) is True


def test_transition_table_can_be_overridden_on_a_subclass(backend, email_renderer):
    class LinearService(ManagedTemplateService):
        ALLOWED_STATUS_TRANSITIONS = {  # noqa: RUF012
            DRAFT: frozenset({ACTIVE}),
            ACTIVE: frozenset(),
            INACTIVE: frozenset(),
            ARCHIVED: frozenset(),
        }

    service = LinearService(backend, email_renderer)
    service.create_template(make_create_input("welcome"))
    service.set_status("welcome", ACTIVE)

    with pytest.raises(ManagedTemplateStatusTransitionError):
        service.set_status("welcome", ARCHIVED)


def test_a_status_missing_from_the_table_allows_nothing(service, welcome):
    service.ALLOWED_STATUS_TRANSITIONS = {}

    assert service.can_transition_to(service.get_template("welcome"), ACTIVE) is False


# ----------------------------------------------------------------------
# Queries
# ----------------------------------------------------------------------


def test_get_all_templates_lists_one_row_per_key(service, welcome):
    service.update_template("welcome", make_update_input(template_body="v2"))
    service.create_template(make_create_input("receipt"))

    listed = service.get_all_templates()

    assert [(t.key, t.version) for t in listed] == [("welcome", 2), ("receipt", 1)]


def test_get_all_templates_can_be_asked_for_every_version(service, welcome):
    service.update_template("welcome", make_update_input(template_body="v2"))
    service.create_template(make_create_input("receipt"))

    assert len(service.get_all_templates(include_all_versions=True)) == 3


def test_get_all_templates_drops_a_key_with_no_active_or_draft_version(service, welcome):
    service.set_status("welcome", ARCHIVED, version=1)
    service.create_template(make_create_input("receipt"))

    assert [t.key for t in service.get_all_templates()] == ["receipt"]
    assert len(service.get_all_templates(include_all_versions=True)) == 2


def test_get_all_templates_is_empty_on_a_fresh_backend(service):
    assert service.get_all_templates() == []


def test_get_filtered_templates_applies_the_filter(service, welcome):
    service.create_template(make_create_input("receipt"))

    assert [t.key for t in service.get_filtered_templates({"key": "receipt"})] == ["receipt"]


def test_get_filtered_templates_handles_logical_groups(service, welcome):
    service.create_template(make_create_input("receipt"))
    service.create_template(make_create_input("reminder"))

    matched = service.get_filtered_templates({"or": [{"key": "welcome"}, {"key": "reminder"}]})

    assert {t.key for t in matched} == {"welcome", "reminder"}


def test_get_filtered_templates_handles_negation(service, welcome):
    service.create_template(make_create_input("receipt"))

    matched = service.get_filtered_templates({"not": {"key": "welcome"}})

    assert [t.key for t in matched] == ["receipt"]


def test_get_filtered_templates_supports_string_lookups(service, welcome):
    service.create_template(make_create_input("welcome-back"))

    matched = service.get_filtered_templates(
        {"key": {"lookup": "starts_with", "value": "WELCOME", "case_sensitive": False}}
    )

    assert {t.key for t in matched} == {"welcome", "welcome-back"}


def test_get_filtered_templates_supports_date_ranges(service, welcome):
    matched = service.get_filtered_templates(
        {"created_at_range": {"from": EPOCH, "to": EPOCH + datetime.timedelta(hours=1)}}
    )

    assert [t.key for t in matched] == ["welcome"]


def test_get_filtered_templates_supports_status_and_version(service, welcome):
    service.update_template("welcome", make_update_input(template_body="v2"))
    service.activate("welcome", version=2)

    matched = service.get_filtered_templates(
        {"and": [{"status": ACTIVE}, {"version": {"lookup": "gte", "value": 2}}]}
    )

    assert [t.version for t in matched] == [2]


def test_get_paginated_templates_slices_the_result(service, welcome):
    service.create_template(make_create_input("receipt"))
    service.create_template(make_create_input("reminder"))

    assert [t.key for t in service.get_paginated_templates(2, 1)] == ["receipt"]
    assert [t.key for t in service.get_paginated_templates(1, 2)] == ["welcome", "receipt"]


def test_get_paginated_templates_past_the_end_is_empty(service, welcome):
    assert service.get_paginated_templates(9, 10) == []


def test_get_paginated_templates_pages_one_row_per_key(service, welcome):
    service.update_template("welcome", make_update_input(template_body="v2"))
    service.create_template(make_create_input("receipt"))

    paged = service.get_paginated_templates(1, 10)

    assert [(t.key, t.version) for t in paged] == [("welcome", 2), ("receipt", 1)]


def test_get_paginated_templates_can_be_asked_for_every_version(service, welcome):
    service.update_template("welcome", make_update_input(template_body="v2"))

    paged = service.get_paginated_templates(1, 10, include_all_versions=True)

    assert [(t.key, t.version) for t in paged] == [("welcome", 1), ("welcome", 2)]


# ----------------------------------------------------------------------
# most_recent_active_version
# ----------------------------------------------------------------------


def test_most_recent_active_version_keeps_the_highest_active_or_draft_version(service, welcome):
    service.set_status("welcome", ACTIVE, version=1)
    service.update_template("welcome", make_update_input(template_body="v2"))

    matched = service.get_filtered_templates({"most_recent_active_version": True})

    assert [(t.key, t.version, t.status) for t in matched] == [("welcome", 2, DRAFT)]


def test_most_recent_active_version_skips_a_retired_higher_version(service, welcome):
    service.set_status("welcome", ACTIVE, version=1)
    service.update_template("welcome", make_update_input(template_body="v2"))
    service.set_status("welcome", ARCHIVED, version=2)

    matched = service.get_filtered_templates({"most_recent_active_version": True})

    assert [(t.version, t.status) for t in matched] == [(1, ACTIVE)]


def test_most_recent_active_version_false_is_the_complement(service, welcome):
    service.update_template("welcome", make_update_input(template_body="v2"))
    service.set_status("welcome", ARCHIVED, version=1)

    current = service.get_filtered_templates({"most_recent_active_version": True})
    rest = service.get_filtered_templates({"most_recent_active_version": False})

    assert [t.version for t in current] == [2]
    assert [t.version for t in rest] == [1]


def test_most_recent_active_version_combines_with_other_fields(service, welcome):
    service.create_template(make_create_input("receipt"))
    service.update_template("receipt", make_update_input(template_body="v2"))

    matched = service.get_filtered_templates({"most_recent_active_version": True, "key": "receipt"})

    assert [(t.key, t.version) for t in matched] == [("receipt", 2)]


@pytest.mark.parametrize("value", ["true", 1, None, ["yes"]])
def test_a_non_boolean_most_recent_active_version_is_rejected(service, value):
    with pytest.raises(ManagedTemplateInvalidFilterError, match="must be a boolean"):
        service.get_filtered_templates({"most_recent_active_version": value})


def test_get_paginated_filtered_templates_filters_then_pages(service, welcome):
    service.update_template("welcome", make_update_input(template_body="v2"))
    service.create_template(make_create_input("receipt"))

    paged = service.get_paginated_filtered_templates({"key": "welcome"}, page=1, page_size=1)

    assert [t.version for t in paged] == [1]


@pytest.mark.parametrize("method", ["get_paginated_templates", "get_paginated_filtered_templates"])
@pytest.mark.parametrize(("page", "page_size"), [(0, 10), (-1, 10), (1, 0), (1, -5)])
def test_pagination_bounds_are_rejected(service, method, page, page_size):
    with pytest.raises(ValueError, match="must be 1 or greater"):
        if method == "get_paginated_templates":
            service.get_paginated_templates(page, page_size)
        else:
            service.get_paginated_filtered_templates({}, page, page_size)


@pytest.mark.parametrize(
    ("call", "backend_method"),
    [
        (lambda s: s.get_filtered_templates({"kye": "welcome"}), "get_filtered_templates"),
        (
            lambda s: s.get_paginated_filtered_templates({"kye": "welcome"}, 1, 10),
            "get_paginated_filtered_templates",
        ),
    ],
)
def test_a_filter_is_validated_before_it_reaches_the_backend(
    service, backend, call, backend_method
):
    with pytest.raises(ManagedTemplateInvalidFilterError):
        call(service)

    assert backend_method not in backend.calls


# ----------------------------------------------------------------------
# Filter validation
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "filters",
    [
        {},
        {"key": "welcome"},
        {"name": {"lookup": "includes", "value": "Welcome", "case_sensitive": True}},
        {"version": 2},
        {"status": ACTIVE},
        {"created_at_range": {"from": EPOCH}},
        {"updated_at_range": {"to": EPOCH}},
        {"description": "d", "template_managed_backend": "in-memory"},
        {"and": [{"key": "a"}, {"or": [{"key": "b"}, {"not": {"key": "c"}}]}]},
        {"not": {"and": [{"key": "a"}]}},
    ],
)
def test_valid_filters_are_accepted(service, filters):
    service.validate_filter(filters)


def test_an_unknown_field_is_rejected_and_the_message_lists_the_known_ones(service):
    with pytest.raises(ManagedTemplateInvalidFilterError) as excinfo:
        service.validate_filter({"kye": "welcome"})

    message = str(excinfo.value)
    assert "unknown field(s): kye" in message
    assert "created_at_range" in message and "status" in message


def test_every_unknown_field_is_reported_at_once(service):
    with pytest.raises(ManagedTemplateInvalidFilterError, match="alpha, beta"):
        service.validate_filter({"alpha": 1, "beta": 2})


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ("not-a-dict", "must be a dict"),
        ({"and": []}, "must not be empty"),
        ({"or": []}, "must not be empty"),
        ({"and": {"key": "a"}}, "must be a list of filters"),
        ({"or": "nope"}, "must be a list of filters"),
        ({"and": [{"key": "a"}], "key": "b"}, "mixes a logical operator"),
        ({"not": {"key": "a"}, "or": [{"key": "b"}]}, "mixes a logical operator"),
    ],
)
def test_malformed_filters_are_rejected(service, filters, expected):
    with pytest.raises(ManagedTemplateInvalidFilterError, match=expected):
        service.validate_filter(filters)


@pytest.mark.parametrize(
    ("filters", "expected_path"),
    [
        ({"and": [{"key": "a"}, {"kye": "b"}]}, "filters.and[1]"),
        ({"or": [{"kye": "b"}]}, "filters.or[0]"),
        ({"not": {"kye": "b"}}, "filters.not"),
        ({"and": [{"or": [{"not": {"kye": "b"}}]}]}, "filters.and[0].or[0].not"),
    ],
)
def test_the_error_points_at_the_offending_branch(service, filters, expected_path):
    with pytest.raises(ManagedTemplateInvalidFilterError) as excinfo:
        service.validate_filter(filters)

    assert expected_path in str(excinfo.value)


def test_a_nested_non_dict_is_rejected(service):
    with pytest.raises(
        ManagedTemplateInvalidFilterError, match=r"filters\.and\[0\] must be a dict"
    ):
        service.validate_filter({"and": ["nope"]})


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------


def test_render_uses_the_latest_version_by_default(service, welcome):
    service.update_template("welcome", make_update_input(template_body="Hey {name}!"))

    rendered = service.render(make_notification("welcome"), {"name": "Hugo"})

    assert rendered.body == "Hey Hugo!"


def test_render_pins_an_explicit_version(service, welcome):
    service.update_template("welcome", make_update_input(template_body="Hey {name}!"))

    rendered = service.render(make_notification("welcome"), {"name": "Hugo"}, version=1)

    assert rendered.body == "Hi Hugo"


def test_render_produces_the_subject_too(service, welcome):
    rendered = service.render(make_notification("welcome"), {"name": "Hugo"})

    assert rendered.subject == "Hello Hugo"


def test_render_carries_the_preheader_when_the_template_has_one(service):
    service.create_template(make_create_input("receipt", template_preheader="Hi {name}, receipt"))

    rendered = service.render(make_notification("receipt"), {"name": "Hugo"})

    assert rendered.preheader == "Hi Hugo, receipt"


def test_render_bypasses_the_wrapped_renderers_own_render(service, welcome, inner_renderer):
    service.render(make_notification("welcome"), {"name": "Hugo"})

    # Going through render() would resolve the key itself and ignore `version`.
    assert inner_renderer.render_calls == []
    assert len(inner_renderer.render_from_content_calls) == 1


def test_render_raises_for_an_unknown_key(service):
    with pytest.raises(ManagedTemplateNotFoundError):
        service.render(make_notification("nope"), {})


def test_render_raises_for_an_unknown_version(service, welcome):
    with pytest.raises(ManagedTemplateNotFoundError):
        service.render(make_notification("welcome"), {}, version=99)


def test_render_can_target_an_unpublished_draft(service, welcome):
    service.activate("welcome", version=1)
    service.update_template("welcome", make_update_input(template_body="Draft {name}"))

    preview = service.render(make_notification("welcome"), {"name": "Hugo"}, version=2)

    assert preview.body == "Draft Hugo"
    assert service.get_template("welcome", 2).status is DRAFT


def test_render_template_skips_the_backend_read(service, welcome, backend):
    template = service.get_template("welcome")
    backend.calls.clear()

    rendered = service.render_template(make_notification("welcome"), template, {"name": "Ana"})

    assert rendered.body == "Hi Ana"
    assert backend.calls == []


def test_render_template_builds_content_from_the_template_it_is_given(
    service, welcome, inner_renderer
):
    service.render_template(make_notification("welcome"), welcome, {"name": "Ana"})

    content = inner_renderer.render_from_content_calls[-1]
    assert content.body_template == "Hi {name}"
    assert content.subject_template == "Hello {name}"
