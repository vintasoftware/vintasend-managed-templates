from enum import Enum


class ManagedTemplateStatus(Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    INACTIVE = "inactive"
    ARCHIVED = "archived"


# The statuses a version has to be in to count as its key's current one for the
# ``most_recent_active_version`` filter: what is published now, plus the draft on its way to
# replacing it. INACTIVE and ARCHIVED versions are history -- a key whose versions are all
# retired has no current version at all and drops out of that filter entirely.
MOST_RECENT_ACTIVE_VERSION_STATUSES: tuple[ManagedTemplateStatus, ...] = (
    ManagedTemplateStatus.ACTIVE,
    ManagedTemplateStatus.DRAFT,
)


class ManagedTemplateTagStatus(Enum):
    """Whether a tag is still offered when tagging a template.

    ARCHIVED retires a tag from the pickers and suggestion lists a UI builds without breaking
    the templates already carrying it: an archived tag keeps its links, and filtering by it
    keeps working. Deleting the tag is the operation that severs those links.
    """

    ACTIVE = "active"
    ARCHIVED = "archived"
