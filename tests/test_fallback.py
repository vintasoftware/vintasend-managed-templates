"""Registered defaults for keys that have nothing published yet.

The fallback applies only to ``render`` (the send path), only to an unpinned notification, only
when the lookup of the notification's own key fails with ``ManagedTemplateNotFoundError`` (which
includes a key holding only drafts or retired versions), and only to a registered key.
"""

import collections
import dataclasses
import logging

import pytest
from vintasend.services.notification_template_renderers.base_templated_email_renderer import (
    TemplatedEmail,
)

from vintasend_managed_templates.exceptions import (
    ManagedTemplateCompositionReferenceError,
    ManagedTemplateNoActiveVersionError,
    ManagedTemplateNotFoundError,
)
from vintasend_managed_templates.managed_template_renderer import (
    FallbackTemplate,
    ManagedTemplateEmailRenderer,
    ManagedTemplateFallback,
    ManagedTemplateSMSRenderer,
)
from vintasend_managed_templates.managed_template_service import ManagedTemplateService

from .fakes import (
    RecordingEmailRenderer,
    RecordingSMSRenderer,
    make_create_input,
    make_notification,
)


WELCOME_DEFAULT = FallbackTemplate(
    subject_template="emails/welcome/subject.txt",
    body_template="emails/welcome/body.html",
    preheader_template="emails/welcome/preheader.txt",
)


class FileRenderer(RecordingEmailRenderer):
    """Stands in for a file-based renderer: ``render`` reads the paths off the notification."""

    def __init__(self) -> None:
        super().__init__()
        self.rendered: list = []

    def render(self, notification, context):
        self.rendered.append(notification)
        email = TemplatedEmail(
            subject=f"file:{notification.subject_template}",
            body=f"file:{notification.body_template}:{context.get('name')}",
            preheader=f"file:{notification.preheader_template}",
        )
        # A file renderer has no version; one that claims one must not have it recorded.
        email.template_version = 99
        return email


@pytest.fixture
def file_renderer() -> FileRenderer:
    return FileRenderer()


@pytest.fixture
def renderer(backend, inner_renderer, file_renderer) -> ManagedTemplateEmailRenderer:
    return ManagedTemplateEmailRenderer(
        backend,
        inner_renderer,
        fallback=ManagedTemplateFallback(
            templates={"welcome": WELCOME_DEFAULT}, renderer=file_renderer
        ),
    )


# ----------------------------------------------------------------------
# A key with nothing stored
# ----------------------------------------------------------------------


def test_a_missing_key_renders_the_registered_default_through_the_fallback_renderer(
    renderer, file_renderer
):
    email = renderer.render(make_notification("welcome"), {"name": "Ana"})

    assert email.subject == "file:emails/welcome/subject.txt"
    assert email.body == "file:emails/welcome/body.html:Ana"
    assert email.preheader == "file:emails/welcome/preheader.txt"
    assert len(file_renderer.rendered) == 1


def test_a_fallback_render_reports_no_template_version(renderer):
    """So ``NotificationService`` leaves ``used_template_version`` null."""
    assert renderer.render(make_notification("welcome"), {"name": "Ana"}).template_version is None


def test_the_fallback_renderer_defaults_to_the_inner_renderer(backend, inner_renderer):
    renderer = ManagedTemplateEmailRenderer(
        backend,
        inner_renderer,
        fallback=ManagedTemplateFallback(templates={"welcome": WELCOME_DEFAULT}),
    )

    email = renderer.render(make_notification("welcome"), {"name": "Ana"})

    assert inner_renderer.render_calls == ["emails/welcome/body.html"]
    assert email.body == "from-render"
    assert email.template_version is None


def test_unset_template_fields_become_empty_strings(backend, file_renderer, inner_renderer):
    renderer = ManagedTemplateEmailRenderer(
        backend,
        inner_renderer,
        fallback=ManagedTemplateFallback(
            templates={"welcome": FallbackTemplate(None, "body.html")}, renderer=file_renderer
        ),
    )

    renderer.render(make_notification("welcome"), {})

    rendered = file_renderer.rendered[0]
    assert (rendered.subject_template, rendered.preheader_template) == ("", "")


def test_the_notification_it_was_given_is_not_mutated(renderer, file_renderer):
    notification = make_notification("welcome")
    before = dataclasses.asdict(notification)

    renderer.render(notification, {"name": "Ana"})

    assert dataclasses.asdict(notification) == before
    assert file_renderer.rendered[0] is not notification
    assert file_renderer.rendered[0].id == notification.id


def test_an_unregistered_key_raises(renderer):
    with pytest.raises(ManagedTemplateNotFoundError):
        renderer.render(make_notification("unregistered"), {})


def test_no_fallback_configured_raises(email_renderer):
    with pytest.raises(ManagedTemplateNotFoundError):
        email_renderer.render(make_notification("welcome"), {})


def test_a_mapping_with_a_default_still_only_answers_registered_keys(
    backend, inner_renderer, file_renderer
):
    templates = collections.defaultdict(lambda: WELCOME_DEFAULT, {"welcome": WELCOME_DEFAULT})
    renderer = ManagedTemplateEmailRenderer(
        backend,
        inner_renderer,
        fallback=ManagedTemplateFallback(templates=templates, renderer=file_renderer),
    )

    with pytest.raises(ManagedTemplateNotFoundError):
        renderer.render(make_notification("anything-else"), {})
    assert renderer.get_fallback_template("anything-else") is None


