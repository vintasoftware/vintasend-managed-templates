"""Only a version that was never published can be deleted, and status history is never deleted.

A published version may have rendered a notification that is pinned to it, and its status
history records who published it. The service enforces the rule for every backend; this suite's
in-memory backend enforces it as well, under an option of the same name, the way a real backend
should.
"""

import pytest

from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.exceptions import (
    ManagedTemplateDeletionNotAllowedError,
    ManagedTemplateError,
    ManagedTemplateNotFoundError,
)
from vintasend_managed_templates.lifecycle import (
    assert_template_version_deletable,
    is_template_version_deletable,
)
from vintasend_managed_templates.managed_template_service import ManagedTemplateService

from .fakes import InMemoryTemplateManagerBackend, make_create_input, make_update_input


PUBLISHED_STATUSES = [
    pytest.param([ManagedTemplateStatus.ACTIVE], id="active"),
    pytest.param([ManagedTemplateStatus.ACTIVE, ManagedTemplateStatus.INACTIVE], id="inactive"),
    pytest.param([ManagedTemplateStatus.ARCHIVED], id="archived"),
]


def _move(service, version, statuses):
    for status in statuses:
        service.set_status("welcome", status, version)


class PermissiveBackend(InMemoryTemplateManagerBackend):
    """A backend that deletes whatever it is asked to, like one written before the rule."""

    def __init__(self) -> None:
        super().__init__(allow_deleting_published_versions=True)


# ----------------------------------------------------------------------
# Through the service
# ----------------------------------------------------------------------


def test_deleting_a_never_published_draft_is_allowed(service, welcome):
    service.delete_template("welcome", 1)

    with pytest.raises(ManagedTemplateNotFoundError):
        service.get_template("welcome", 1)


@pytest.mark.parametrize("statuses", PUBLISHED_STATUSES)
def test_deleting_a_published_version_by_number_is_refused(service, welcome, statuses):
    _move(service, 1, statuses)

    with pytest.raises(ManagedTemplateDeletionNotAllowedError, match="archive"):
        service.delete_template("welcome", 1)

    assert service.get_template("welcome", 1).status is statuses[-1]


@pytest.mark.parametrize("statuses", PUBLISHED_STATUSES)
def test_deleting_with_no_version_is_refused_when_the_latest_is_published(
    service, welcome, statuses
):
    _move(service, 1, statuses)

    with pytest.raises(ManagedTemplateDeletionNotAllowedError):
        service.delete_template("welcome")

    assert service.get_template("welcome").version == 1


def test_a_draft_that_was_once_active_is_refused(service, welcome):
    _move(service, 1, [ManagedTemplateStatus.ACTIVE])
    # The default lifecycle never moves a version back to DRAFT, but a host may allow it.
    service.validate_status_transitions = False
    _move(service, 1, [ManagedTemplateStatus.DRAFT])
    assert service.get_template("welcome", 1).status is ManagedTemplateStatus.DRAFT

    with pytest.raises(ManagedTemplateDeletionNotAllowedError):
        service.delete_template("welcome", 1)


def test_the_check_runs_against_the_version_it_deletes(service, welcome, backend):
    """With no version, the latest is resolved once, checked, and that exact one deleted."""
    service.update_template("welcome", make_update_input(template_body="v2"))
    backend.calls.clear()

    service.delete_template("welcome")

    assert [t.version for t in service.get_template_versions("welcome")] == [1]


def test_a_missing_version_is_still_reported_as_not_found(service, welcome):
    with pytest.raises(ManagedTemplateNotFoundError):
        service.delete_template("welcome", 9)
    with pytest.raises(ManagedTemplateNotFoundError):
        service.delete_template("nowhere")


def test_the_service_enforces_the_rule_for_a_backend_that_does_not(email_renderer):
    backend = PermissiveBackend()
    service = ManagedTemplateService(backend, email_renderer)
    service.create_template(make_create_input("welcome"))
    service.activate("welcome", 1)

    with pytest.raises(ManagedTemplateDeletionNotAllowedError):
        service.delete_template("welcome", 1)

    assert "delete_template" not in backend.calls


def test_a_refused_delete_keeps_the_status_history(service, welcome):
    service.activate("welcome", 1, changed_by="publisher")

    with pytest.raises(ManagedTemplateDeletionNotAllowedError):
        service.delete_template("welcome", 1)

    assert [(r.status, r.created_by) for r in service.get_status_history("welcome", 1)] == [
        (ManagedTemplateStatus.ACTIVE, "publisher")
    ]


