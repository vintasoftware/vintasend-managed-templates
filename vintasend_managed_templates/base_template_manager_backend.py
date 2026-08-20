from abc import ABC, abstractmethod
from collections.abc import Iterable

from .constants import ManagedTemplateStatus
from .dataclasses import (
    ManagedTemplate,
    ManagedTemplateCreateInput,
    ManagedTemplateStatusHistory,
    ManagedTemplateUpdateInput,
)
from .filters import ManagedTemplateFilter


class BaseTemplateManagerBackend(ABC):
    @abstractmethod
    def create_template(self, data: ManagedTemplateCreateInput) -> ManagedTemplate:
        """
        Creates a new template in the backend.

        param data: ManagedTemplateCreateInput
        return: ManagedTemplate
        """
        ...

    @abstractmethod
    def get_template(self, template_key: str, version: int | None = None) -> ManagedTemplate:
        """
        Retrieves a template from the backend by its key.

        param template_key: str
        param version: int | None
        return: ManagedTemplate
        """

        ...

    @abstractmethod
    def update_template(
        self, template_key: str, data: ManagedTemplateUpdateInput
    ) -> ManagedTemplate:
        """
        Creates a new version of an existing template in the backend.

        param template_key: str
        param data: ManagedTemplateCreateInput
        return: ManagedTemplate
        """
        ...

    @abstractmethod
    def delete_template(self, template_key: str, version: int | None = None) -> None:
        """
        Deletes a template from the backend by its key.

        param template_key: str
        """
        ...

    @abstractmethod
    def create_template_status_update(
        self,
        template_key: str,
        version: int,
        status: ManagedTemplateStatus,
        changed_by: str | None = None,
    ) -> None:
        """
        Creates a new status update for a template in the backend.

        param template_key: str
        param version: int,
        param status: ManagedTemplateStatus
        param changed_by: str | None
        """
        ...

    @abstractmethod
    def get_template_status_history(
        self, template_key: str, version: int | None = None
    ) -> Iterable[ManagedTemplateStatusHistory]:
        """
        Retrieves the status history of a template from the backend.

        param template_key: str
        param version: int | None
        return: Iterable[ManagedTemplateStatusHistory]
        """
        ...

    @abstractmethod
    def get_all_templates(self) -> Iterable[ManagedTemplate]:
        """
        Retrieves all templates from the backend.

        return: Iterable[ManagedTemplate]
        """
        ...

    @abstractmethod
    def get_templates_by_status(
        self, status: Iterable[ManagedTemplateStatus]
    ) -> Iterable[ManagedTemplate]:
        """
        Retrieves all templates from the backend with a specific status.

        param status: Iterable[ManagedTemplateStatus]
        return: Iterable[ManagedTemplate]
        """
        ...

    @abstractmethod
    def get_filtered_templates(self, filters: ManagedTemplateFilter) -> Iterable[ManagedTemplate]:
        """
        Retrieves templates from the backend that match the given filters.

        param filters: dict
        return: Iterable[ManagedTemplate]
        """
        ...

    @abstractmethod
    def get_paginated_templates(self, page: int, page_size: int) -> Iterable[ManagedTemplate]:
        """
        Retrieves a paginated list of templates from the backend.

        param page: int
        param page_size: int
        return: Iterable[ManagedTemplate]
        """
        ...

    @abstractmethod
    def get_paginated_filtered_templates(
        self,
        filters: ManagedTemplateFilter,
        page: int,
        page_size: int,
    ) -> Iterable[ManagedTemplate]:
        """
        Retrieves a paginated list of templates matching the given filters from the backend.

        param filters: ManagedTemplateFilter
        param page: int
        param page_size: int
        return: Iterable[ManagedTemplate]
        """
        ...
