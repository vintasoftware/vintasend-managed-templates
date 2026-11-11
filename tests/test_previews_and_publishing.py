"""Previewing a template, publishing it safely, and which version counts as current.

* **Parts.** A base or fragment named without ``version=`` resolves to its newest version,
  drafts included, under ``"latest"`` (previews, and the service's own composer), and to its
  newest ACTIVE version under ``"published"`` (every send, through the renderer's composer).
* **``render_standalone``** renders a template with no notification, under either rule.
* **The render check** on ``activate`` / ``set_status`` renders the version with its bases and
  fragments at their newest published versions before anything is written, and writes nothing
  when it fails.
* **``get_current_template_version``** is the newest active version unless that is a base, and
  ``get_latest_template_version`` -- what vintasend pins with -- answers the same.
"""

import dataclasses
import datetime

import pytest
from vintasend.services.dataclasses import Notification
from vintasend.services.notification_adapters.stubs.fake_adapter import FakeEmailAdapter
from vintasend.services.notification_backends.stubs.fake_backend import FakeFileBackend
from vintasend.services.notification_service import NotificationService

from vintasend_managed_templates.composition import TemplateComposer, part_resolver
from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.exceptions import (
    ManagedTemplateCompositionReferenceError,
    ManagedTemplateError,
    ManagedTemplateNoActiveVersionError,
    ManagedTemplateNotFoundError,
    ManagedTemplateRenderCheckError,
    ManagedTemplateStatusTransitionError,
)
from vintasend_managed_templates.managed_template_renderer import (
    FallbackTemplate,
    ManagedTemplateEmailRenderer,
    ManagedTemplateFallback,
    standalone_notification,
)
from vintasend_managed_templates.managed_template_service import (
    ManagedTemplateService,
    RenderCheck,
)

from .fakes import (
    InMemoryTemplateManagerBackend,
    RecordingEmailRenderer,
    make_create_input,
    make_notification,
    make_update_input,
    publish,
)


class NotificationRecordingRenderer(RecordingEmailRenderer):
    """Also records the notification each ``render_from_template_content`` call was handed."""

    def __init__(self) -> None:
        super().__init__()
        self.notifications: list = []

    def render_from_template_content(self, notification, template_content, context, **kwargs):
        self.notifications.append(notification)
        return super().render_from_template_content(
            notification, template_content, context, **kwargs
        )


@pytest.fixture
def base_active_v1_draft_v2(backend):
    """``base`` with v1 published and v2 drafted on top of it, and ``welcome`` extending it."""
    backend.create_template(make_create_input("base", template_body="[[{% managed_children %}]]"))
    publish(backend, "base", 1)
    backend.update_template(
        "base", make_update_input(template_body="((draft {% managed_children %}))")
    )
    backend.create_template(
        make_create_input("welcome", template_body='{% managed_extends "base" %}Hi {name}')
    )
    return backend.get_template("welcome", 1)


@pytest.fixture
def draft_base(backend):
    """``welcome`` (v1, DRAFT) extending ``base``, which has never been published."""
    backend.create_template(make_create_input("base", template_body="[[{% managed_children %}]]"))
    backend.create_template(
        make_create_input("welcome", template_body='{% managed_extends "base" %}Hi {name}')
    )
    return backend.get_template("welcome", 1)


def writes(backend: InMemoryTemplateManagerBackend) -> list[str]:
    """The backend calls that change something, in order."""
    return [call for call in backend.calls if not call.startswith("get_")]


# ----------------------------------------------------------------------
# Part resolution
# ----------------------------------------------------------------------


def test_latest_resolves_an_unpinned_part_to_its_newest_version_drafts_included(
    base_active_v1_draft_v2, backend
):
    assert part_resolver(backend, "latest")("base", None).version == 2


def test_published_resolves_an_unpinned_part_to_its_newest_active_version(
    base_active_v1_draft_v2, backend
):
    assert part_resolver(backend, "published")("base", None).version == 1


@pytest.mark.parametrize("parts", ["latest", "published"])
def test_a_pinned_part_resolves_to_exactly_that_version_either_way(
    base_active_v1_draft_v2, backend, parts
):
    assert part_resolver(backend, parts)("base", 2).version == 2


def test_an_unknown_part_resolution_is_refused(backend):
    with pytest.raises(ValueError, match="parts must be one of latest, published"):
        part_resolver(backend, "newest")  # type: ignore[arg-type]


