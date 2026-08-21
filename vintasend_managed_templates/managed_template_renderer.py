"""The renderer that feeds a stored template to an ordinary vintasend renderer.

``ManagedTemplateRenderer`` wraps another renderer and swaps out where the template comes
from: instead of a path an engine's loader resolves, the notification's ``body_template`` is a
key this package's storage seam looks up. What the inner renderer receives is template
*source*, so it needs a loader that accepts source -- see the README.

Templates are composed before they reach the inner renderer. A stored template can extend a
base and include shared fragments (see ``composition``), and none of that survives into what
the engine sees: it gets one flat string. Composition is on by default and can be turned off
per renderer with ``compose_templates=False``, which is the right call only if a store predates
composition and holds ``managed_``-prefixed text meant to be passed through.

There is no AsyncIO twin for this seam -- ``render`` stays synchronous everywhere; async
adapters call it directly.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Generic, TypeVar

from vintasend.services.notification_template_renderers.base import (
    BaseNotificationTemplateRenderer,
    NotificationSendInput,
    TemplateContent,
)
from vintasend.services.notification_template_renderers.base_templated_email_renderer import (
    EmailTemplateContent,
)

from .base_template_manager_backend import BaseTemplateManagerBackend
from .composition import TemplateComposer
from .dataclasses import ManagedTemplate
from .exceptions import ManagedTemplateNotFoundError


if TYPE_CHECKING:
    from vintasend.services.dataclasses import (
        Notification,
        NotificationContextDict,
        OneOffNotification,
    )


TemplateContentType = TypeVar("TemplateContentType", bound=TemplateContent)


class ManagedTemplateRenderer(
    Generic[TemplateContentType], BaseNotificationTemplateRenderer[TemplateContentType], ABC
):
    def __init__(
        self,
        template_manager_backend: BaseTemplateManagerBackend,
        template_renderer: "BaseNotificationTemplateRenderer[TemplateContentType]",
        compose_templates: bool = True,
        composer: TemplateComposer | None = None,
    ):
        """
        :param template_manager_backend: where the template behind a key is read from.
        :param template_renderer: the renderer handed the composed source. It has to accept
            template source in the ``body_template`` field rather than a loadable name.
        :param compose_templates: when True (the default), ``managed_*`` inheritance and
            inclusion tags are resolved before the inner renderer sees the template.
        :param composer: the composer to resolve them with. Defaults to one reading through
            ``template_manager_backend``; pass your own to change the tag prefix or the depth
            limit.
        """
        self.manager_backend = template_manager_backend
        self.renderer = template_renderer
        self.compose_templates = compose_templates
        self.composer = composer or TemplateComposer.from_backend(template_manager_backend)

    def compose(self, template: ManagedTemplate) -> ManagedTemplate:
        """Resolve a template's composition tags, unless this renderer was told not to.

        param template: ManagedTemplate
        return: ManagedTemplate -- flattened, with nothing left for a loader to resolve.
        raises ManagedTemplateCompositionError: if the template cannot be assembled.
        """
        if not self.compose_templates:
            return template
        return self.composer.compose(template)

    @abstractmethod
    def create_template_content(self, template: ManagedTemplate) -> TemplateContentType:
        """
        Creates a TemplateContentType instance from a ManagedTemplate.

        param template: ManagedTemplate
        return: TemplateContentType
        """
        ...

    def render_from_template_content(
        self,
        notification: "Notification | OneOffNotification",
        template_content: TemplateContentType,
        context: "NotificationContextDict",
        **kwargs,
    ) -> NotificationSendInput:
        return self.renderer.render_from_template_content(
            notification, template_content, context, **kwargs
        )

    def get_latest_template_version(self, template_key: str) -> int | None:
        """The newest version of a stored template, for ``NotificationService`` to pin to.

        Answers with whatever version the backend considers current for that key, which is
        the same version ``render`` would resolve to if the notification were left unpinned.
        A key with nothing behind it answers None rather than raising: a missing template is
        the send's problem to report, and failing here would fail the *creation* of a
        notification over a template that might well exist by the time it is sent.

        param template_key: str
        return: int | None
        """
        try:
            return self.manager_backend.get_template(template_key).version
        except ManagedTemplateNotFoundError:
            return None

    def render(
        self, notification: "Notification | OneOffNotification", context: "NotificationContextDict"
    ) -> NotificationSendInput:
        """Render a notification against the version it is pinned to, or the current one.

        ``requested_template_version`` is read off the notification rather than passed in --
        the pin belongs to the record, so a notification recorded against v3 renders v3 for
        as long as it exists, however many versions follow. Unpinned, this resolves to
        whatever the backend considers current, which is how every managed template rendered
        before pinning existed.

        The version that was actually used goes back on the send input, so the service can
        record it against the notification. That is the only way to find out afterwards which
        version an unpinned notification went out with.
        """
        # getattr rather than an attribute read: the field arrived in vintasend 2.1, and this
        # renderer should keep working against a Notification dataclass that predates it.
        requested_version = getattr(notification, "requested_template_version", None)
        template = self.manager_backend.get_template(notification.body_template, requested_version)
        template_content = self.create_template_content(self.compose(template))
        send_input = self.render_from_template_content(notification, template_content, context)
        send_input.template_version = template.version
        return send_input


class ManagedTemplateEmailRenderer(ManagedTemplateRenderer[EmailTemplateContent]):
    def create_template_content(self, template: ManagedTemplate) -> EmailTemplateContent:
        return EmailTemplateContent(
            subject_template=template.subject_template or "",
            body_template=template.body_template,
            preheader_template=template.preheader_template,
        )


class ManagedTemplateSMSRenderer(ManagedTemplateRenderer[TemplateContent]):
    def create_template_content(self, template: ManagedTemplate) -> TemplateContent:
        return TemplateContent(body_template=template.body_template)
