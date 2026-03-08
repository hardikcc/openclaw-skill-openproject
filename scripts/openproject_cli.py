#!/usr/bin/env python3
"""Conservative OpenProject API v3 CLI for project management and knowledge workflows."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlparse

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - dependency fallback for help-only usage
    requests = None  # type: ignore[assignment]

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - dependency fallback for help-only usage
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False

API_PREFIX = "/api/v3"
DEFAULT_DECISION_LOG_DIR = Path("project-knowledge/decisions")
DEFAULT_WEEKLY_STATUS_DIR = Path("project-knowledge/status")
DEFAULT_WEEKLY_FETCH_LIMIT = 200
REQUEST_TIMEOUT_SECONDS = 30
MAX_PAGE_SIZE = 200
RELATION_TYPES = {
    "relates",
    "duplicates",
    "duplicated",
    "blocks",
    "blocked",
    "precedes",
    "follows",
    "includes",
    "partof",
    "requires",
    "required",
}


def to_api_path(url_or_path: str) -> str:
    """Normalize an API href/path into a path that can be passed to `_request`."""
    value = (url_or_path or "").strip()
    if not value:
        return "/"

    if "://" in value:
        parsed = urlparse(value)
        value = parsed.path or "/"

    if value.startswith(API_PREFIX):
        value = value[len(API_PREFIX) :] or "/"

    if not value.startswith("/"):
        value = f"/{value}"

    return value


def to_legacy_path(path: str) -> str:
    """Normalize a non-v3 path."""
    value = (path or "").strip()
    if not value.startswith("/"):
        value = f"/{value}"
    return value


def encode_wiki_title(title: str) -> str:
    """Encode wiki title for URL path usage."""
    value = title.strip()
    if not value:
        raise OpenProjectError("Wiki title is required.")
    return quote(value, safe="")


def extract_legacy_wiki_page(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a normalized wiki page object from legacy JSON payloads."""
    wiki_page = payload.get("wiki_page")
    if isinstance(wiki_page, dict):
        return wiki_page
    return payload


def extract_wiki_text(page: Dict[str, Any]) -> str:
    """Extract wiki text from known payload shapes."""
    text_value = page.get("text")
    if isinstance(text_value, str):
        return text_value
    if isinstance(text_value, dict):
        raw = text_value.get("raw")
        if isinstance(raw, str):
            return raw
    return ""