def test_a_composer_from_a_backend_resolves_latest_by_default(base_active_v1_draft_v2, backend):
    composed = TemplateComposer.from_backend(backend).compose(base_active_v1_draft_v2)

    assert composed.body_template == "((draft Hi {name}))"


def test_a_composer_from_a_backend_can_resolve_published(base_active_v1_draft_v2, backend):
    composer = TemplateComposer.from_backend(backend, parts="published")

    assert composer.compose(base_active_v1_draft_v2).body_template == "[[Hi {name}]]"


def test_a_part_with_nothing_published_fails_to_compose_under_published(draft_base, backend):
    composer = TemplateComposer.from_backend(backend, parts="published")

    with pytest.raises(ManagedTemplateCompositionReferenceError) as raised:
        composer.compose(draft_base)

    assert "'base', which has no active version" in str(raised.value)
    assert isinstance(raised.value, ManagedTemplateNotFoundError)
    assert isinstance(raised.value.__cause__, ManagedTemplateNoActiveVersionError)


def test_a_part_that_does_not_exist_still_says_so_under_published(backend):
    backend.create_template(make_create_input("welcome", template_body='{% managed_include "x" %}'))
    composer = TemplateComposer.from_backend(backend, parts="published")

    with pytest.raises(ManagedTemplateCompositionReferenceError, match="'x', which does not exist"):
        composer.compose(backend.get_template("welcome"))


def test_with_resolver_keeps_prefix_depth_and_subclass_and_leaves_the_original_alone(backend):
    class MyComposer(TemplateComposer):
        pass

    original = MyComposer(backend.get_template, tag_prefix="cms_", max_depth=3)
    resolver = part_resolver(backend, "published")

    switched = original.with_resolver(resolver)

    assert type(switched) is MyComposer
    assert (switched.tag_prefix, switched.max_depth) == ("cms_", 3)
    assert switched.resolve_template is resolver
    assert original.resolve_template == backend.get_template


# ----------------------------------------------------------------------
# Sends compose published parts
# ----------------------------------------------------------------------


def test_a_send_composes_a_part_at_its_newest_active_version_not_a_newer_draft(
    base_active_v1_draft_v2, backend, email_renderer
):
    publish(backend, "welcome")

    email = email_renderer.render(make_notification("welcome"), {"name": "Ana"})

    assert email.body == "[[Hi Ana]]"


def test_a_pinned_send_still_composes_published_parts(base_active_v1_draft_v2, email_renderer):
    notification = dataclasses.replace(make_notification("welcome"), requested_template_version=1)

    assert email_renderer.render(notification, {"name": "Ana"}).body == "[[Hi Ana]]"


def test_a_send_whose_part_has_nothing_published_fails_and_skips_the_fallback(
    draft_base, backend, inner_renderer
):
    publish(backend, "welcome")
    renderer = ManagedTemplateEmailRenderer(
        backend,
        inner_renderer,
        fallback=ManagedTemplateFallback(
            templates={"welcome": FallbackTemplate(subject_template="s", body_template="b")}
        ),
    )

    with pytest.raises(ManagedTemplateCompositionReferenceError, match="no active version"):
        renderer.render(make_notification("welcome"), {"name": "Ana"})
    assert inner_renderer.render_calls == []


def test_a_renderer_can_keep_composing_sends_against_the_latest_parts(
    base_active_v1_draft_v2, backend, inner_renderer
):
    publish(backend, "welcome")
    renderer = ManagedTemplateEmailRenderer(backend, inner_renderer, composer_parts="latest")

    email = renderer.render(make_notification("welcome"), {"name": "Ana"})

    assert email.body == "((draft Hi Ana))"


def test_a_renderer_given_its_own_composer_resolves_parts_as_that_composer_does(
    base_active_v1_draft_v2, backend, inner_renderer
):
    publish(backend, "welcome")
    renderer = ManagedTemplateEmailRenderer(
        backend, inner_renderer, composer=TemplateComposer.from_backend(backend)
    )

    assert renderer.render(make_notification("welcome"), {"name": "Ana"}).body == (
        "((draft Hi Ana))"
    )


def test_the_services_own_composer_still_resolves_latest(base_active_v1_draft_v2, service):
    assert service.get_composed_template("welcome").body_template == "((draft Hi {name}))"
    service.validate_composition(service.get_template("welcome"))


