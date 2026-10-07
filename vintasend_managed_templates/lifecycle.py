"""The two lifecycle rules every backend has to agree on: which version a send renders, and
which versions may be deleted.

Both live here rather than in one backend, so every backend and the service apply the same
rule, and a backend author has a function to call instead of prose to re-derive.

**Which version a send renders.** "The latest version" means two different things, and the
seam keeps them apart:

* The *editing view* -- ``get_template(key)`` with no version -- is the newest version whatever
  its status. An editor, or an API listing a key's history, wants the draft someone is
  working on.
* The *send path* -- ``get_active_template(key)`` -- is the newest ACTIVE version. A draft has
  not been reviewed, so publishing is the deliberate act that puts a version in front of
  recipients. When several versions are active at once (the lifecycle allows it), the
  highest-numbered one wins.

**Which versions may be deleted.** Only one that was never published -- see
``is_template_version_deletable``.
"""

from collections.abc import Iterable

from .constants import ManagedTemplateStatus
from .dataclasses import ManagedTemplate, ManagedTemplateStatusHistory
from .exceptions import ManagedTemplateDeletionNotAllowedError, ManagedTemplateNoActiveVersionError


def newest_active_version(versions: Iterable[ManagedTemplate]) -> ManagedTemplate | None:
    """The highest-numbered ACTIVE version among ``versions``, or None when none is active.

    ``versions`` should hold one key's rows; the caller narrows it to that key first.

    param versions: Iterable[ManagedTemplate]
    return: ManagedTemplate | None
    """
    newest: ManagedTemplate | None = None
    for template in versions:
        if template.status is not ManagedTemplateStatus.ACTIVE:
            continue
        if newest is None or template.version > newest.version:
            newest = template
    return newest


def no_active_version(template_key: str) -> ManagedTemplateNoActiveVersionError:
    """The error a backend raises for a key that exists but has no ACTIVE version.

    param template_key: str
    return: ManagedTemplateNoActiveVersionError
    """
    return ManagedTemplateNoActiveVersionError(
        f"Template '{template_key}' has no active version. Activate a version before sending it."
    )


def is_template_version_deletable(
    template: ManagedTemplate, history: Iterable[ManagedTemplateStatusHistory]
) -> bool:
    """Whether a template version may be hard-deleted, meaning it was never published.

    The version must still be in DRAFT, and its status history must record nothing but DRAFT.
    A backend that writes a history entry when a version is created passes, and so does one
    that writes none. A version that was ever ACTIVE, INACTIVE or ARCHIVED may have rendered a
    notification that is pinned to it, and its history records who published it. Retire it
    with ``archive`` instead.

    ``history`` may hold other versions' entries -- a whole key's trail, say. Only the entries
    for this template's key and version count.

    param template: ManagedTemplate
    param history: Iterable[ManagedTemplateStatusHistory]
    return: bool
    """
    if template.status is not ManagedTemplateStatus.DRAFT:
        return False
    return all(
        record.status is ManagedTemplateStatus.DRAFT
        for record in history
        if record.template_key == template.key and record.version == template.version
    )


def assert_template_version_deletable(
    template: ManagedTemplate, history: Iterable[ManagedTemplateStatusHistory]
) -> None:
    """Raise unless ``is_template_version_deletable`` allows deleting ``template``.

    param template: ManagedTemplate
    param history: Iterable[ManagedTemplateStatusHistory]
    raises ManagedTemplateDeletionNotAllowedError: if the version has been published.
    """
    if is_template_version_deletable(template, history):
        return
    raise ManagedTemplateDeletionNotAllowedError(
        f"Template '{template.key}' v{template.version} has been published and cannot be "
        "deleted. Only a draft that was never published can be deleted; archive this version "
        "instead."
    )
