"""Which version a send renders.

"The latest version" means two things here. The editing view -- ``get_template(key)`` -- is the
newest version whatever its status. The send path -- ``render`` on an unpinned notification,
``get_latest_template_version`` (what ``NotificationService`` pins to) and the service's
``render`` with no version and no pin -- is the newest ACTIVE version. A draft is never sent.
"""

import dataclasses

import pytest
from vintasend.services.notification_adapters.stubs.fake_adapter import FakeEmailAdapter
from vintasend.services.notification_backends.stubs.fake_backend import FakeFileBackend
from vintasend.services.notification_service import NotificationService, register_context

from vintasend_managed_templates.base_template_manager_backend import BaseTemplateManagerBackend
from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.exceptions import (
    ManagedTemplateNoActiveVersionError,
    ManagedTemplateNotFoundError,
)
from vintasend_managed_templates.lifecycle import newest_active_version
from vintasend_managed_templates.managed_template_renderer import ManagedTemplateEmailRenderer

from .fakes import (
    InMemoryTemplateManagerBackend,
    make_create_input,
    make_notification,
    make_update_input,
)


def pinned(version: int):
    return dataclasses.replace(make_notification("welcome"), requested_template_version=version)


@pytest.fixture
def active_v1_draft_v2(service):
    """``welcome`` with v1 published and v2 drafted on top of it."""
    service.create_template(make_create_input("welcome", template_body="v1 {name}"))
    service.activate("welcome", 1)
    service.update_template("welcome", make_update_input(template_body="v2 draft {name}"))


# ----------------------------------------------------------------------
# An unpinned send
# ----------------------------------------------------------------------


def test_an_unpinned_send_renders_the_active_version_not_a_newer_draft(
    active_v1_draft_v2, email_renderer
):
    email = email_renderer.render(make_notification("welcome"), {"name": "Ana"})

    assert email.body == "v1 Ana"
    assert email.template_version == 1


def test_a_key_with_only_a_draft_raises_the_no_active_version_error(service, email_renderer):
    service.create_template(make_create_input("welcome"))

    with pytest.raises(ManagedTemplateNoActiveVersionError, match="welcome"):
        email_renderer.render(make_notification("welcome"), {"name": "Ana"})


def test_the_no_active_version_error_is_a_not_found_error():
    assert issubclass(ManagedTemplateNoActiveVersionError, ManagedTemplateNotFoundError)


def test_a_key_with_nothing_stored_raises_the_plain_not_found_error(email_renderer):
    with pytest.raises(ManagedTemplateNotFoundError) as raised:
        email_renderer.render(make_notification("nowhere"), {})

    assert type(raised.value) is ManagedTemplateNotFoundError


def test_two_active_versions_render_the_higher_one(service, email_renderer):
    service.create_template(make_create_input("welcome", template_body="v1"))
    service.update_template("welcome", make_update_input(template_body="v2"))
    service.update_template("welcome", make_update_input(template_body="v3 draft"))
    # Activated out of order, so "the last one activated" and "the highest number" disagree.
    service.activate("welcome", 2)
    service.activate("welcome", 1)

    email = email_renderer.render(make_notification("welcome"), {"name": "Ana"})

    assert email.body == "v2"
    assert email.template_version == 2


def test_a_newer_version_that_was_deactivated_is_skipped(service, email_renderer):
    service.create_template(make_create_input("welcome", template_body="v1"))
    service.update_template("welcome", make_update_input(template_body="v2"))
    service.activate("welcome", 1)
    service.activate("welcome", 2)
    service.deactivate("welcome", 2)

    assert email_renderer.render(make_notification("welcome"), {"name": "Ana"}).body == "v1"


def test_a_key_whose_versions_are_all_retired_has_nothing_to_send(service, email_renderer):
    service.create_template(make_create_input("welcome"))
    service.activate("welcome", 1)
    service.archive("welcome", 1)

    with pytest.raises(ManagedTemplateNoActiveVersionError):
        email_renderer.render(make_notification("welcome"), {"name": "Ana"})


def test_the_sms_renderer_resolves_the_same_way(service, sms_renderer):
    service.create_template(make_create_input("alert", template_body="v1 {code}"))
    service.activate("alert", 1)
    service.update_template("alert", make_update_input(template_body="v2 draft {code}"))

    assert sms_renderer.render(make_notification("alert"), {"code": "7"}).body == "v1 7"


# ----------------------------------------------------------------------
# A pinned send
# ----------------------------------------------------------------------


def test_a_pinned_send_renders_its_version_even_when_it_is_a_draft(
    active_v1_draft_v2, email_renderer
):
    assert email_renderer.render(pinned(2), {"name": "Ana"}).body == "v2 draft Ana"


def test_a_deactivated_pinned_version_still_renders(active_v1_draft_v2, service, email_renderer):
    service.activate("welcome", 2)
    service.deactivate("welcome", 1)

    email = email_renderer.render(pinned(1), {"name": "Ana"})

    assert email.body == "v1 Ana"
    assert email.template_version == 1


def test_an_archived_pinned_version_still_renders(active_v1_draft_v2, service, email_renderer):
    service.archive("welcome", 1)

    assert email_renderer.render(pinned(1), {"name": "Ana"}).body == "v1 Ana"


# ----------------------------------------------------------------------
# Pinning at creation: get_latest_template_version
# ----------------------------------------------------------------------


def test_pinning_answers_the_active_version_not_a_newer_draft(active_v1_draft_v2, email_renderer):
    assert email_renderer.get_latest_template_version("welcome") == 1


def test_pinning_answers_none_for_a_key_with_only_drafts(service, email_renderer):
    service.create_template(make_create_input("welcome"))

    assert email_renderer.get_latest_template_version("welcome") is None