def test_service_render_composes_published_parts_and_a_latest_preview_finds_the_draft(
    base_active_v1_draft_v2, backend, service
):
    """``service.render`` composes through the renderer, as a send does; a preview opts in."""
    publish(backend, "welcome")
    template = service.get_template("welcome")

    assert service.render(make_notification("welcome"), {"name": "Ana"}).body == "[[Hi Ana]]"
    assert service.render(make_notification("welcome"), {"name": "Ana"}, version=1).body == (
        "[[Hi Ana]]"
    )
    pinned = dataclasses.replace(make_notification("welcome"), requested_template_version=1)
    assert service.render(pinned, {"name": "Ana"}).body == "[[Hi Ana]]"
    assert service.render_standalone(template, {"name": "Ana"}, parts="latest").body == (
        "((draft Hi Ana))"
    )


def test_service_render_follows_a_renderer_built_to_compose_latest_parts(
    base_active_v1_draft_v2, backend, inner_renderer
):
    publish(backend, "welcome")
    renderer = ManagedTemplateEmailRenderer(backend, inner_renderer, composer_parts="latest")
    service = ManagedTemplateService(backend, renderer)

    assert service.render(make_notification("welcome"), {"name": "Ana"}).body == (
        "((draft Hi Ana))"
    )


# ----------------------------------------------------------------------
# render_standalone
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("parts", "body"), [("latest", "((draft Hi Ana))"), ("published", "[[Hi Ana]]")]
)
def test_the_service_renders_a_template_standalone_under_either_rule(
    base_active_v1_draft_v2, service, parts, body
):
    email = service.render_standalone(base_active_v1_draft_v2, {"name": "Ana"}, parts=parts)

    assert email.body == body
    assert email.subject == "Hello Ana"
    assert email.template_version == 1


@pytest.mark.parametrize(
    ("parts", "body"), [("latest", "((draft Hi Ana))"), ("published", "[[Hi Ana]]")]
)
def test_the_renderer_renders_a_template_standalone_under_either_rule(
    base_active_v1_draft_v2, email_renderer, parts, body
):
    email = email_renderer.render_standalone(base_active_v1_draft_v2, {"name": "Ana"}, parts=parts)

    assert email.body == body
    assert email.template_version == 1


def test_a_standalone_render_needs_no_version_to_be_published(draft_base, service):
    """A draft renders standalone as it is: previews are how a draft gets reviewed."""
    assert service.render_standalone(draft_base, {"name": "Ana"}, parts="latest").body == (
        "[[Hi Ana]]"
    )


def test_a_standalone_render_of_an_unsaved_edit_renders_the_edit(
    base_active_v1_draft_v2, service, backend
):
    edited = dataclasses.replace(
        base_active_v1_draft_v2, body_template='{% managed_extends "base" %}Bye {name}'
    )
    backend.calls.clear()

    assert service.render_standalone(edited, {"name": "Ana"}, parts="published").body == (
        "[[Bye Ana]]"
    )
    assert writes(backend) == []


def test_a_published_standalone_render_fails_on_a_part_with_nothing_published(
    draft_base, service, email_renderer
):
    with pytest.raises(ManagedTemplateCompositionReferenceError):
        service.render_standalone(draft_base, {"name": "Ana"}, parts="published")
    with pytest.raises(ManagedTemplateCompositionReferenceError):
        email_renderer.render_standalone(draft_base, {"name": "Ana"}, parts="published")


@pytest.mark.parametrize("parts", ["newest", None])
def test_a_standalone_render_refuses_an_unknown_rule(base_active_v1_draft_v2, service, parts):
    with pytest.raises(ValueError, match="parts must be one of"):
        service.render_standalone(base_active_v1_draft_v2, {}, parts=parts)
    with pytest.raises(ValueError, match="parts must be one of"):
        service.template_renderer.render_standalone(base_active_v1_draft_v2, {}, parts=parts)


def test_the_inner_renderer_gets_a_notification_with_no_id_and_no_recipient(
    base_active_v1_draft_v2, backend
):
    inner = NotificationRecordingRenderer()
    service = ManagedTemplateService(backend, ManagedTemplateEmailRenderer(backend, inner))

    service.render_standalone(base_active_v1_draft_v2, {"name": "Ana"}, parts="latest")
    service.template_renderer.render_standalone(
        base_active_v1_draft_v2, {"name": "Ana"}, parts="latest"
    )

    for notification in inner.notifications:
        assert notification == standalone_notification(base_active_v1_draft_v2)
        assert notification.id == ""
        assert notification.user_id == ""
        assert notification.context_kwargs == {}
        assert notification.body_template == "welcome"
        assert notification.requested_template_version == 1
    assert len(inner.notifications) == 2