class OpenProjectError(Exception):
    """Raised when OpenProject configuration or API interaction fails."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpenProjectClient:
    """Minimal OpenProject API v3 client with safe defaults for this alpha."""

    def __init__(
        self,
        base_url: str,
        auth_mode: str = "token",
        api_token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        cleaned_base_url = base_url.strip().rstrip("/")
        if cleaned_base_url.endswith(API_PREFIX):
            cleaned_base_url = cleaned_base_url[: -len(API_PREFIX)]
        if not cleaned_base_url:
            raise OpenProjectError("OPENPROJECT_BASE_URL is required.")

        self.base_url = cleaned_base_url
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/hal+json",
                "Content-Type": "application/json",
                "User-Agent": "openclaw-openproject-cli/0.1.0-alpha",
            }
        )

        mode = auth_mode.strip().lower() if auth_mode else "token"
        if mode == "token":
            if not api_token:
                raise OpenProjectError("OPENPROJECT_API_TOKEN is required for token authentication.")
            # OpenProject token auth convention: username=apikey, password=<token>
            self.session.auth = ("apikey", api_token)
        elif mode == "basic":
            if not username or not password:
                raise OpenProjectError(
                    "OPENPROJECT_USERNAME and OPENPROJECT_PASSWORD are required for basic auth mode."
                )
            self.session.auth = (username, password)
        else:
            raise OpenProjectError(
                f"Unsupported auth mode '{auth_mode}'. Use 'token' (default) or 'basic'."
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
        expected_statuses: Tuple[int, ...] = (200,),
    ) -> Dict[str, Any]:
        """Send a request to OpenProject and normalize common errors."""
        if not path.startswith("/"):
            path = f"/{path}"

        url = f"{self.base_url}{API_PREFIX}{path}"

        try:
            response = self.session.request(
                method=method,
                url=url,
                params=params,
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise OpenProjectError(f"Network error while calling OpenProject: {exc}") from exc

        if response.status_code in expected_statuses:
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {}

        detail = extract_error_message(response)
        if response.status_code in (401, 403):
            raise OpenProjectError(
                "Authentication failed. Check OPENPROJECT_BASE_URL, OPENPROJECT_API_TOKEN, "
                "and token permissions.",
                status_code=response.status_code,
            )

        raise OpenProjectError(
            f"OpenProject API error {response.status_code} for {method.upper()} {path}: {detail}",
            status_code=response.status_code,
        )

    def _legacy_request(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        expected_statuses: Tuple[int, ...] = (200,),
    ) -> Dict[str, Any]:
        """Call legacy (non-v3) JSON endpoints used for wiki read/write compatibility."""
        normalized_path = to_legacy_path(path)
        url = f"{self.base_url}{normalized_path}"
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"

        try:
            response = self.session.request(
                method=method,
                url=url,
                json=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise OpenProjectError(f"Network error while calling legacy endpoint: {exc}") from exc

        if response.status_code in expected_statuses:
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {}

        detail = extract_error_message(response)
        if response.status_code in (401, 403):
            raise OpenProjectError(
                "Legacy wiki endpoint rejected authentication. "
                "Use OPENPROJECT_AUTH_MODE=basic with OPENPROJECT_USERNAME/OPENPROJECT_PASSWORD, "
                "or verify legacy wiki API access for your token."
            )

        raise OpenProjectError(
            f"OpenProject legacy API error {response.status_code} for {method.upper()} "
            f"{normalized_path}: {detail}",
            status_code=response.status_code,
        )

    def _collect_collection(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Collect HAL collection pages up to `limit` elements."""
        if limit <= 0:
            return []

        base_params = dict(params or {})
        page_size = int(base_params.pop("pageSize", min(limit, MAX_PAGE_SIZE)))
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))

        collected: List[Dict[str, Any]] = []
        offset = 1

        while len(collected) < limit:
            remaining = limit - len(collected)
            request_params = dict(base_params)
            request_params["offset"] = offset
            request_params["pageSize"] = min(page_size, remaining)

            data = self._request("GET", path, params=request_params, expected_statuses=(200,))
            elements = extract_embedded_elements(data)
            if not elements:
                break

            collected.extend(elements[:remaining])

            count = int(data.get("count", len(elements)) or len(elements))
            if count <= 0:
                break

            links = data.get("_links", {})
            has_next = isinstance(links, dict) and isinstance(links.get("nextByOffset"), dict)
            if not has_next:
                break

            offset += count

        return collected[:limit]

    def get_projects(self, limit: int = 500) -> List[Dict[str, Any]]:
        """Return a list of projects visible to the current user."""
        return self._collect_collection("/projects", limit=limit)

    def get_statuses(self) -> List[Dict[str, Any]]:
        """Return available work package statuses."""
        data = self._request("GET", "/statuses", expected_statuses=(200,))
        return extract_embedded_elements(data)

    def get_priorities(self) -> List[Dict[str, Any]]:
        """Return available work package priorities."""
        data = self._request("GET", "/priorities", expected_statuses=(200,))
        return extract_embedded_elements(data)

    def get_users(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Return users visible to the current principal."""
        return self._collect_collection("/users", limit=limit)

    def resolve_project(self, project_ref: str) -> Dict[str, Any]:
        """Resolve a project by id, identifier, or exact name (case-insensitive for text)."""
        target = project_ref.strip()
        if not target:
            raise OpenProjectError("Project value is empty.")

        projects = self.get_projects()
        if not projects:
            raise OpenProjectError("No projects were returned by OpenProject.")

        lowered_target = target.lower()

        for project in projects:
            project_id = str(project.get("id", "")).strip()
            identifier = str(project.get("identifier", "")).strip()
            name = str(project.get("name", "")).strip()

            if target.isdigit() and project_id == target:
                return project
            if identifier and identifier.lower() == lowered_target:
                return project
            if name and name.lower() == lowered_target:
                return project

        raise OpenProjectError(
            "Could not resolve project. Provide a valid project ID or identifier, "
            "or set OPENPROJECT_DEFAULT_PROJECT."
        )

    def resolve_project_identifier(self, project_ref: str) -> str:
        """Resolve a project reference and return a stable project identifier."""
        project = self.resolve_project(project_ref)
        identifier = str(project.get("identifier", "")).strip()
        if identifier:
            return identifier
        project_id = project.get("id")
        if project_id is not None:
            return str(project_id)
        raise OpenProjectError("Resolved project does not include identifier or id.")

    def get_types(self, project_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return available work package types, preferring project scope when possible."""
        endpoints: List[str] = []
        if project_id is not None:
            endpoints.append(f"/projects/{project_id}/types")
        endpoints.append("/types")

        for endpoint in endpoints:
            try:
                data = self._request("GET", endpoint, expected_statuses=(200,))
            except OpenProjectError as exc:
                if endpoint.startswith("/projects/") and exc.status_code in (404, 405):
                    continue
                raise

            elements = extract_embedded_elements(data)
            if elements:
                return elements

        return []

    def get_wiki_page_by_id(self, page_id: int) -> Dict[str, Any]:
        """
        Read wiki page metadata from API v3.

        Note: on many OpenProject versions this endpoint is a stub and does not include full text.
        """
        return self._request("GET", f"/wiki_pages/{page_id}", expected_statuses=(200,))

    def list_wiki_pages(self, project_ref: str) -> Tuple[str, List[Dict[str, Any]]]:
        """List wiki pages for a project using the legacy JSON endpoint."""
        project_identifier = self.resolve_project_identifier(project_ref)
        path = f"/projects/{quote(project_identifier, safe='')}/wiki/index.json"
        payload = self._legacy_request("GET", path, expected_statuses=(200,))

        pages = payload.get("wiki_pages")
        if not isinstance(pages, list):
            pages = []

        normalized = [item for item in pages if isinstance(item, dict)]
        return project_identifier, normalized

    def get_wiki_page(self, project_ref: str, title: str) -> Tuple[str, Dict[str, Any]]:
        """Read wiki page content by project + title from the legacy JSON endpoint."""
        project_identifier = self.resolve_project_identifier(project_ref)
        encoded_title = encode_wiki_title(title)
        path = f"/projects/{quote(project_identifier, safe='')}/wiki/{encoded_title}.json"
        payload = self._legacy_request("GET", path, expected_statuses=(200,))
        return project_identifier, extract_legacy_wiki_page(payload)

    def write_wiki_page(
        self,
        project_ref: str,
        title: str,
        text: str,
        comment: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Create or update a wiki page by project + title using the legacy JSON endpoint."""
        project_identifier = self.resolve_project_identifier(project_ref)
        encoded_title = encode_wiki_title(title)
        path = f"/projects/{quote(project_identifier, safe='')}/wiki/{encoded_title}.json"

        payload: Dict[str, Any] = {
            "wiki_page": {
                "text": text,
            }
        }
        if comment:
            payload["wiki_page"]["comments"] = comment

        result = self._legacy_request(
            "PUT",
            path,
            payload=payload,
            expected_statuses=(200, 201, 204),
        )
        if not result:
            _, page = self.get_wiki_page(project_identifier, title)
            return project_identifier, page

        return project_identifier, extract_legacy_wiki_page(result)

    def list_work_packages(
        self,
        project_id: int,
        limit: int = 50,
        status_filter: Optional[str] = None,
        assignee_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch work packages for a project using project scope and optional filter hints."""
        params: Dict[str, Any] = {}
        # Try API-side filtering via query hints when available in the server version.
        if status_filter:
            params["status"] = status_filter
        if assignee_filter:
            params["assignee"] = assignee_filter

        path = f"/projects/{project_id}/work_packages"
        try:
            return self._collect_collection(path, params=params, limit=limit)
        except OpenProjectError as exc:
            if not (status_filter or assignee_filter) or exc.status_code not in (400, 422):
                raise
            return self._collect_collection(path, params={}, limit=limit)

    def resolve_type(self, project_id: Optional[int], type_name: str) -> Tuple[str, str]:
        """Resolve a work package type by name and return (name, href)."""
        lowered_target = type_name.strip().lower()
        endpoints: List[str] = []
        if project_id is not None:
            endpoints.append(f"/projects/{project_id}/types")
        endpoints.append("/types")
        available: List[str] = []

        for endpoint in endpoints:
            try:
                data = self._request("GET", endpoint, expected_statuses=(200,))
            except OpenProjectError as exc:
                if exc.status_code in (404, 405):
                    continue
                raise

            for item in extract_embedded_elements(data):
                name = str(item.get("name", "")).strip()
                href = nested_get(item, ["_links", "self", "href"], "")
                if name:
                    available.append(name)
                if name and name.lower() == lowered_target:
                    return name, href or f"{API_PREFIX}/types/{item.get('id')}"

        if available:
            hint = ", ".join(sorted(set(available)))
            raise OpenProjectError(f"Unknown type '{type_name}'. Available types: {hint}")

        raise OpenProjectError(
            "Could not resolve type list from OpenProject. Check permissions for reading types."
        )

    def resolve_priority(self, priority_name: str) -> Tuple[str, str]:
        """Resolve priority by name and return (name, href)."""
        lowered_target = priority_name.strip().lower()
        priorities = self.get_priorities()
        available: List[str] = []

        for priority in priorities:
            name = str(priority.get("name", "")).strip()
            href = nested_get(priority, ["_links", "self", "href"], "")
            if name:
                available.append(name)
            if name and name.lower() == lowered_target:
                return name, href or f"{API_PREFIX}/priorities/{priority.get('id')}"

        if available:
            hint = ", ".join(sorted(set(available)))
            raise OpenProjectError(
                f"Unknown priority '{priority_name}'. Available priorities: {hint}"
            )

        raise OpenProjectError("No priorities were returned by OpenProject.")

    def resolve_user(self, user_ref: str) -> Tuple[str, str]:
        """Resolve user by id, login, or display name and return (name, href)."""
        target = user_ref.strip()
        if not target:
            raise OpenProjectError("Assignee value is empty.")

        if target.isdigit():
            user_id = int(target)
            user = self._request("GET", f"/users/{user_id}", expected_statuses=(200,))
            name = user_display_name(user)
            return name, f"{API_PREFIX}/users/{user_id}"

        try:
            users = self.get_users(limit=500)
        except OpenProjectError as exc:
            if exc.status_code == 403:
                raise OpenProjectError(
                    "Cannot resolve assignee by name because user listing is not permitted. "
                    "Use numeric --assignee <user_id> or request permission to list users."
                ) from exc
            raise
        lowered_target = target.lower()
        exact_match: Optional[Dict[str, Any]] = None
        partial_match: Optional[Dict[str, Any]] = None

        for user in users:
            keys = user_identity_keys(user)
            lowered_keys = [key.lower() for key in keys]
            if lowered_target in lowered_keys:
                exact_match = user
                break
            if not partial_match and any(lowered_target in key for key in lowered_keys):
                partial_match = user

        selected = exact_match or partial_match
        if selected is None:
            available = sorted(
                {
                    user_display_name(user)
                    for user in users
                    if user_display_name(user) not in {"-", ""}
                }
            )
            hint = ", ".join(available[:12]) if available else "No visible users found."
            raise OpenProjectError(
                f"Unknown user '{user_ref}'. Use numeric user ID, login, or exact display name. "
                f"Known users: {hint}"
            )

        href = nested_get(selected, ["_links", "self", "href"], "")
        user_id = selected.get("id")
        if not href and user_id is not None:
            href = f"{API_PREFIX}/users/{user_id}"
        if not href:
            raise OpenProjectError("Resolved user did not include a self href or id.")
        return user_display_name(selected), href

    def create_work_package(
        self,
        project: Dict[str, Any],
        subject: str,
        type_name: str = "Task",
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a work package in a project."""
        project_id = int(project["id"])
        project_href = nested_get(project, ["_links", "self", "href"], f"{API_PREFIX}/projects/{project_id}")
        _, type_href = self.resolve_type(project_id, type_name)

        payload: Dict[str, Any] = {
            "subject": subject,
            "_links": {
                "project": {"href": project_href},
                "type": {"href": type_href},
            },
        }
        if description:
            payload["description"] = {"raw": description}

        return self._request("POST", "/work_packages", payload=payload, expected_statuses=(200, 201))

    def get_work_package(self, work_package_id: int) -> Dict[str, Any]:
        """Fetch a single work package by id."""
        return self._request("GET", f"/work_packages/{work_package_id}", expected_statuses=(200,))

    def resolve_status(self, status_name: str) -> Tuple[str, str]:
        """Resolve status by name and return (name, href)."""
        lowered_target = status_name.strip().lower()
        data = self._request("GET", "/statuses", expected_statuses=(200,))
        statuses = extract_embedded_elements(data)

        available: List[str] = []
        for status in statuses:
            name = str(status.get("name", "")).strip()
            href = nested_get(status, ["_links", "self", "href"], "")
            if name:
                available.append(name)
            if name and name.lower() == lowered_target:
                return name, href or f"{API_PREFIX}/statuses/{status.get('id')}"

        if available:
            hint = ", ".join(sorted(set(available)))
            raise OpenProjectError(f"Unknown status '{status_name}'. Available statuses: {hint}")

        raise OpenProjectError("No statuses were returned by OpenProject.")

    def resolve_allowed_transition_status(
        self,
        work_package: Dict[str, Any],
        status_name: str,
    ) -> Tuple[str, str]:
        """
        Resolve status from statuses allowed for this specific work package transition.

        Uses the work package form endpoint to respect workflow/role-based transitions.
        Falls back to global status lookup if transition metadata is unavailable.
        """
        lock_version = work_package.get("lockVersion")
        if lock_version is None:
            raise OpenProjectError("Work package payload did not include lockVersion.")

        update_form_href = nested_get(work_package, ["_links", "update", "href"], "")
        if not update_form_href:
            return self.resolve_status(status_name)

        try:
            form_data = self._request(
                "POST",
                to_api_path(update_form_href),
                payload={"lockVersion": lock_version},
                expected_statuses=(200,),
            )
        except OpenProjectError as exc:
            if exc.status_code in (404, 405, 422):
                return self.resolve_status(status_name)
            raise

        status_schema = nested_get(form_data, ["_embedded", "schema", "status"], None)
        if not isinstance(status_schema, dict):
            return self.resolve_status(status_name)

        allowed_values = nested_get(status_schema, ["_embedded", "allowedValues"], [])
        if not isinstance(allowed_values, list) or not allowed_values:
            return self.resolve_status(status_name)

        lowered_target = status_name.strip().lower()
        available: List[str] = []
        for status in allowed_values:
            if not isinstance(status, dict):
                continue
            name = str(status.get("name", "")).strip()
            href = nested_get(status, ["_links", "self", "href"], "")
            if name:
                available.append(name)
            if name and name.lower() == lowered_target:
                return name, href or f"{API_PREFIX}/statuses/{status.get('id')}"

        if available:
            hint = ", ".join(sorted(set(available)))
            raise OpenProjectError(
                f"Status '{status_name}' is not an allowed transition for this work package. "
                f"Allowed statuses: {hint}"
            )

        return self.resolve_status(status_name)

    def update_work_package_status(self, work_package_id: int, status_name: str) -> Dict[str, Any]:
        """Update a work package status using transition-aware status resolution."""
        work_package = self.get_work_package(work_package_id)
        lock_version = work_package.get("lockVersion")
        if lock_version is None:
            raise OpenProjectError("Work package payload did not include lockVersion.")

        _, status_href = self.resolve_allowed_transition_status(work_package, status_name)
        update_href = nested_get(work_package, ["_links", "updateImmediately", "href"], "")
        patch_path = to_api_path(update_href) if update_href else f"/work_packages/{work_package_id}"
        payload = {
            "lockVersion": lock_version,
            "_links": {
                "status": {
                    "href": status_href,
                }
            },
        }
        try:
            return self._request(
                "PATCH",
                patch_path,
                payload=payload,
                expected_statuses=(200,),
            )
        except OpenProjectError as exc:
            if exc.status_code == 422:
                raise OpenProjectError(
                    f"Status update rejected by workflow for work package #{work_package_id}. {exc}"
                ) from exc
            raise

    def update_work_package(
        self,
        work_package_id: int,
        *,
        subject: Optional[str] = None,
        description: Optional[str] = None,
        status_name: Optional[str] = None,
        assignee_ref: Optional[str] = None,
        priority_name: Optional[str] = None,
        type_name: Optional[str] = None,
        start_date: Optional[str] = None,
        due_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update mutable work package fields in a single PATCH call."""
        work_package = self.get_work_package(work_package_id)
        lock_version = work_package.get("lockVersion")
        if lock_version is None:
            raise OpenProjectError("Work package payload did not include lockVersion.")

        payload: Dict[str, Any] = {"lockVersion": lock_version}

        if subject is not None:
            payload["subject"] = subject
        if description is not None:
            payload["description"] = {"raw": description}
        if start_date is not None:
            payload["startDate"] = start_date
        if due_date is not None:
            payload["dueDate"] = due_date

        link_updates: Dict[str, Dict[str, str]] = {}

        if status_name:
            _, status_href = self.resolve_allowed_transition_status(work_package, status_name)
            link_updates["status"] = {"href": status_href}

        if priority_name:
            _, priority_href = self.resolve_priority(priority_name)
            link_updates["priority"] = {"href": priority_href}

        if assignee_ref:
            _, assignee_href = self.resolve_user(assignee_ref)
            link_updates["assignee"] = {"href": assignee_href}

        if type_name:
            project_href = str(nested_get(work_package, ["_links", "project", "href"], ""))
            project_id = extract_numeric_id_from_href(project_href, "projects")
            _, resolved_type_href = self.resolve_type(project_id, type_name)
            link_updates["type"] = {"href": resolved_type_href}

        if link_updates:
            payload["_links"] = link_updates

        if len(payload) <= 1:
            raise OpenProjectError("No fields provided to update.")

        update_href = nested_get(work_package, ["_links", "updateImmediately", "href"], "")
        patch_path = to_api_path(update_href) if update_href else f"/work_packages/{work_package_id}"
        try:
            return self._request(
                "PATCH",
                patch_path,
                payload=payload,
                expected_statuses=(200,),
            )
        except OpenProjectError as exc:
            if exc.status_code == 422:
                raise OpenProjectError(
                    f"Work package update rejected for #{work_package_id}. {exc}"
                ) from exc
            raise

    def list_work_package_relations(
        self,
        work_package_id: int,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """List relations for a single work package."""
        wp_relations_path = f"/work_packages/{work_package_id}/relations"
        try:
            return self._collect_collection(wp_relations_path, limit=limit)
        except OpenProjectError as exc:
            if exc.status_code not in (404, 405):
                raise

        filters = json.dumps(
            [{"involved": {"operator": "=", "values": [str(work_package_id)]}}]
        )
        return self._collect_collection("/relations", params={"filters": filters}, limit=limit)

    def create_relation(
        self,
        from_work_package_id: int,
        to_work_package_id: int,
        relation_type: str,
        description: Optional[str] = None,
        lag: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create a relation between two work packages."""
        relation = relation_type.strip().lower()
        if relation not in RELATION_TYPES:
            allowed = ", ".join(sorted(RELATION_TYPES))
            raise OpenProjectError(
                f"Unsupported relation type '{relation_type}'. Allowed types: {allowed}"
            )

        payload: Dict[str, Any] = {
            "type": relation,
            "_links": {"to": {"href": f"{API_PREFIX}/work_packages/{to_work_package_id}"}},
        }
        if description is not None:
            payload["description"] = description
        if lag is not None:
            payload["lag"] = lag

        return self._request(
            "POST",
            f"/work_packages/{from_work_package_id}/relations",
            payload=payload,
            expected_statuses=(200, 201),
        )

    def add_comment(self, work_package_id: int, comment: str) -> Dict[str, Any]:
        """Add a note/comment to a work package using best-effort API compatibility."""
        work_package = self.get_work_package(work_package_id)
        lock_version = work_package.get("lockVersion")

        add_comment_link = nested_get(work_package, ["_links", "addComment"], None)
        if isinstance(add_comment_link, dict):
            comment_href = add_comment_link.get("href", "")
            method = str(add_comment_link.get("method", "post")).upper()
            if comment_href and method in {"POST", "PATCH"}:
                payload = {"comment": {"raw": comment}}
                try:
                    return self._request(
                        method,
                        to_api_path(comment_href),
                        payload=payload,
                        expected_statuses=(200, 201),
                    )
                except OpenProjectError as exc:
                    if exc.status_code not in (400, 404, 405, 415, 422):
                        raise

        if lock_version is not None:
            update_href = nested_get(work_package, ["_links", "updateImmediately", "href"], "")
            patch_path = to_api_path(update_href) if update_href else f"/work_packages/{work_package_id}"
            patch_payload = {
                "lockVersion": lock_version,
                "comment": {
                    "raw": comment,
                },
            }
            try:
                return self._request(
                    "PATCH",
                    patch_path,
                    payload=patch_payload,
                    expected_statuses=(200,),
                )
            except OpenProjectError as patch_error:
                if patch_error.status_code not in (400, 404, 405, 415, 422):
                    raise

        activities_href = nested_get(work_package, ["_links", "activities", "href"], "")
        fallback_path = to_api_path(activities_href) if activities_href else f"/work_packages/{work_package_id}/activities"
        try:
            return self._request(
                "POST",
                fallback_path,
                payload={"comment": {"raw": comment}},
                expected_statuses=(200, 201),
            )
        except OpenProjectError as fallback_error:
            raise OpenProjectError(
                "Unable to add comment. API v3 comment creation is not available via addComment, "
                "PATCH comment, or activities endpoint on this server/version."
            ) from fallback_error

    def get_time_entry_activities(self) -> List[Dict[str, Any]]:
        """Return available time entry activity types."""
        data = self._request("GET", "/time_entries/activities", expected_statuses=(200,))
        return extract_embedded_elements(data)

    def resolve_time_entry_activity(self, activity_name: str) -> Tuple[str, str]:
        """Resolve a time entry activity by name and return (name, href)."""
        lowered_target = activity_name.strip().lower()
        activities = self.get_time_entry_activities()
        available: List[str] = []
        for activity in activities:
            name = str(activity.get("name", "")).strip()
            href = nested_get(activity, ["_links", "self", "href"], "")
            if name:
                available.append(name)
            if name and name.lower() == lowered_target:
                return name, href if href else f"{API_PREFIX}/time_entries/activities/{activity.get('id')}"
        if available:
            hint = ", ".join(sorted(set(available)))
            raise OpenProjectError(
                f"Unknown time entry activity '{activity_name}'. Available activities: {hint}"
            )
        raise OpenProjectError("No time entry activities were returned by OpenProject.")

    def find_or_create_work_package(
        self,
        project: Dict[str, Any],
        subject: str,
        type_name: str = "Task",
        description: Optional[str] = None,
        limit: int = 200,
    ) -> Tuple[Dict[str, Any], bool]:
        """Find an existing work package by subject in a project, or create it.

        Returns a tuple of (work_package, created) where *created* is True when
        a new work package was created and False when an existing one was found.

        Note: up to *limit* work packages (default 200) are fetched for the
        subject search. In projects with more work packages than the limit some
        existing packages may not be found and a duplicate could be created.
        Increase *limit* if the project is very large.
        """
        project_id = int(project["id"])
        existing = self.list_work_packages(project_id, limit=limit)
        for wp in existing:
            if wp.get("subject") == subject:
                return wp, False
        created = self.create_work_package(
            project=project,
            subject=subject,
            type_name=type_name,
            description=description,
        )
        return created, True

    def log_time(
        self,
        work_package_id: int,
        hours: float,
        activity_name: str,
        spent_on: Optional[str] = None,
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Log spent hours on a work package.

        Parameters
        ----------
        work_package_id:
            The numeric ID of the work package to log time against.
        hours:
            Number of hours to log (e.g. 1.0 for one hour). Must be positive.
        activity_name:
            Name of the time entry activity (e.g. "Internal discussion and meetings").
        spent_on:
            Date the time was spent (``YYYY-MM-DD``). Defaults to today.
        comment:
            Optional free-text comment for the time entry.
        """
        if hours <= 0:
            raise OpenProjectError("hours must be a positive number.")

        _, activity_href = self.resolve_time_entry_activity(activity_name)

        iso_duration = hours_to_iso_duration(hours)
        date_value = spent_on if spent_on else datetime.now().date().isoformat()

        payload: Dict[str, Any] = {
            "hours": iso_duration,
            "spentOn": date_value,
            "_links": {
                "workPackage": {"href": f"{API_PREFIX}/work_packages/{work_package_id}"},
                "activity": {"href": activity_href},
            },
        }
        if comment:
            payload["comment"] = {"raw": comment}

        return self._request("POST", "/time_entries", payload=payload, expected_statuses=(200, 201))


def extract_error_message(response: requests.Response) -> str:
    """Extract a readable error message from an OpenProject error payload."""
    try:
        data = response.json()
    except ValueError:
        body = response.text.strip()
        return body if body else "No error body returned."

    if isinstance(data, dict):
        if isinstance(data.get("message"), str) and data["message"].strip():
            return data["message"].strip()

        embedded = data.get("_embedded")
        if isinstance(embedded, dict):
            errors = embedded.get("errors")
            if isinstance(errors, list):
                messages: List[str] = []
                for err in errors:
                    if isinstance(err, dict):
                        msg = err.get("message")
                        if isinstance(msg, str) and msg.strip():
                            messages.append(msg.strip())
                if messages:
                    return "; ".join(messages)

    return "Unexpected error format returned by server."


def extract_embedded_elements(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return `_embedded.elements` from HAL payloads."""
    embedded = payload.get("_embedded")
    if not isinstance(embedded, dict):
        return []

    elements = embedded.get("elements")
    if not isinstance(elements, list):
        return []

    return [item for item in elements if isinstance(item, dict)]


def nested_get(data: Dict[str, Any], keys: Iterable[str], default: Any = "") -> Any:
    """Safely walk a nested dictionary path."""
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        if key not in current:
            return default
        current = current[key]
    return current


def get_default_project() -> str:
    """Return default project reference from env, if configured."""
    return os.getenv("OPENPROJECT_DEFAULT_PROJECT", "").strip()


def require_project(project_arg: Optional[str]) -> str:
    """Return explicit project argument or env default, otherwise fail."""
    project = (project_arg or "").strip() or get_default_project()
    if not project:
        raise OpenProjectError(
            "--project is required unless OPENPROJECT_DEFAULT_PROJECT is set."
        )
    return project


def build_client_from_env() -> OpenProjectClient:
    """Build the OpenProject client from environment configuration."""
    if requests is None:
        raise OpenProjectError(
            "Missing dependency 'requests'. Install requirements.txt before using API commands."
        )

    base_url = os.getenv("OPENPROJECT_BASE_URL", "").strip()
    auth_mode = os.getenv("OPENPROJECT_AUTH_MODE", "token").strip().lower()

    if auth_mode == "basic":
        username = os.getenv("OPENPROJECT_USERNAME", "").strip()
        password = os.getenv("OPENPROJECT_PASSWORD", "").strip()
        return OpenProjectClient(
            base_url=base_url,
            auth_mode="basic",
            username=username,
            password=password,
        )

    api_token = os.getenv("OPENPROJECT_API_TOKEN", "").strip()
    return OpenProjectClient(base_url=base_url, auth_mode="token", api_token=api_token)


def link_title(item: Dict[str, Any], relation: str, default: str = "-") -> str:
    """Get a relation title from `_links` with fallback to relation id."""
    link_obj = nested_get(item, ["_links", relation], None)
    if isinstance(link_obj, dict):
        title = link_obj.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
        href = link_obj.get("href")
        if isinstance(href, str) and href.strip():
            return href.rstrip("/").split("/")[-1]
    return default


def format_date(iso_timestamp: str) -> str:
    """Format ISO timestamp into YYYY-MM-DD where possible."""
    if not iso_timestamp:
        return "-"
    if len(iso_timestamp) >= 10:
        return iso_timestamp[:10]
    return iso_timestamp


def truncate(value: str, max_length: int = 70) -> str:
    """Truncate text for compact terminal rows."""
    text = value.strip()
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 3]}..."


def normalize(value: str) -> str:
    """Normalize text for case-insensitive comparisons."""
    return value.strip().lower()


def ensure_iso_date(value: str, arg_name: str) -> str:
    """Validate YYYY-MM-DD input and return the normalized value."""
    normalized_value = value.strip()
    try:
        datetime.strptime(normalized_value, "%Y-%m-%d")
    except ValueError as exc:
        raise OpenProjectError(f"{arg_name} must be in YYYY-MM-DD format.") from exc
    return normalized_value


def hours_to_iso_duration(hours: float) -> str:
    """Convert a numeric hours value to an ISO 8601 duration string (e.g. 1.5 -> 'PT1H30M').

    The fractional hours are converted to minutes and rounded to the nearest whole
    minute before building the duration string (e.g. 1.508 h → 90 min → 'PT1H30M').
    """
    if hours <= 0:
        raise OpenProjectError("hours must be a positive number.")
    total_minutes = round(hours * 60)
    h, m = divmod(total_minutes, 60)
    if h and m:
        return f"PT{h}H{m}M"
    if h:
        return f"PT{h}H"
    return f"PT{m}M"


def extract_numeric_id_from_href(href: str, resource: str) -> Optional[int]:
    """Extract trailing numeric id from API href like /api/v3/<resource>/<id>."""
    if not href:
        return None
    match = re.search(rf"/{re.escape(resource)}/(\d+)$", href.strip())
    if not match:
        return None
    return int(match.group(1))


def extract_formattable_text(value: Any) -> str:
    """Extract raw text from either string or OP text object shape."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        raw = value.get("raw")
        if isinstance(raw, str):
            return raw
    return ""


def user_display_name(user: Dict[str, Any]) -> str:
    """Get preferred display name for a user payload."""
    name = str(user.get("name", "")).strip()
    if name:
        return name
    first = str(user.get("firstName", "")).strip()
    last = str(user.get("lastName", "")).strip()
    full = " ".join(part for part in (first, last) if part).strip()
    if full:
        return full
    login = str(user.get("login", "")).strip()
    if login:
        return login
    user_id = user.get("id")
    if user_id is not None:
        return str(user_id)
    return "-"


def user_identity_keys(user: Dict[str, Any]) -> List[str]:
    """Return user identity keys suitable for matching assignee input."""
    keys: List[str] = []
    name = str(user.get("name", "")).strip()
    login = str(user.get("login", "")).strip()
    first = str(user.get("firstName", "")).strip()
    last = str(user.get("lastName", "")).strip()
    full = " ".join(part for part in (first, last) if part).strip()
    user_id = user.get("id")

    for value in (name, login, first, last, full):
        if value:
            keys.append(value)
    if user_id is not None:
        keys.append(str(user_id))

    return keys


def filter_work_packages(
    work_packages: List[Dict[str, Any]],
    status_filter: Optional[str],
    assignee_filter: Optional[str],
) -> List[Dict[str, Any]]:
    """Apply client-side status and assignee filtering."""
    status_query = normalize(status_filter or "")
    assignee_query = normalize(assignee_filter or "")

    filtered: List[Dict[str, Any]] = []
    for wp in work_packages:
        status = normalize(link_title(wp, "status", ""))
        assignee = normalize(link_title(wp, "assignee", "unassigned"))

        if status_query and status_query not in status:
            continue
        if assignee_query and assignee_query not in assignee:
            continue
        filtered.append(wp)

    return filtered


def filter_users(users: List[Dict[str, Any]], query: Optional[str]) -> List[Dict[str, Any]]:
    """Apply case-insensitive substring match over common user identity fields."""
    needle = normalize(query or "")
    if not needle:
        return users

    matched: List[Dict[str, Any]] = []
    for user in users:
        keys = user_identity_keys(user)
        lowered = [key.lower() for key in keys]
        if any(needle in key for key in lowered):
            matched.append(user)

    return matched


def print_projects(projects: List[Dict[str, Any]]) -> None:
    """Print projects in a concise, readable table."""
    if not projects:
        print("No projects found.")
        return

    print("ID   Identifier            Name")
    print("---  --------------------  ------------------------------")
    for project in projects:
        project_id = str(project.get("id", "?"))
        identifier = str(project.get("identifier", "-") or "-")
        name = str(project.get("name", "-") or "-")
        print(f"{project_id:<3}  {truncate(identifier, 20):<20}  {truncate(name, 30)}")


def print_work_packages(work_packages: List[Dict[str, Any]]) -> None:
    """Print work package rows with the required fields."""
    if not work_packages:
        print("No matching work packages found.")
        return

    print("WP ID   Subject                              Status         Assignee        Updated")
    print("-----   -----------------------------------  -------------  --------------  ----------")
    for wp in work_packages:
        wp_id = str(wp.get("id", "?"))
        subject = truncate(str(wp.get("subject", "(no subject)")), 35)
        status = truncate(link_title(wp, "status", "-"), 13)
        assignee = truncate(link_title(wp, "assignee", "Unassigned"), 14)
        updated = format_date(str(wp.get("updatedAt", "")))
        print(f"{wp_id:<5}   {subject:<35}  {status:<13}  {assignee:<14}  {updated}")


def print_statuses(statuses: List[Dict[str, Any]]) -> None:
    """Print available statuses."""
    if not statuses:
        print("No statuses returned.")
        return

    print("ID   Name                       Closed")
    print("---  -------------------------  ------")
    for status in statuses:
        status_id = str(status.get("id", "?"))
        name = truncate(str(status.get("name", "-")), 25)
        is_closed = str(bool(status.get("isClosed", False)))
        print(f"{status_id:<3}  {name:<25}  {is_closed}")


def print_types(types: List[Dict[str, Any]]) -> None:
    """Print available work package types."""
    if not types:
        print("No types returned.")
        return

    print("ID   Name                       Milestone")
    print("---  -------------------------  ---------")
    for type_item in types:
        type_id = str(type_item.get("id", "?"))
        name = truncate(str(type_item.get("name", "-")), 25)
        is_milestone = str(bool(type_item.get("isMilestone", False)))
        print(f"{type_id:<3}  {name:<25}  {is_milestone}")


def print_priorities(priorities: List[Dict[str, Any]]) -> None:
    """Print available priorities."""
    if not priorities:
        print("No priorities returned.")
        return

    print("ID   Name                       Position")
    print("---  -------------------------  --------")
    for priority in priorities:
        priority_id = str(priority.get("id", "?"))
        name = truncate(str(priority.get("name", "-")), 25)
        position = str(priority.get("position", "-"))
        print(f"{priority_id:<3}  {name:<25}  {position}")


def print_users(users: List[Dict[str, Any]]) -> None:
    """Print visible users."""
    if not users:
        print("No users returned.")
        return

    print("ID   Name                              Login")
    print("---  --------------------------------  ----------------------")
    for user in users:
        user_id = str(user.get("id", "?"))
        name = truncate(user_display_name(user), 32)
        login = truncate(str(user.get("login", "-") or "-"), 22)
        print(f"{user_id:<3}  {name:<32}  {login}")


def print_work_package_detail(work_package: Dict[str, Any]) -> None:
    """Print high-value details for a single work package."""
    wp_id = work_package.get("id", "?")
    subject = str(work_package.get("subject", "(no subject)"))
    status = link_title(work_package, "status", "-")
    type_name = link_title(work_package, "type", "-")
    priority = link_title(work_package, "priority", "-")
    assignee = link_title(work_package, "assignee", "Unassigned")
    author = link_title(work_package, "author", "-")
    created = format_date(str(work_package.get("createdAt", "")))
    updated = format_date(str(work_package.get("updatedAt", "")))
    start_date = str(work_package.get("startDate", "-") or "-")
    due_date = str(work_package.get("dueDate", "-") or "-")
    lock_version = str(work_package.get("lockVersion", "-"))

    print(f"Work package #{wp_id}")
    print(f"Subject: {subject}")
    print(f"Status: {status}")
    print(f"Type: {type_name}")
    print(f"Priority: {priority}")
    print(f"Assignee: {assignee}")
    print(f"Author: {author}")
    print(f"Start date: {start_date}")
    print(f"Due date: {due_date}")
    print(f"Created: {created}")
    print(f"Updated: {updated}")
    print(f"Lock version: {lock_version}")

    description = extract_formattable_text(work_package.get("description"))
    if description.strip():
        print("\nDescription:\n")
        print(description.strip())


def print_relations(relations: List[Dict[str, Any]]) -> None:
    """Print relations in a compact table."""
    if not relations:
        print("No relations returned.")
        return

    print("ID    Type        From           To             Lag")
    print("----  ----------  -------------  -------------  ----")
    for relation in relations:
        relation_id = str(relation.get("id", "?"))
        relation_type = truncate(str(relation.get("type", "-")), 10)
        from_wp = truncate(link_title(relation, "from", "-"), 13)
        to_wp = truncate(link_title(relation, "to", "-"), 13)
        lag = str(relation.get("lag", "-"))
        print(f"{relation_id:<4}  {relation_type:<10}  {from_wp:<13}  {to_wp:<13}  {lag}")


def print_wiki_pages(project_identifier: str, pages: List[Dict[str, Any]]) -> None:
    """Print wiki pages in a compact table."""
    print(f"Project wiki: {project_identifier}")
    if not pages:
        print("No wiki pages found.")
        return

    print("Title                              Version   Updated")
    print("---------------------------------  --------  ----------")
    for page in pages:
        title = truncate(str(page.get("title", "(untitled)")), 33)
        version = str(page.get("version", "-"))
        updated = format_date(str(page.get("updated_on") or page.get("updatedAt") or ""))
        print(f"{title:<33}  {version:<8}  {updated}")


def status_bucket(status_name: str) -> str:
    """Map a status name into high-level weekly summary buckets."""
    label = normalize(status_name)
    if any(token in label for token in ("done", "closed", "resolved", "complete", "completed")):
        return "completed"
    if any(token in label for token in ("block", "risk", "hold", "stuck")):
        return "blockers"
    if any(token in label for token in ("progress", "doing", "review", "test", "active", "open", "new")):
        return "in_progress"
    return "in_progress"


def wp_line(wp: Dict[str, Any]) -> str:
    """Return a compact markdown bullet line for a work package."""
    wp_id = wp.get("id", "?")
    subject = str(wp.get("subject", "(no subject)"))
    status = link_title(wp, "status", "-")
    assignee = link_title(wp, "assignee", "Unassigned")
    return f"- #{wp_id} {subject} ({status}; {assignee})"


def build_weekly_summary(project: Dict[str, Any], work_packages: List[Dict[str, Any]]) -> str:
    """Generate a compact markdown weekly summary grouped by status."""
    completed: List[Dict[str, Any]] = []
    in_progress: List[Dict[str, Any]] = []
    blockers: List[Dict[str, Any]] = []

    for wp in work_packages:
        status = link_title(wp, "status", "-")
        bucket = status_bucket(status)
        if bucket == "completed":
            completed.append(wp)
        elif bucket == "blockers":
            blockers.append(wp)
        else:
            in_progress.append(wp)

    project_name = str(project.get("name") or project.get("identifier") or project.get("id"))
    today = datetime.now().date().isoformat()

    lines: List[str] = [
        f"# Weekly Status - {project_name}",
        f"Date: {today}",
        "",
        "## Wins / completed",
    ]

    if completed:
        lines.extend(wp_line(wp) for wp in completed[:10])
    else:
        lines.append("- No completed items detected in current snapshot.")

    lines.extend(["", "## In progress"])
    if in_progress:
        lines.extend(wp_line(wp) for wp in in_progress[:15])
    else:
        lines.append("- No in-progress items detected.")

    lines.extend(["", "## Blockers / risks"])
    if blockers:
        lines.extend(wp_line(wp) for wp in blockers[:10])
    else:
        lines.append("- No explicit blockers inferable from current status labels.")

    lines.extend(["", "## Next focus"])
    focus_items = in_progress[:5]
    if focus_items:
        lines.extend(wp_line(wp) for wp in focus_items)
    else:
        lines.append("- Confirm priorities for the next sprint window.")

    return "\n".join(lines).strip() + "\n"


def write_text_file(path: Path, content: str) -> Path:
    """Write text content to a file, creating parent directories when needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def slugify(value: str) -> str:
    """Generate a filesystem-safe slug from free text."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "decision"


def unique_path(base_path: Path) -> Path:
    """Avoid overwriting an existing file by appending a numeric suffix."""
    if not base_path.exists():
        return base_path

    counter = 2
    while True:
        candidate = base_path.with_name(f"{base_path.stem}-{counter}{base_path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def build_decision_markdown(
    date_text: str,
    project: str,
    title: str,
    decision: str,
    context: str,
    impact: str,
    followup: str,
) -> str:
    """Render a decision log entry in markdown."""
    context_block = context.strip() or "(none provided)"
    impact_block = impact.strip() or "(to be assessed)"
    followup_block = followup.strip() or "(none)"

    return (
        f"# Decision: {title}\n\n"
        f"Date: {date_text}\n"
        f"Project: {project}\n\n"
        "## Context\n"
        f"{context_block}\n\n"
        "## Decision\n"
        f"{decision.strip()}\n\n"
        "## Impact\n"
        f"{impact_block}\n\n"
        "## Follow-up\n"
        f"{followup_block}\n"
    )


def maybe_print_json(data: Any, enabled: bool) -> None:
    """Print debug JSON when requested."""
    if enabled:
        print(json.dumps(data, indent=2, sort_keys=True))


def command_list_projects(args: argparse.Namespace) -> None:
    client = build_client_from_env()
    projects = client.get_projects()
    print_projects(projects)
    maybe_print_json(projects, args.debug_json)


def command_list_work_packages(args: argparse.Namespace) -> None:
    project_ref = require_project(args.project)
    client = build_client_from_env()
    project = client.resolve_project(project_ref)

    work_packages = client.list_work_packages(
        int(project["id"]),
        limit=args.limit,
        status_filter=args.status,
        assignee_filter=args.assignee,
    )
    filtered = filter_work_packages(work_packages, args.status, args.assignee)

    project_label = project.get("identifier") or project.get("name") or project.get("id")
    print(f"Project: {project_label}")
    print_work_packages(filtered)
    maybe_print_json(filtered, args.debug_json)


def command_create_work_package(args: argparse.Namespace) -> None:
    project_ref = require_project(args.project)
    client = build_client_from_env()
    project = client.resolve_project(project_ref)

    created = client.create_work_package(
        project=project,
        subject=args.subject,
        type_name=args.type,
        description=args.description,
    )

    wp_id = created.get("id", "?")
    subject = created.get("subject", args.subject)
    print(f"Created work package #{wp_id}: {subject}")
    maybe_print_json(created, args.debug_json)


def command_update_work_package_status(args: argparse.Namespace) -> None:
    client = build_client_from_env()
    updated = client.update_work_package_status(args.id, args.status)

    wp_id = updated.get("id", args.id)
    status = link_title(updated, "status", args.status)
    print(f"Updated work package #{wp_id} to status '{status}'.")
    maybe_print_json(updated, args.debug_json)


def command_add_comment(args: argparse.Namespace) -> None:
    client = build_client_from_env()
    updated = client.add_comment(args.id, args.comment)

    wp_id = updated.get("id", args.id)
    print(f"Added comment to work package #{wp_id}.")
    maybe_print_json(updated, args.debug_json)


def command_get_work_package(args: argparse.Namespace) -> None:
    client = build_client_from_env()
    work_package = client.get_work_package(args.id)
    print_work_package_detail(work_package)
    maybe_print_json(work_package, args.debug_json)


def command_update_work_package(args: argparse.Namespace) -> None:
    if not any(
        value is not None
        for value in (
            args.subject,
            args.description,
            args.status,
            args.assignee,
            args.priority,
            args.type,
            args.start_date,
            args.due_date,
        )
    ):
        raise OpenProjectError("Provide at least one field to update.")

    start_date = ensure_iso_date(args.start_date, "--start-date") if args.start_date else None
    due_date = ensure_iso_date(args.due_date, "--due-date") if args.due_date else None

    client = build_client_from_env()
    updated = client.update_work_package(
        args.id,
        subject=args.subject,
        description=args.description,
        status_name=args.status,
        assignee_ref=args.assignee,
        priority_name=args.priority,
        type_name=args.type,
        start_date=start_date,
        due_date=due_date,
    )

    wp_id = updated.get("id", args.id)
    print(f"Updated work package #{wp_id}.")
    print_work_package_detail(updated)
    maybe_print_json(updated, args.debug_json)


def command_list_statuses(args: argparse.Namespace) -> None:
    client = build_client_from_env()
    statuses = client.get_statuses()
    print_statuses(statuses)
    maybe_print_json(statuses, args.debug_json)


def command_list_types(args: argparse.Namespace) -> None:
    client = build_client_from_env()

    project_id: Optional[int] = None
    if args.project:
        project = client.resolve_project(args.project)
        project_id = int(project["id"])
        project_label = project.get("identifier") or project.get("name") or project_id
        print(f"Project: {project_label}")

    types = client.get_types(project_id=project_id)
    print_types(types)
    maybe_print_json(types, args.debug_json)


def command_list_priorities(args: argparse.Namespace) -> None:
    client = build_client_from_env()
    priorities = client.get_priorities()
    print_priorities(priorities)
    maybe_print_json(priorities, args.debug_json)


def command_list_users(args: argparse.Namespace) -> None:
    client = build_client_from_env()
    try:
        users = client.get_users(limit=args.limit)
    except OpenProjectError as exc:
        if exc.status_code == 403:
            raise OpenProjectError(
                "Listing users is forbidden for this token/role. "
                "Use an account with user-list permission or assign by numeric user ID."
            ) from exc
        raise
    filtered = filter_users(users, args.query)
    print_users(filtered)
    maybe_print_json(filtered, args.debug_json)


def command_list_relations(args: argparse.Namespace) -> None:
    client = build_client_from_env()
    relations = client.list_work_package_relations(args.id, limit=args.limit)
    print(f"Work package #{args.id}")
    print_relations(relations)
    maybe_print_json(relations, args.debug_json)


def command_create_relation(args: argparse.Namespace) -> None:
    client = build_client_from_env()
    relation = client.create_relation(
        from_work_package_id=args.from_id,
        to_work_package_id=args.to_id,
        relation_type=args.type,
        description=args.description,
        lag=args.lag,
    )

    relation_id = relation.get("id", "?")
    relation_type = relation.get("type", args.type)
    print(
        f"Created relation #{relation_id}: #{args.from_id} {relation_type} #{args.to_id}."
    )
    maybe_print_json(relation, args.debug_json)


def command_list_wiki_pages(args: argparse.Namespace) -> None:
    project_ref = require_project(args.project)
    client = build_client_from_env()
    project_identifier, pages = client.list_wiki_pages(project_ref)

    print_wiki_pages(project_identifier, pages)
    maybe_print_json({"project": project_identifier, "wiki_pages": pages}, args.debug_json)


def command_read_wiki_page(args: argparse.Namespace) -> None:
    client = build_client_from_env()

    if args.id is not None and (args.project or args.title):
        raise OpenProjectError("Use either --id OR (--project and --title), not both.")

    if args.id is None and not args.title:
        raise OpenProjectError("Provide --id or --title.")

    page: Dict[str, Any]
    project_identifier = ""
    title = ""

    if args.id is not None:
        page = client.get_wiki_page_by_id(args.id)
        title = str(page.get("title") or f"wiki-page-{args.id}")

        project_identifier = str(
            nested_get(page, ["_embedded", "project", "identifier"], "")
            or nested_get(page, ["_embedded", "project", "id"], "")
            or nested_get(page, ["_links", "project", "title"], "")
        )

        text = extract_wiki_text(page)
        if not text and project_identifier and title:
            try:
                project_identifier, legacy_page = client.get_wiki_page(project_identifier, title)
                page = legacy_page
            except OpenProjectError:
                pass
    else:
        project_ref = require_project(args.project)
        project_identifier, page = client.get_wiki_page(project_ref, args.title)
        title = str(page.get("title") or args.title)

    text = extract_wiki_text(page)
    version = page.get("version", "-")

    print(f"Wiki page: {title}")
    if project_identifier:
        print(f"Project: {project_identifier}")
    print(f"Version: {version}")

    if text:
        print("")
        print(text)
    else:
        print("")
        print(
            "No wiki text returned. This server may expose only wiki metadata via API v3 "
            "or block legacy wiki JSON endpoints for the current auth mode."
        )

    if args.output:
        output_path = Path(args.output)
        output_content = text if text else f"# {title}\n\n(No wiki text returned by API.)\n"
        written_path = write_text_file(output_path, output_content)
        print(f"\nSaved wiki content to {written_path}")

    maybe_print_json(page, args.debug_json)


def command_write_wiki_page(args: argparse.Namespace) -> None:
    project_ref = require_project(args.project)
    client = build_client_from_env()

    if bool(args.content) == bool(args.content_file):
        raise OpenProjectError("Provide exactly one of --content or --content-file.")

    if args.content_file:
        content_path = Path(args.content_file)
        if not content_path.exists():
            raise OpenProjectError(f"Content file not found: {content_path}")
        content = content_path.read_text(encoding="utf-8")
    else:
        content = args.content

    project_identifier, page = client.write_wiki_page(
        project_ref=project_ref,
        title=args.title,
        text=content,
        comment=args.comment,
    )

    title = str(page.get("title") or args.title)
    version = page.get("version", "-")
    print(f"Wrote wiki page '{title}' in project '{project_identifier}' (version: {version}).")
    maybe_print_json(page, args.debug_json)


def command_weekly_summary(args: argparse.Namespace) -> None:
    project_ref = require_project(args.project)
    client = build_client_from_env()
    project = client.resolve_project(project_ref)

    work_packages = client.list_work_packages(int(project["id"]), limit=DEFAULT_WEEKLY_FETCH_LIMIT)
    summary = build_weekly_summary(project, work_packages)
    print(summary)

    if args.output:
        output_path = Path(args.output)
    else:
        today = datetime.now().date().isoformat()
        output_path = DEFAULT_WEEKLY_STATUS_DIR / f"{today}-weekly-status.md"

    written_path = write_text_file(output_path, summary)
    print(f"Saved weekly summary to {written_path}")


def command_log_decision(args: argparse.Namespace) -> None:
    project_ref = require_project(args.project)

    configured_dir = os.getenv("OPENPROJECT_DECISION_LOG_DIR", "").strip()
    decision_dir = Path(configured_dir) if configured_dir else DEFAULT_DECISION_LOG_DIR

    today = datetime.now().date().isoformat()
    filename = f"{today}_{slugify(args.title)}.md"
    file_path = unique_path(decision_dir / filename)

    markdown = build_decision_markdown(
        date_text=today,
        project=project_ref,
        title=args.title,
        decision=args.decision,
        context=args.context or "",
        impact=args.impact or "",
        followup=args.followup or "",
    )

    written_path = write_text_file(file_path, markdown)
    print(f"Created decision log: {written_path}")


def command_log_spent_hours(args: argparse.Namespace) -> None:
    """Find or create a work package in a project and log spent hours against it."""
    project_ref = require_project(args.project)
    client = build_client_from_env()
    project = client.resolve_project(project_ref)

    spent_on = ensure_iso_date(args.spent_on, "--spent-on") if args.spent_on else None

    wp, created = client.find_or_create_work_package(
        project=project,
        subject=args.work_package,
        type_name=args.type,
    )
    wp_id = wp.get("id", "?")
    if created:
        print(f"Created work package #{wp_id}: {wp.get('subject', args.work_package)}")
    else:
        print(f"Found existing work package #{wp_id}: {wp.get('subject', args.work_package)}")

    time_entry = client.log_time(
        work_package_id=int(wp_id),
        hours=args.hours,
        activity_name=args.activity,
        spent_on=spent_on,
        comment=args.comment,
    )
    entry_id = time_entry.get("id", "?")
    logged_date = time_entry.get("spentOn") or spent_on or datetime.now().date().isoformat()
    print(
        f"Logged {args.hours}h on work package #{wp_id} "
        f"(date: {logged_date}, activity: '{args.activity}', time entry #{entry_id})."
    )
    maybe_print_json(time_entry, args.debug_json)


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser and subcommands."""
    parser = argparse.ArgumentParser(
        description="OpenProject project-management and knowledge CLI (alpha)."
    )
    parser.add_argument(
        "--debug-json",
        action="store_true",
        help="Print raw JSON payloads for troubleshooting.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    parser_projects = subparsers.add_parser(
        "list-projects",
        help="List visible OpenProject projects.",
        description="Fetch and print projects from OpenProject.",
    )
    parser_projects.set_defaults(func=command_list_projects)

    parser_list_wp = subparsers.add_parser(
        "list-work-packages",
        help="List work packages for a project.",
        description="Fetch work packages and apply optional client-side filters.",
    )
    parser_list_wp.add_argument(
        "--project",
        help="Project ID or identifier. Optional when OPENPROJECT_DEFAULT_PROJECT is set.",
    )
    parser_list_wp.add_argument(
        "--status",
        help="Optional status filter (case-insensitive substring match).",
    )
    parser_list_wp.add_argument(
        "--assignee",
        help="Optional assignee filter (case-insensitive substring match).",
    )
    parser_list_wp.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum number of work packages to fetch (default: 50).",
    )
    parser_list_wp.set_defaults(func=command_list_work_packages)

    parser_create_wp = subparsers.add_parser(
        "create-work-package",
        help="Create a new work package.",
        description="Create a work package in a project.",
    )
    parser_create_wp.add_argument(
        "--project",
        help="Project ID or identifier. Optional when OPENPROJECT_DEFAULT_PROJECT is set.",
    )
    parser_create_wp.add_argument("--subject", required=True, help="Work package subject/title.")
    parser_create_wp.add_argument(
        "--type",
        default="Task",
        help="Work package type name (default: Task).",
    )
    parser_create_wp.add_argument(
        "--description",
        help="Optional work package description.",
    )
    parser_create_wp.set_defaults(func=command_create_work_package)

    parser_update_status = subparsers.add_parser(
        "update-work-package-status",
        help="Update work package status.",
        description="Update status of an existing work package by status name.",
    )
    parser_update_status.add_argument("--id", type=int, required=True, help="Work package ID.")
    parser_update_status.add_argument(
        "--status",
        required=True,
        help="Target status name (case-insensitive).",
    )
    parser_update_status.set_defaults(func=command_update_work_package_status)

    parser_comment = subparsers.add_parser(
        "add-comment",
        help="Add a comment to a work package.",
        description="Add a note/comment to a work package via API v3 best effort.",
    )
    parser_comment.add_argument("--id", type=int, required=True, help="Work package ID.")
    parser_comment.add_argument("--comment", required=True, help="Comment text.")
    parser_comment.set_defaults(func=command_add_comment)

    parser_get_wp = subparsers.add_parser(
        "get-work-package",
        help="Get details for a single work package.",
        description="Fetch and print high-value fields for one work package.",
    )
    parser_get_wp.add_argument("--id", type=int, required=True, help="Work package ID.")
    parser_get_wp.set_defaults(func=command_get_work_package)

    parser_update_wp = subparsers.add_parser(
        "update-work-package",
        help="Update work package fields in one command.",
        description="Patch a work package with one or more mutable fields.",
    )
    parser_update_wp.add_argument("--id", type=int, required=True, help="Work package ID.")
    parser_update_wp.add_argument("--subject", help="New subject/title.")
    parser_update_wp.add_argument("--description", help="New description text.")
    parser_update_wp.add_argument(
        "--status",
        help="New status name (transition-aware, case-insensitive).",
    )
    parser_update_wp.add_argument(
        "--assignee",
        help="Assignee user id, login, or display name.",
    )
    parser_update_wp.add_argument("--priority", help="Priority name.")
    parser_update_wp.add_argument("--type", help="Work package type name.")
    parser_update_wp.add_argument("--start-date", help="Start date (YYYY-MM-DD).")
    parser_update_wp.add_argument("--due-date", help="Due date (YYYY-MM-DD).")
    parser_update_wp.set_defaults(func=command_update_work_package)

    parser_statuses = subparsers.add_parser(
        "list-statuses",
        help="List available work package statuses.",
        description="Fetch and print status values from OpenProject.",
    )
    parser_statuses.set_defaults(func=command_list_statuses)

    parser_types = subparsers.add_parser(
        "list-types",
        help="List available work package types.",
        description="Fetch and print type values globally or project-scoped when provided.",
    )
    parser_types.add_argument(
        "--project",
        help="Optional project ID or identifier for project-scoped type resolution.",
    )
    parser_types.set_defaults(func=command_list_types)

    parser_priorities = subparsers.add_parser(
        "list-priorities",
        help="List available work package priorities.",
        description="Fetch and print priority values from OpenProject.",
    )
    parser_priorities.set_defaults(func=command_list_priorities)

    parser_users = subparsers.add_parser(
        "list-users",
        help="List visible users.",
        description="Fetch users and optionally filter by query string.",
    )
    parser_users.add_argument(
        "--query",
        help="Optional case-insensitive substring filter (name/login/id).",
    )
    parser_users.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum number of users to fetch (default: 200).",
    )
    parser_users.set_defaults(func=command_list_users)

    parser_relations = subparsers.add_parser(
        "list-relations",
        help="List relations for a work package.",
        description="Fetch and print relation rows for a single work package.",
    )
    parser_relations.add_argument("--id", type=int, required=True, help="Work package ID.")
    parser_relations.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum number of relations to fetch (default: 100).",
    )
    parser_relations.set_defaults(func=command_list_relations)

    parser_create_relation = subparsers.add_parser(
        "create-relation",
        help="Create a relation between work packages.",
        description="Create a relation from one work package to another.",
    )
    parser_create_relation.add_argument(
        "--from-id",
        type=int,
        required=True,
        help="Source work package ID.",
    )
    parser_create_relation.add_argument(
        "--to-id",
        type=int,
        required=True,
        help="Target work package ID.",
    )
    parser_create_relation.add_argument(
        "--type",
        required=True,
        help="Relation type (for example: relates, blocks, follows).",
    )
    parser_create_relation.add_argument(
        "--description",
        help="Optional relation description.",
    )
    parser_create_relation.add_argument(
        "--lag",
        type=int,
        help="Optional lag value (integer, typically in days).",
    )
    parser_create_relation.set_defaults(func=command_create_relation)

    parser_list_wiki = subparsers.add_parser(
        "list-wiki-pages",
        help="List wiki pages for a project.",
        description="List wiki pages using OpenProject legacy JSON endpoint compatibility.",
    )
    parser_list_wiki.add_argument(
        "--project",
        help="Project ID or identifier. Optional when OPENPROJECT_DEFAULT_PROJECT is set.",
    )
    parser_list_wiki.set_defaults(func=command_list_wiki_pages)

    parser_read_wiki = subparsers.add_parser(
        "read-wiki-page",
        help="Read a wiki page by id or project/title.",
        description="Read wiki metadata from API v3 and text content via legacy wiki JSON endpoint when available.",
    )
    parser_read_wiki.add_argument(
        "--id",
        type=int,
        help="Wiki page ID from API v3.",
    )
    parser_read_wiki.add_argument(
        "--project",
        help="Project ID or identifier (used with --title). Optional when OPENPROJECT_DEFAULT_PROJECT is set.",
    )
    parser_read_wiki.add_argument(
        "--title",
        help="Wiki page title (used with --project or default project).",
    )
    parser_read_wiki.add_argument(
        "--output",
        help="Optional output file path for page text.",
    )
    parser_read_wiki.set_defaults(func=command_read_wiki_page)

    parser_write_wiki = subparsers.add_parser(
        "write-wiki-page",
        help="Create or update a wiki page.",
        description="Write wiki page content through OpenProject legacy wiki JSON endpoint compatibility.",
    )
    parser_write_wiki.add_argument(
        "--project",
        help="Project ID or identifier. Optional when OPENPROJECT_DEFAULT_PROJECT is set.",
    )
    parser_write_wiki.add_argument("--title", required=True, help="Wiki page title.")
    parser_write_wiki.add_argument(
        "--content",
        help="Inline wiki content.",
    )
    parser_write_wiki.add_argument(
        "--content-file",
        help="Path to file containing wiki content.",
    )
    parser_write_wiki.add_argument(
        "--comment",
        help="Optional update comment/changelog note.",
    )
    parser_write_wiki.set_defaults(func=command_write_wiki_page)

    parser_weekly = subparsers.add_parser(
        "weekly-summary",
        help="Generate a compact weekly markdown summary.",
        description="Create a weekly status summary grouped by work package status.",
    )
    parser_weekly.add_argument(
        "--project",
        help="Project ID or identifier. Optional when OPENPROJECT_DEFAULT_PROJECT is set.",
    )
    parser_weekly.add_argument(
        "--output",
        help="Optional output file path. Defaults to project-knowledge/status/YYYY-MM-DD-weekly-status.md.",
    )
    parser_weekly.set_defaults(func=command_weekly_summary)

    parser_decision = subparsers.add_parser(
        "log-decision",
        help="Write a project decision markdown entry.",
        description="Create a decision log entry in project-knowledge/decisions.",
    )
    parser_decision.add_argument(
        "--project",
        help="Project ID or identifier. Optional when OPENPROJECT_DEFAULT_PROJECT is set.",
    )
    parser_decision.add_argument("--title", required=True, help="Decision title.")
    parser_decision.add_argument("--decision", required=True, help="Decision statement.")
    parser_decision.add_argument("--context", help="Optional context notes.")
    parser_decision.add_argument("--impact", help="Optional impact notes.")
    parser_decision.add_argument("--followup", help="Optional follow-up actions.")
    parser_decision.set_defaults(func=command_log_decision)

    parser_log_hours = subparsers.add_parser(
        "log-spent-hours",
        help="Log spent hours on a work package (find or create it).",
        description=(
            "Find a work package by subject in a project (creating it if absent) "
            "and log a time entry against it."
        ),
    )
    parser_log_hours.add_argument(
        "--project",
        help="Project ID or identifier. Optional when OPENPROJECT_DEFAULT_PROJECT is set.",
    )
    parser_log_hours.add_argument(
        "--work-package",
        required=True,
        help="Work package subject. An existing work package is found by exact subject match; "
             "a new one is created if none is found.",
    )
    parser_log_hours.add_argument(
        "--hours",
        type=float,
        required=True,
        help="Number of hours to log (e.g. 1 or 1.5).",
    )
    parser_log_hours.add_argument(
        "--activity",
        required=True,
        help="Time entry activity name (e.g. 'Internal discussion and meetings').",
    )
    parser_log_hours.add_argument(
        "--spent-on",
        help="Date the time was spent (YYYY-MM-DD). Defaults to today.",
    )
    parser_log_hours.add_argument(
        "--comment",
        help="Optional comment for the time entry.",
    )
    parser_log_hours.add_argument(
        "--type",
        default="Task",
        help="Work package type name used when creating a new work package (default: Task).",
    )
    parser_log_hours.set_defaults(func=command_log_spent_hours)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint."""
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if hasattr(args, "limit") and args.limit is not None and args.limit <= 0:
            raise OpenProjectError("--limit must be greater than 0.")
        if hasattr(args, "lag") and args.lag is not None and args.lag < 0:
            raise OpenProjectError("--lag must be zero or greater.")
        args.func(args)
        return 0
    except OpenProjectError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Error: Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
