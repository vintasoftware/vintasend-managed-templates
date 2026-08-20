import pytest

from vintasend_managed_templates.managed_template_renderer import (
    ManagedTemplateEmailRenderer,
    ManagedTemplateSMSRenderer,
)
from vintasend_managed_templates.managed_template_service import ManagedTemplateService

from .fakes import (
    InMemoryTemplateManagerBackend,
    RecordingEmailRenderer,
    RecordingSMSRenderer,
    make_create_input,
)


@pytest.fixture
def backend() -> InMemoryTemplateManagerBackend:
    return InMemoryTemplateManagerBackend()


@pytest.fixture
def inner_renderer() -> RecordingEmailRenderer:
    return RecordingEmailRenderer()


@pytest.fixture
def email_renderer(backend, inner_renderer) -> ManagedTemplateEmailRenderer:
    return ManagedTemplateEmailRenderer(backend, inner_renderer)


@pytest.fixture
def sms_renderer(backend) -> ManagedTemplateSMSRenderer:
    return ManagedTemplateSMSRenderer(backend, RecordingSMSRenderer())


@pytest.fixture
def service(backend, email_renderer) -> ManagedTemplateService:
    return ManagedTemplateService(backend, email_renderer)


@pytest.fixture
def welcome(service):
    """A single ``welcome`` template at v1, still in DRAFT."""
    return service.create_template(make_create_input("welcome"))