def test_standalone_notification_is_a_vintasend_notification(base_active_v1_draft_v2):
    assert isinstance(standalone_notification(base_active_v1_draft_v2), Notification)


@pytest.mark.parametrize("parts", ["latest", "published"])
def test_a_service_told_not_to_compose_renders_standalone_verbatim(
    base_active_v1_draft_v2, backend, parts
):
    class Echo(RecordingEmailRenderer):
        def render_from_template_content(self, notification, template_content, context, **kw):
            return super().render_from_template_content(
                notification,
                dataclasses.replace(template_content, body_template="{body}"),
                {**context, "body": template_content.body_template},
            )

    service = ManagedTemplateService(
        backend, ManagedTemplateEmailRenderer(backend, Echo()), compose_templates=False
    )
    renderer = ManagedTemplateEmailRenderer(backend, Echo(), compose_templates=False)
    source = base_active_v1_draft_v2.body_template

    assert service.render_standalone(base_active_v1_draft_v2, {"name": "A"}, parts=parts).body == (
        source
    )
    assert renderer.render_standalone(base_active_v1_draft_v2, {"name": "A"}, parts=parts).body == (
        source
    )


# ----------------------------------------------------------------------
# The render check on publish
# ----------------------------------------------------------------------


def test_a_version_that_renders_is_activated(base_active_v1_draft_v2, service):
    activated = service.activate(
        "welcome", 1, "ana", check_render=RenderCheck(context={"name": "Ana"})
    )

    assert activated.status is ManagedTemplateStatus.ACTIVE
    assert [record.status for record in service.get_status_history("welcome", 1)] == [
        ManagedTemplateStatus.ACTIVE
    ]


def test_set_status_to_active_takes_the_check_too(base_active_v1_draft_v2, service):
    activated = service.set_status(
        "welcome",
        ManagedTemplateStatus.ACTIVE,
        1,
        "ana",
        check_render=RenderCheck(context={"name": "Ana"}),
    )

    assert activated.status is ManagedTemplateStatus.ACTIVE


def test_a_version_that_fails_to_render_is_not_activated_and_nothing_is_written(
    base_active_v1_draft_v2, service, backend
):
    backend.calls.clear()
    history_before = list(backend.status_history)

    with pytest.raises(ManagedTemplateRenderCheckError) as raised:
        # ``{name}`` has nothing to fill it.
        service.activate("welcome", 1, "ana", check_render=RenderCheck(context={"secret": "s3"}))

    error = raised.value
    assert isinstance(error, ManagedTemplateError)
    assert isinstance(error.__cause__, KeyError)
    assert (error.template_key, error.version) == ("welcome", 1)
    assert str(error) == (
        "Template 'welcome' v1 failed its render check (KeyError), so it was not activated."
    )
    assert "s3" not in str(error)
    assert writes(backend) == []
    assert backend.status_history == history_before
    assert service.get_template("welcome", 1).status is ManagedTemplateStatus.DRAFT


def test_the_check_renders_parts_as_a_send_would_not_as_the_service_previews(
    draft_base, service, backend
):
    """The service's composer would find the draft base; a send would not, so neither does the
    check."""
    service.validate_composition(draft_base)
    backend.calls.clear()

    with pytest.raises(ManagedTemplateRenderCheckError) as raised:
        service.activate("welcome", 1, check_render=RenderCheck(context={"name": "Ana"}))

    assert isinstance(raised.value.__cause__, ManagedTemplateCompositionReferenceError)
    assert writes(backend) == []
    assert service.get_template("welcome", 1).status is ManagedTemplateStatus.DRAFT

    service.activate("base", 1)
    assert (
        service.activate("welcome", 1, check_render=RenderCheck(context={"name": "Ana"})).status
        is ManagedTemplateStatus.ACTIVE
    )


def test_a_failing_set_status_check_writes_nothing(base_active_v1_draft_v2, service, backend):
    backend.calls.clear()

    with pytest.raises(ManagedTemplateRenderCheckError):
        service.set_status(
            "welcome", ManagedTemplateStatus.ACTIVE, 1, check_render=RenderCheck(context={})
        )

    assert writes(backend) == []
    assert service.get_status_history("welcome", 1) == []


