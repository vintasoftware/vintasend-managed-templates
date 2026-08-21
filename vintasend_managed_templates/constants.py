from enum import Enum


class ManagedTemplateStatus(Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    INACTIVE = "inactive"
    ARCHIVED = "archived"


class ManagedTemplateTagStatus(Enum):
    """Whether a tag is still offered when tagging a template.

    ARCHIVED retires a tag from the pickers and suggestion lists a UI builds without breaking
    the templates already carrying it: an archived tag keeps its links, and filtering by it
    keeps working. Deleting the tag is the operation that severs those links.
    """

    ACTIVE = "active"
    ARCHIVED = "archived"