def test_pinning_answers_the_highest_active_version(service, email_renderer):
    service.create_template(make_create_input("welcome"))
    service.update_template("welcome", make_update_input(template_body="v2"))
    service.activate("welcome", 2)
    service.activate("welcome", 1)

    assert email_renderer.get_latest_template_version("welcome") == 2


def test_a_notification_created_with_pinning_records_the_active_version(
    active_v1_draft_v2, backend, email_renderer, tmp_path
):
    """End to end through ``NotificationService``, which is what calls the pinning hook."""
    register_context("managed_templates_pinning_test")(lambda: {"name": "Ana"})
    notification_backend = FakeFileBackend(database_file_name=str(tmp_path / "notifications.json"))
    adapter = FakeEmailAdapter(template_renderer=email_renderer, backend=notification_backend)
    notifications = NotificationService(
        notification_adapters=[adapter],
        notification_backend=notification_backend,
        pin_template_versions=True,
    )

    notification = notifications.create_notification(
        user_id=1,
        notification_type="EMAIL",
        title="Welcome",
        body_template="welcome",
        context_name="managed_templates_pinning_test",
        context_kwargs={},
    )

    assert notification.requested_template_version == 1
    sent, _context, _attachments = adapter.sent_emails[0]
    assert sent.requested_template_version == 1
    assert notifications.get_notification(notification.id).used_template_version == 1


# ----------------------------------------------------------------------
# The editing view and the service
# ----------------------------------------------------------------------


def test_the_editing_view_still_reads_the_newest_version_whatever_its_status(
    active_v1_draft_v2, service
):
    assert service.get_template("welcome").version == 2


def test_the_service_answers_the_active_template(active_v1_draft_v2, service):
    assert service.get_active_template("welcome").version == 1


def test_the_service_tells_a_missing_key_apart_from_one_with_no_active_version(service):
    service.create_template(make_create_input("welcome"))

    with pytest.raises(ManagedTemplateNoActiveVersionError):
        service.get_active_template("welcome")
    with pytest.raises(ManagedTemplateNotFoundError) as raised:
        service.get_active_template("nowhere")
    assert type(raised.value) is ManagedTemplateNotFoundError


def test_the_service_renders_the_active_version_when_neither_a_version_nor_a_pin_is_given(
    active_v1_draft_v2, service
):
    email = service.render(make_notification("welcome"), {"name": "Ana"})

    assert email.body == "v1 Ana"
    assert email.template_version == 1


def test_the_service_still_previews_a_draft_by_explicit_version(active_v1_draft_v2, service):
    assert service.render(make_notification("welcome"), {"name": "Ana"}, version=2).body == (
        "v2 draft Ana"
    )


# ----------------------------------------------------------------------
# The seam: get_active_template
# ----------------------------------------------------------------------


def test_the_seam_method_is_not_abstract():
    assert "get_active_template" not in BaseTemplateManagerBackend.__abstractmethods__


def test_a_backend_without_its_own_get_active_template_is_answered_through_the_filter_seam(
    active_v1_draft_v2, backend
):
    """The in-memory fake does not override ``get_active_template``: the default answers."""
    assert "get_active_template" not in vars(InMemoryTemplateManagerBackend)
    backend.calls.clear()

    assert backend.get_active_template("welcome").version == 1
    assert "get_filtered_templates" in backend.calls


def test_the_default_ignores_rows_a_backend_returns_for_another_key_or_status():
    """A backend that drops part of the filter must not make another key's row the answer."""

    class IgnoresFilters(InMemoryTemplateManagerBackend):
        def get_filtered_templates(self, filters):
            return self.get_all_templates()

    sloppy = IgnoresFilters()
    sloppy.create_template(make_create_input("other"))
    sloppy.update_template("other", make_update_input(template_body="other v2"))
    sloppy.create_template_status_update("other", 2, ManagedTemplateStatus.ACTIVE)
    sloppy.create_template(make_create_input("welcome"))
    sloppy.update_template("welcome", make_update_input(template_body="v2 draft"))
    sloppy.create_template_status_update("welcome", 1, ManagedTemplateStatus.ACTIVE)
    sloppy.create_template(make_create_input("unpublished"))

    assert sloppy.get_active_template("welcome").version == 1
    with pytest.raises(ManagedTemplateNoActiveVersionError):
        sloppy.get_active_template("unpublished")


def test_the_renderer_uses_a_backends_own_get_active_template(inner_renderer):
    class NativeActive(InMemoryTemplateManagerBackend):
        def get_active_template(self, template_key):
            self.calls.append("native_get_active_template")
            return super().get_active_template(template_key)

    native = NativeActive()
    native.create_template(make_create_input("welcome", template_body="v1", template_subject="s"))
    native.create_template_status_update("welcome", 1, ManagedTemplateStatus.ACTIVE)
    renderer = ManagedTemplateEmailRenderer(native, inner_renderer)

    assert renderer.render(make_notification("welcome"), {}).body == "v1"
    assert "native_get_active_template" in native.calls


def test_newest_active_version_compares_version_numbers_not_input_order(backend):
    backend.create_template(make_create_input("welcome"))
    backend.update_template("welcome", make_update_input(template_body="v2"))
    backend.update_template("welcome", make_update_input(template_body="v3"))
    for version in (1, 2, 3):
        backend.create_template_status_update("welcome", version, ManagedTemplateStatus.ACTIVE)
    newest_first = sorted(backend.templates, key=lambda template: -template.version)

    assert newest_active_version(newest_first).version == 3
    assert newest_active_version([]) is None