@pytest.mark.parametrize(
    "status",
    [ManagedTemplateStatus.DRAFT, ManagedTemplateStatus.INACTIVE, ManagedTemplateStatus.ARCHIVED],
)
def test_the_check_with_any_status_but_active_is_refused_before_anything_is_read(
    base_active_v1_draft_v2, service, backend, status
):
    backend.calls.clear()

    with pytest.raises(ValueError, match="check_render applies only when activating"):
        service.set_status("welcome", status, 1, check_render=RenderCheck(context={}))

    assert backend.calls == []
    assert service.get_template("welcome", 1).status is ManagedTemplateStatus.DRAFT


def test_a_version_already_active_is_returned_unchecked(base_active_v1_draft_v2, service, backend):
    publish(backend, "welcome")
    backend.calls.clear()
    renders_before = len(service.template_renderer.renderer.render_from_content_calls)

    unchanged = service.activate("welcome", 1, check_render=RenderCheck(context={}))

    assert unchanged.status is ManagedTemplateStatus.ACTIVE
    assert writes(backend) == []
    assert len(service.template_renderer.renderer.render_from_content_calls) == renders_before


def test_a_disallowed_move_is_refused_before_the_check_renders(service, backend):
    service.create_template(make_create_input("welcome"))
    service.archive("welcome", 1)
    backend.calls.clear()

    with pytest.raises(ManagedTemplateStatusTransitionError):
        service.activate("welcome", 1, check_render=RenderCheck(context={}))

    assert writes(backend) == []
    assert service.template_renderer.renderer.render_from_content_calls == []


def test_a_missing_version_with_a_check_raises_not_found_and_writes_nothing(service, backend):
    with pytest.raises(ManagedTemplateNotFoundError):
        service.activate("nowhere", check_render=RenderCheck(context={}))

    assert writes(backend) == []


def test_a_render_checks_repr_leaves_the_context_out():
    assert "s3" not in repr(RenderCheck(context={"secret": "s3"}))


# ----------------------------------------------------------------------
# get_current_template_version
# ----------------------------------------------------------------------


@pytest.fixture
def current_versions(backend):
    """welcome: v1 active, v2 draft. base: v1 active, abstract. draft-only: v1 draft."""
    backend.create_template(make_create_input("base", template_body="[[{% managed_children %}]]"))
    publish(backend, "base")
    backend.create_template(make_create_input("welcome", template_body="v1"))
    publish(backend, "welcome")
    backend.update_template("welcome", make_update_input(template_body="v2"))
    backend.create_template(make_create_input("draft-only"))


@pytest.mark.parametrize(
    ("key", "expected"),
    [("welcome", 1), ("base", None), ("draft-only", None), ("missing", None)],
)
def test_the_current_version_is_the_newest_active_one_unless_it_is_a_base(
    current_versions, service, email_renderer, key, expected
):
    assert email_renderer.get_current_template_version(key) == expected
    assert service.get_current_template_version(key) == expected
    # What vintasend pins with answers the same, so the two cannot disagree.
    assert email_renderer.get_latest_template_version(key) == expected


def test_a_renderer_that_does_not_compose_treats_nothing_as_a_base(
    current_versions, backend, inner_renderer
):
    renderer = ManagedTemplateEmailRenderer(backend, inner_renderer, compose_templates=False)

    assert renderer.get_current_template_version("base") == 1


def test_an_active_version_too_malformed_to_parse_is_current(backend, email_renderer):
    backend.create_template(make_create_input("broken", template_body="{% managed_block %}"))
    publish(backend, "broken")

    assert email_renderer.get_current_template_version("broken") == 1


def test_vintasend_never_pins_a_notification_to_a_base(
    current_versions, backend, email_renderer, tmp_path
):
    notification_backend = FakeFileBackend(database_file_name=str(tmp_path / "notifications.json"))
    notifications = NotificationService(
        notification_adapters=[
            FakeEmailAdapter(template_renderer=email_renderer, backend=notification_backend)
        ],
        notification_backend=notification_backend,
    )
    # Scheduled, so creating one does not send it and no context has to be registered.
    tomorrow = datetime.datetime.now(tz=datetime.timezone.utc) + datetime.timedelta(days=1)

    def create(body_template: str) -> Notification:
        return notifications.create_notification(
            user_id=1,
            notification_type="EMAIL",
            title="t",
            body_template=body_template,
            context_name="unused",
            context_kwargs={},
            send_after=tomorrow,
            pin_template_versions=True,
        )

    assert create("welcome").requested_template_version == 1
    assert create("base").requested_template_version is None
