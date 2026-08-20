import inspect

import pytest
from vintasend.services.notification_template_renderers.base import (
    BaseNotificationTemplateRenderer,
    TemplateContent,
)
from vintasend.services.notification_template_renderers.base_templated_email_renderer import (
    EmailTemplateContent,
)

from vintasend_managed_templates.exceptions import ManagedTemplateNotFoundError
from vintasend_managed_templates.managed_template_renderer import (
    ManagedTemplateEmailRenderer,
    ManagedTemplateRenderer,
    ManagedTemplateSMSRenderer,
)

from .fakes import (
    RecordingEmailRenderer,
    make_create_input,
    make_notification,
    make_one_off_notification,
    make_update_input,
)


def test_renderer_is_abstract():
    assert inspect.isabstract(ManagedTemplateRenderer)


def test_renderer_stores_both_collaborators(backend, inner_renderer):
    renderer = ManagedTemplateEmailRenderer(backend, inner_renderer)

    assert renderer.manager_backend is backend
    assert renderer.renderer is inner_renderer


def test_subclasses_are_concrete():
    assert not inspect.isabstract(ManagedTemplateEmailRenderer)
    assert not inspect.isabstract(ManagedTemplateSMSRenderer)


def test_subclasses_implement_the_base_seam():
    assert issubclass(ManagedTemplateRenderer, BaseNotificationTemplateRenderer)


def test_a_subclass_must_implement_create_template_content(backend, inner_renderer):
    class Incomplete(ManagedTemplateRenderer):
        pass

    with pytest.raises(TypeError, match="create_template_content"):
        Incomplete(backend, inner_renderer)  # type: ignore[abstract]


# ----------------------------------------------------------------------
# Email
# ----------------------------------------------------------------------


def test_email_content_is_built_from_the_managed_template(service, email_renderer):
    template = service.create_template(
        make_create_input("welcome", template_preheader="Peek {name}")
    )

    content = email_renderer.create_template_content(template)

    assert isinstance(content, EmailTemplateContent)
    assert content.body_template == "Hi {name}"
    assert content.subject_template == "Hello {name}"
    assert content.preheader_template == "Peek {name}"


def test_email_content_falls_back_to_an_empty_subject(service, email_renderer):
    template = service.create_template(make_create_input("no-subject", template_subject=None))

    assert email_renderer.create_template_content(template).subject_template == ""


def test_email_content_keeps_a_missing_preheader_as_none(service, email_renderer):
    template = service.create_template(make_create_input("welcome"))

    assert email_renderer.create_template_content(template).preheader_template is None


def test_email_render_resolves_the_key_through_the_backend(service, email_renderer):
    service.create_template(make_create_input("welcome"))

    rendered = email_renderer.render(make_notification("welcome"), {"name": "Hugo"})

    assert rendered.body == "Hi Hugo"
    assert rendered.subject == "Hello Hugo"


def test_email_render_always_uses_the_backends_current_version(service, email_renderer):
    service.create_template(make_create_input("welcome"))
    service.update_template("welcome", make_update_input(template_body="Hey {name}!"))

    # The renderer has no version argument -- that is exactly why the service exposes one.
    assert email_renderer.render(make_notification("welcome"), {"name": "Hugo"}).body == "Hey Hugo!"


def test_email_render_raises_for_an_unknown_key(email_renderer):
    with pytest.raises(ManagedTemplateNotFoundError):
        email_renderer.render(make_notification("nope"), {})


def test_email_render_works_for_a_one_off_notification(service, email_renderer):
    service.create_template(make_create_input("welcome"))

    rendered = email_renderer.render(make_one_off_notification("welcome"), {"name": "Ana"})

    assert rendered.body == "Hi Ana"


def test_render_from_template_content_delegates_to_the_wrapped_renderer(
    email_renderer, inner_renderer
):
    content = EmailTemplateContent(subject_template="S {name}", body_template="B {name}")

    rendered = email_renderer.render_from_template_content(
        make_notification("welcome"), content, {"name": "Hugo"}
    )

    assert rendered.body == "B Hugo"
    assert inner_renderer.render_from_content_calls == [content]


def test_render_from_template_content_forwards_extra_kwargs(backend):
    seen = {}

    class KwargRecorder(RecordingEmailRenderer):
        def render_from_template_content(self, notification, template_content, context, **kwargs):
            seen.update(kwargs)
            return super().render_from_template_content(notification, template_content, context)

    renderer = ManagedTemplateEmailRenderer(backend, KwargRecorder())
    renderer.render_from_template_content(
        make_notification("welcome"),
        EmailTemplateContent(subject_template="S", body_template="B"),
        {},
        flag=True,
    )

    assert seen == {"flag": True}


# ----------------------------------------------------------------------
# SMS
# ----------------------------------------------------------------------


def test_sms_content_carries_only_the_body(service, sms_renderer):
    template = service.create_template(
        make_create_input("alert", template_body="Code {code}", template_subject="ignored")
    )

    content = sms_renderer.create_template_content(template)

    assert type(content) is TemplateContent
    assert content.body_template == "Code {code}"


def test_sms_render_resolves_the_key_through_the_backend(service, sms_renderer):
    service.create_template(make_create_input("alert", template_body="Code {code}"))

    assert sms_renderer.render(make_notification("alert"), {"code": "123"}).body == "Code 123"
