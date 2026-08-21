"""The storage seam is a public contract for downstream ``vintasend-*`` packages.

These tests guard its shape: every method stays abstract (so a partial implementation fails
loudly at construction rather than at the first call), the ``version`` / ``changed_by``
arguments stay optional, and the fake in ``tests/fakes.py`` -- which stands in for a real
backend across the suite -- satisfies the whole contract.
"""

import inspect

import pytest

from vintasend_managed_templates.base_template_manager_backend import BaseTemplateManagerBackend

from .fakes import InMemoryTemplateManagerBackend


EXPECTED_ABSTRACT_METHODS = frozenset(
    {
        "create_template",
        "get_template",
        "update_template",
        "delete_template",
        "create_template_status_update",
        "get_template_status_history",
        "get_all_templates",
        "get_templates_by_status",
        "get_filtered_templates",
        "get_paginated_templates",
        "get_paginated_filtered_templates",
        "get_or_create_tags",
        "create_tag",
        "get_tag",
        "update_tag",
        "set_tag_status",
        "delete_tag",
        "get_tags",
        "get_template_tags",
        "set_template_tags",
    }
)


def test_the_seam_is_abstract():
    assert inspect.isabstract(BaseTemplateManagerBackend)


def test_the_seam_declares_exactly_the_expected_methods():
    assert set(BaseTemplateManagerBackend.__abstractmethods__) == EXPECTED_ABSTRACT_METHODS


def test_the_seam_cannot_be_instantiated():
    with pytest.raises(TypeError, match="abstract"):
        BaseTemplateManagerBackend()  # type: ignore[abstract]


def test_a_partial_implementation_cannot_be_instantiated():
    class Partial(BaseTemplateManagerBackend):
        def create_template(self, input):  # noqa: A002
            return None

    with pytest.raises(TypeError, match="abstract"):
        Partial()  # type: ignore[abstract]


def test_the_in_memory_fake_implements_the_whole_contract():
    backend = InMemoryTemplateManagerBackend()

    assert isinstance(backend, BaseTemplateManagerBackend)
    assert not inspect.isabstract(InMemoryTemplateManagerBackend)


@pytest.mark.parametrize(
    ("method_name", "argument"),
    [
        ("get_template", "version"),
        ("delete_template", "version"),
        ("get_template_status_history", "version"),
        ("create_template_status_update", "changed_by"),
        ("get_or_create_tags", "tenant"),
        ("create_tag", "tenant"),
        ("get_tags", "status"),
        ("get_tags", "search"),
        ("get_tags", "tenant"),
        ("get_template_tags", "version"),
        ("set_template_tags", "version"),
    ],
)
def test_optional_arguments_keep_their_none_default(method_name, argument):
    signature = inspect.signature(getattr(BaseTemplateManagerBackend, method_name))

    assert signature.parameters[argument].default is None


def test_every_abstract_method_is_documented():
    undocumented = [
        name
        for name in EXPECTED_ABSTRACT_METHODS
        if not (getattr(BaseTemplateManagerBackend, name).__doc__ or "").strip()
    ]

    assert undocumented == []
