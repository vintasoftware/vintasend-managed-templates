import datetime
import uuid
from dataclasses import dataclass, field

from .constants import ManagedTemplateStatus, ManagedTemplateTagStatus


@dataclass
class ManagedTemplateTag:
    """A label attached to any number of templates, and templates carry any number of them.

    ``slug`` is the identity: it is normalized from ``text`` by
    ``vintasend_managed_templates.tags.slugify_tag``, unique across the store, and what tag
    filters match on. Editing ``text`` regenerates it.
    """

    id: int | str | uuid.UUID
    text: str
    slug: str
    status: ManagedTemplateTagStatus
    created: datetime.datetime
    updated: datetime.datetime
    tenant: str | None = field(default=None)


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
    # Every tag on this version, in the order the backend returns them. Defaulted so a backend
    # written before tags existed still constructs, and so a test building a template by hand
    # does not have to say "no tags" explicitly.
    tags: list[ManagedTemplateTag] = field(default_factory=list)


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
    # Tag *texts*, not slugs: a caller tags a template with what a person typed, and any text
    # with no tag behind it yet becomes one. None and [] both mean "no tags" on a create.
    tags: list[str] | None = field(default=None)


@dataclass
class ManagedTemplateUpdateInput:
    name: str | None
    description: str | None
    template_body: str | None
    template_subject: str | None
    template_preheader: str | None
    # None carries the previous version's tags forward, matching every other field here.
    # An empty list is the way to say "this version has no tags".
    tags: list[str] | None = field(default=None)