def test_a_published_version_is_deleted_only_when_both_service_and_backend_opt_in(
    email_renderer,
):
    strict_backend = InMemoryTemplateManagerBackend()
    opted_in_service = ManagedTemplateService(
        strict_backend, email_renderer, allow_deleting_published_versions=True
    )
    opted_in_service.create_template(make_create_input("welcome"))
    opted_in_service.activate("welcome", 1)

    # The service lets it through; the backend still refuses on its own.
    with pytest.raises(ManagedTemplateDeletionNotAllowedError):
        opted_in_service.delete_template("welcome", 1)

    both = ManagedTemplateService(
        PermissiveBackend(), email_renderer, allow_deleting_published_versions=True
    )
    both.create_template(make_create_input("welcome"))
    both.activate("welcome", 1, changed_by="publisher")
    both.delete_template("welcome", 1)

    with pytest.raises(ManagedTemplateNotFoundError):
        both.get_template("welcome", 1)
    # Even a permitted hard delete leaves the status history in place.
    assert [r.created_by for r in both.get_status_history("welcome", 1)] == ["publisher"]


def test_the_service_option_is_off_by_default(service):
    assert service.allow_deleting_published_versions is False


def test_the_error_is_a_managed_template_error_but_not_a_not_found_error():
    assert issubclass(ManagedTemplateDeletionNotAllowedError, ManagedTemplateError)
    assert not issubclass(ManagedTemplateDeletionNotAllowedError, ManagedTemplateNotFoundError)


# ----------------------------------------------------------------------
# The helpers
# ----------------------------------------------------------------------


def _history(backend, key="welcome", version=1):
    return list(backend.get_template_status_history(key, version))


def test_a_draft_with_no_history_or_only_draft_entries_is_deletable(backend, service, welcome):
    assert is_template_version_deletable(welcome, _history(backend)) is True

    backend.create_template_status_update("welcome", 1, ManagedTemplateStatus.DRAFT)

    assert is_template_version_deletable(service.get_template("welcome", 1), _history(backend))


@pytest.mark.parametrize("statuses", PUBLISHED_STATUSES)
def test_a_version_that_is_not_a_draft_is_not_deletable(backend, service, welcome, statuses):
    _move(service, 1, statuses)

    assert not is_template_version_deletable(service.get_template("welcome", 1), _history(backend))


def test_history_belonging_to_another_version_is_ignored(backend, service, welcome):
    service.update_template("welcome", make_update_input(template_body="v2"))
    service.activate("welcome", 1)
    v2 = service.get_template("welcome", 2)

    # Handed the whole key's trail, the v1 entry must not count against v2.
    assert is_template_version_deletable(v2, list(backend.get_template_status_history("welcome")))


def test_assert_raises_the_deletion_error_naming_the_version(backend, service, welcome):
    service.activate("welcome", 1)

    with pytest.raises(ManagedTemplateDeletionNotAllowedError, match="'welcome' v1"):
        assert_template_version_deletable(service.get_template("welcome", 1), _history(backend))


def test_assert_returns_quietly_for_a_deletable_version(backend, welcome):
    assert_template_version_deletable(welcome, _history(backend))


# ----------------------------------------------------------------------
# The in-memory backend on its own
# ----------------------------------------------------------------------


def test_the_backend_refuses_a_published_version_by_default(backend, service, welcome):
    service.activate("welcome", 1)

    with pytest.raises(ManagedTemplateDeletionNotAllowedError):
        backend.delete_template("welcome", 1)


def test_the_backend_deletes_a_never_published_draft(backend, welcome):
    backend.delete_template("welcome", 1)

    assert backend.templates == []


def test_a_new_version_never_reuses_a_deleted_versions_number(email_renderer):
    service = ManagedTemplateService(
        PermissiveBackend(), email_renderer, allow_deleting_published_versions=True
    )
    service.create_template(make_create_input("welcome"))
    service.update_template("welcome", make_update_input(template_body="v2"))
    service.activate("welcome", 2)
    service.delete_template("welcome", 2)

    drafted = service.update_template("welcome", make_update_input(template_body="v3"))

    assert drafted.version == 3
    assert service.get_status_history("welcome", 3) == []