def test_falling_back_logs_the_key_and_notification_id_but_never_the_context(renderer, caplog):
    secret = "a-value-that-must-not-reach-the-log"

    with caplog.at_level(logging.DEBUG, logger="vintasend_managed_templates"):
        renderer.render(make_notification("welcome"), {"name": secret})

    messages = [record.getMessage() for record in caplog.records]
    assert any("'welcome'" in message and "notification 1" in message for message in messages)
    assert not any(secret in message for message in messages)
    assert not any(secret in repr(record.args) for record in caplog.records)


# ----------------------------------------------------------------------
# A key with stored versions
# ----------------------------------------------------------------------


def test_a_key_with_only_a_draft_counts_as_not_customized_and_falls_back(service, renderer):
    service.create_template(make_create_input("welcome", template_body="draft {name}"))

    assert renderer.render(make_notification("welcome"), {"name": "Ana"}).body == (
        "file:emails/welcome/body.html:Ana"
    )


def test_a_key_whose_versions_are_all_retired_falls_back(service, renderer):
    service.create_template(make_create_input("welcome"))
    service.activate("welcome", 1)
    service.archive("welcome", 1)

    assert renderer.render(make_notification("welcome"), {"name": "Ana"}).body.startswith("file:")


def test_a_published_version_is_rendered_instead_of_the_default(service, renderer, file_renderer):
    service.create_template(make_create_input("welcome", template_body="stored {name}"))
    service.activate("welcome", 1)

    email = renderer.render(make_notification("welcome"), {"name": "Ana"})

    assert email.body == "stored Ana"
    assert email.template_version == 1
    assert file_renderer.rendered == []


def test_a_stored_template_extending_a_missing_base_raises_rather_than_falling_back(
    service, renderer, file_renderer
):
    service.create_template(
        make_create_input("welcome", template_body='{% managed_extends "gone" %}Hi')
    )
    service.activate("welcome", 1)

    with pytest.raises(ManagedTemplateCompositionReferenceError, match="gone"):
        renderer.render(make_notification("welcome"), {"name": "Ana"})
    assert file_renderer.rendered == []


def test_an_error_that_is_not_a_not_found_error_is_not_caught(backend, renderer, file_renderer):
    class StoreDownError(RuntimeError):
        pass

    def explode(template_key):
        raise StoreDownError("store is down")

    backend.get_active_template = explode  # type: ignore[method-assign]

    with pytest.raises(StoreDownError):
        renderer.render(make_notification("welcome"), {})
    assert file_renderer.rendered == []


# ----------------------------------------------------------------------
# A pinned notification
# ----------------------------------------------------------------------


def test_a_missing_pinned_version_raises_rather_than_falling_back(service, renderer, file_renderer):
    service.create_template(make_create_input("welcome"))
    notification = dataclasses.replace(make_notification("welcome"), requested_template_version=7)

    with pytest.raises(ManagedTemplateNotFoundError):
        renderer.render(notification, {"name": "Ana"})
    assert file_renderer.rendered == []


def test_a_pinned_notification_for_a_missing_key_raises(renderer, file_renderer):
    notification = dataclasses.replace(make_notification("welcome"), requested_template_version=1)

    with pytest.raises(ManagedTemplateNotFoundError):
        renderer.render(notification, {"name": "Ana"})
    assert file_renderer.rendered == []


# ----------------------------------------------------------------------
# The paths that never fall back
# ----------------------------------------------------------------------


def test_pinning_still_answers_none_for_a_key_with_only_a_default(renderer):
    assert renderer.get_latest_template_version("welcome") is None


def test_the_services_render_never_falls_back(backend, renderer, file_renderer):
    service = ManagedTemplateService(backend, renderer)

    with pytest.raises(ManagedTemplateNotFoundError):
        service.render(make_notification("welcome"), {"name": "Ana"})
    service.create_template(make_create_input("welcome"))
    with pytest.raises(ManagedTemplateNoActiveVersionError):
        service.render(make_notification("welcome"), {"name": "Ana"})
    assert file_renderer.rendered == []


# ----------------------------------------------------------------------
# get_fallback_template
# ----------------------------------------------------------------------


def test_get_fallback_template_exposes_the_registered_default(renderer):
    assert renderer.get_fallback_template("welcome") == WELCOME_DEFAULT


def test_get_fallback_template_answers_none_for_an_unregistered_key_or_no_fallback(
    renderer, email_renderer
):
    assert renderer.get_fallback_template("unregistered") is None
    assert renderer.get_fallback_template("__class__") is None
    assert email_renderer.get_fallback_template("welcome") is None


def test_the_registered_templates_are_copied_when_the_renderer_is_built(backend, inner_renderer):
    templates = {"welcome": WELCOME_DEFAULT}
    renderer = ManagedTemplateEmailRenderer(
        backend, inner_renderer, fallback=ManagedTemplateFallback(templates=templates)
    )

    templates["late"] = WELCOME_DEFAULT

    assert renderer.get_fallback_template("late") is None


# ----------------------------------------------------------------------
# SMS
# ----------------------------------------------------------------------


def test_the_sms_renderer_falls_back_the_same_way(backend):
    class SMSFileRenderer(RecordingSMSRenderer):
        def render(self, notification, context):
            return TemplatedEmail(subject="", body=f"file:{notification.body_template}")

    renderer = ManagedTemplateSMSRenderer(
        backend,
        RecordingSMSRenderer(),
        fallback=ManagedTemplateFallback(
            templates={"alert": FallbackTemplate(None, "sms/alert.txt")},
            renderer=SMSFileRenderer(),
        ),
    )

    sms = renderer.render(make_notification("alert"), {"code": "7"})

    assert sms.body == "file:sms/alert.txt"
    assert sms.template_version is None
