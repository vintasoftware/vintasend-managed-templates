"""Rendering seam stub.

Subclass ``BaseNotificationTemplateRenderer`` (or, more likely, one of its typed subclasses
``BaseTemplatedEmailRenderer`` / ``BaseTemplatedSMSRenderer``) to turn a notification's template
plus context into the input its adapter needs to send. There is no AsyncIO twin for this seam --
``render`` stays synchronous everywhere; async adapters call it directly.
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
from .dataclasses import ManagedTemplate


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
    ):
        self.manager_backend = template_manager_backend
        self.renderer = template_renderer

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

    def render(
        self, notification: "Notification | OneOffNotification", context: "NotificationContextDict"
    ) -> NotificationSendInput:
        template = self.manager_backend.get_template(notification.body_template)
        template_content = self.create_template_content(template)
        return self.render_from_template_content(notification, template_content, context)


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
