import dataclasses
import datetime

import pytest

from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.dataclasses import (
    ManagedTemplate,
    ManagedTemplateCreateInput,
    ManagedTemplateStatusHistory,
    ManagedTemplateUpdateInput,
)
from vintasend_managed_templates.exceptions import (
    ManagedTemplateChangeUserNotFoundError,
    ManagedTemplateError,
    ManagedTemplateInvalidFilterError,
    ManagedTemplateNotFoundError,
    ManagedTemplateStatusTransitionError,
)


NOW = datetime.datetime(2026, 1, 1, 12, 0, 0)


# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------


def test_every_status_has_the_expected_wire_value():
    assert {s.name: s.value for s in ManagedTemplateStatus} == {
        "DRAFT": "draft",
        "ACTIVE": "active",
        "INACTIVE": "inactive",
        "ARCHIVED": "archived",
    }


def test_a_status_round_trips_through_its_value():
    assert ManagedTemplateStatus("draft") is ManagedTemplateStatus.DRAFT


def test_an_unknown_status_value_is_rejected():
    with pytest.raises(ValueError, match="not a valid ManagedTemplateStatus"):
        ManagedTemplateStatus("published")


# ----------------------------------------------------------------------
# Dataclasses
# ----------------------------------------------------------------------


def _managed_template(**overrides) -> ManagedTemplate:
    fields = {
        "id": 1,
        "name": "Welcome",
        "description": "d",
        "key": "welcome",
        "template_managed_backend": "in-memory",
        "body_template": "Hi",
        "version": 1,
        "status": ManagedTemplateStatus.DRAFT,
        "created": NOW,
        "updated": NOW,
    }
    return ManagedTemplate(**{**fields, **overrides})


def test_managed_template_optional_fields_default_to_none():
    template = _managed_template()

    assert template.subject_template is None
    assert template.preheader_template is None
    assert template.tenant is None


def test_managed_template_accepts_the_optional_fields():
    template = _managed_template(subject_template="S", preheader_template="P", tenant="acme")

    assert (template.subject_template, template.preheader_template, template.tenant) == (
        "S",
        "P",
        "acme",
    )


@pytest.mark.parametrize("identifier", [1, "abc", datetime.datetime(2026, 1, 1).isoformat()])
def test_managed_template_id_accepts_several_key_types(identifier):
    assert _managed_template(id=identifier).id == identifier


def test_managed_template_replace_produces_a_new_version():
    original = _managed_template()

    bumped = dataclasses.replace(original, version=2)

    assert (original.version, bumped.version) == (1, 2)


def test_status_history_optional_fields_default_to_none():
    record = ManagedTemplateStatusHistory(
        template_key="welcome",
        version=1,
        status=ManagedTemplateStatus.ACTIVE,
        created=NOW,
    )

    assert record.created_by is None
    assert record.tenant is None


def test_create_input_requires_every_field():
    with pytest.raises(TypeError):
        ManagedTemplateCreateInput(name="n")  # type: ignore[call-arg]


def test_create_input_holds_what_it_is_given():
    payload = ManagedTemplateCreateInput(
        name="n",
        description="d",
        key="k",
        template_managed_backend="b",
        template_body="body",
        template_subject=None,
        template_preheader=None,
        tenant=None,
    )

    assert payload.key == "k"
    assert payload.template_subject is None


def test_update_input_carries_only_the_editable_fields():
    assert [f.name for f in dataclasses.fields(ManagedTemplateUpdateInput)] == [
        "name",
        "description",
        "template_body",
        "template_subject",
        "template_preheader",
    ]


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "exception_class",
    [
        ManagedTemplateNotFoundError,
        ManagedTemplateInvalidFilterError,
        ManagedTemplateChangeUserNotFoundError,
        ManagedTemplateStatusTransitionError,
    ],
)
def test_every_error_shares_one_base(exception_class):
    assert issubclass(exception_class, ManagedTemplateError)
    assert issubclass(exception_class, Exception)


@pytest.mark.parametrize(
    "exception_class",
    [
        ManagedTemplateError,
        ManagedTemplateNotFoundError,
        ManagedTemplateInvalidFilterError,
        ManagedTemplateChangeUserNotFoundError,
        ManagedTemplateStatusTransitionError,
    ],
)
def test_every_error_keeps_its_message(exception_class):
    assert str(exception_class("boom")) == "boom"


def test_the_base_error_catches_every_subclass():
    with pytest.raises(ManagedTemplateError):
        raise ManagedTemplateStatusTransitionError("boom")
