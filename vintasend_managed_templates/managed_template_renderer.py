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

Which version a send renders
----------------------------

A notification pinned to a version (``requested_template_version``) renders that version,
whatever its status today. The pin exists so a notification renders what was reviewed when it
was created, and deactivating the version later does not change that. An unpinned notification
renders the key's newest **ACTIVE** version: drafts are never sent, and when several versions
are active the highest-numbered one wins. A key with no active version has nothing to send.

Falling back to a default that ships with the app
-------------------------------------------------

An application can send a notification before anyone has written its template in the store.
Register a default per key under ``fallback`` and, while the key has nothing published,
``render`` hands the default to a renderer of your choosing -- a file-based one, typically::

    ManagedTemplateEmailRenderer(
        manager_backend,
        inner_renderer,
        fallback=ManagedTemplateFallback(
            templates={
                "welcome": FallbackTemplate(
                    subject_template="emails/welcome/subject.txt",
                    body_template="emails/welcome/body.html",
                ),
            },
            renderer=DjangoTemplatedEmailRenderer(),
        ),
    )

The fallback applies only to ``render`` (the send path), only to an unpinned notification, only
when the key itself has nothing published -- not when a stored template fails to compose -- and
only to a registered key. Once a version of the key is activated, sends use it.

There is no AsyncIO twin for this seam -- ``render`` stays synchronous everywhere; async
adapters call it directly.
"""

import dataclasses
import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
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


logger = logging.getLogger(__name__)


TemplateContentType = TypeVar("TemplateContentType", bound=TemplateContent)


@dataclasses.dataclass(frozen=True)
class FallbackTemplate:
    """The default a key renders while nothing is published under it.

    The values are whatever the fallback renderer's ``render`` expects in a notification's
    template fields: file paths for a file-based renderer, names for one that looks templates
    up by name. This package never reads them. ``None`` becomes the empty string the
    notification's own fields default to.
    """

    subject_template: str | None
    body_template: str
    preheader_template: str | None = None


@dataclasses.dataclass(frozen=True)
class ManagedTemplateFallback(Generic[TemplateContentType]):
    """Defaults to send for keys that have nothing published yet.

    :param templates: the default per template key. Copied into a plain ``dict`` when the
        renderer is built, so a mapping with a default for missing keys (a ``defaultdict``,
        say) still only answers for the keys actually registered.
    :param renderer: what renders a default. Its ``render`` is handed a copy of the
        notification whose ``body_template``, ``subject_template`` and ``preheader_template``
        are replaced by the registered values. ``None`` uses the managed renderer's inner
        renderer.
    """

    templates: Mapping[str, FallbackTemplate]
    renderer: "BaseNotificationTemplateRenderer[TemplateContentType] | None" = None


class ManagedTemplateRenderer(
    Generic[TemplateContentType], BaseNotificationTemplateRenderer[TemplateContentType], ABC
):
    def __init__(
        self,
        template_manager_backend: BaseTemplateManagerBackend,
        template_renderer: "BaseNotificationTemplateRenderer[TemplateContentType]",
        compose_templates: bool = True,
        composer: TemplateComposer | None = None,
        fallback: ManagedTemplateFallback[TemplateContentType] | None = None,
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
        :param fallback: defaults to send for keys that have nothing published yet. See the
            module docstring for exactly when one is used.
        """
        self.manager_backend = template_manager_backend
        self.renderer = template_renderer
        self.compose_templates = compose_templates
        self.composer = composer or TemplateComposer.from_backend(template_manager_backend)
        self.fallback = fallback
        self._fallback_templates: dict[str, FallbackTemplate] = (
            dict(fallback.templates) if fallback is not None else {}
        )

    def get_fallback_template(self, template_key: str) -> FallbackTemplate | None:
        """The default registered for ``template_key``, or None when there is none.

        For a dashboard: it can show that a key is "using the default", and start the key's
        first stored version from the default's source.

        param template_key: str
        return: FallbackTemplate | None
        """
        return self._fallback_templates.get(template_key)

    def get_active_template(self, template_key: str) -> ManagedTemplate:
        """The key's newest ACTIVE version -- what an unpinned send renders.

        param template_key: str
        return: ManagedTemplate
        raises ManagedTemplateNotFoundError: if the key does not exist.
        raises ManagedTemplateNoActiveVersionError: if no version of it is ACTIVE.
        """
        return self.manager_backend.get_active_template(template_key)

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
        """The version a notification created now is pinned to: the key's newest ACTIVE version.

        The same version ``render`` would resolve to if the notification were left unpinned,
        so a draft is never pinned. A key with nothing published -- no versions, or only
        drafts and retired ones -- answers None rather than raising: a missing template is
        the send's problem to report, and failing here would fail the *creation* of a
        notification over a template that might well be published by the time it is sent.
        A registered fallback does not change that answer: there is no stored version to pin.

        param template_key: str
        return: int | None
        """
        try:
            return self.get_active_template(template_key).version
        except ManagedTemplateNotFoundError:
            return None

    def render(
        self, notification: "Notification | OneOffNotification", context: "NotificationContextDict"
    ) -> NotificationSendInput:
        """Render a notification against the version it is pinned to, or the newest active one.

        ``requested_template_version`` is read off the notification rather than passed in --
        the pin belongs to the record, so a notification recorded against v3 renders v3 for
        as long as it exists, whatever its status and however many versions follow. Unpinned,
        this resolves to the key's newest ACTIVE version, so a draft is never sent.

        The version that was actually used goes back on the send input, so the service can
        record it against the notification. That is the only way to find out afterwards which
        version an unpinned notification went out with.

        An unpinned notification whose key has nothing published renders the default
        registered under ``fallback``, when there is one. That send input's
        ``template_version`` is None, so the notification's ``used_template_version`` stays
        null: a managed render of a stored template always reports a version, so a null one
        is how a host tells the default went out.

        raises ManagedTemplateNotFoundError: if the key (or the pinned version) does not exist
            and no fallback applies.
        raises ManagedTemplateNoActiveVersionError: if the key has no ACTIVE version and no
            fallback applies.
        raises ManagedTemplateCompositionError: if the stored template cannot be assembled.
        """
        # getattr rather than an attribute read: the field arrived in vintasend 2.1, and this
        # renderer should keep working against a Notification dataclass that predates it.
        requested_version = getattr(notification, "requested_template_version", None)
        try:
            template = self._resolve_template(notification.body_template, requested_version)
        except ManagedTemplateNotFoundError:
            # Only the lookup of the notification's own key is guarded. A stored template that
            # fails to compose -- a base it extends is missing -- raises from ``compose`` below
            # and never reaches this branch: that template exists and is broken, and a default
            # must not hide it.
            fallback = (
                self.get_fallback_template(notification.body_template)
                if requested_version is None
                else None
            )
            if fallback is None:
                raise
            return self._render_fallback(notification, fallback, context)
        template_content = self.create_template_content(self.compose(template))
        send_input = self.render_from_template_content(notification, template_content, context)
        send_input.template_version = template.version
        return send_input

    def _resolve_template(self, template_key: str, version: int | None) -> ManagedTemplate:
        """A pinned version as stored, whatever its status; otherwise the newest active one."""
        if version is not None:
            return self.manager_backend.get_template(template_key, version)
        return self.get_active_template(template_key)

    def _render_fallback(
        self,
        notification: "Notification | OneOffNotification",
        fallback: FallbackTemplate,
        context: "NotificationContextDict",
    ) -> NotificationSendInput:
        # The key and the notification id only: the context is the recipient's data.
        logger.info(
            "Managed template %r has nothing published; rendering its registered fallback "
            "for notification %s.",
            notification.body_template,
            notification.id,
        )
        renderer = (
            self.fallback.renderer
            if self.fallback is not None and self.fallback.renderer is not None
            else self.renderer
        )
        send_input = renderer.render(
            dataclasses.replace(
                notification,
                body_template=fallback.body_template,
                subject_template=fallback.subject_template or "",
                preheader_template=fallback.preheader_template or "",
            ),
            context,
        )
        # No stored version rendered. Cleared rather than trusted, so a fallback renderer that
        # sets a version of its own cannot have it recorded as ``used_template_version``.
        send_input.template_version = None
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
