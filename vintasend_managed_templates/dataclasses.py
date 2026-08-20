import datetime
import uuid
from dataclasses import dataclass, field

from .constants import ManagedTemplateStatus


@dataclass
class ManagedTemplate:
    id: int | str | uuid.UUID
    name: str
    description: str
    key: str
    template_managed_backend: str
    body_template: str
    version: int
    status: ManagedTemplateStatus
    created: datetime.datetime
    updated: datetime.datetime
    subject_template: str | None = field(default=None)
    preheader_template: str | None = field(default=None)
    tenant: str | None = field(default=None)


@dataclass
class ManagedTemplateStatusHistory:
    template_key: str
    version: int
    status: ManagedTemplateStatus
    created: datetime.datetime
    created_by: str | None = field(default=None)
    tenant: str | None = field(default=None)


@dataclass
class ManagedTemplateCreateInput:
    name: str
    description: str
    key: str
    template_managed_backend: str
    template_body: str
    template_subject: str | None
    template_preheader: str | None
    tenant: str | None


@dataclass
class ManagedTemplateUpdateInput:
    name: str | None
    description: str | None
    template_body: str | None
    template_subject: str | None
    template_preheader: str | None
