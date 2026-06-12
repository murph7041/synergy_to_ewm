"""
REST client for the GitLab API v4.

Covers the operations needed for migration:
  - Project lookup
  - Milestone creation
  - Issue creation (with notes and file uploads)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)


class GitLabError(RuntimeError):
    pass


class GitLabAPIError(GitLabError):
    def __init__(self, message: str, status_code: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class GitLabClient:
    """
    Thin GitLab API v4 client.

    Authentication uses a Personal Access Token (PAT) or Project Access Token
    with ``api`` and ``write_repository`` scopes.  Pass ``token=None`` to fall
    back to the ``GITLAB_TOKEN`` environment variable.
    """

    def __init__(
        self,
        server: str,
        project: str,
        token: Optional[str] = None,
        verify_ssl: bool = True,
        ca_bundle: Optional[str] = None,
        rate_limit_delay: float = 0.1,
    ) -> None:
        self.server = server.rstrip("/")
        self.project = project          # "namespace/name" or numeric ID string
        self.verify: Any = ca_bundle if ca_bundle else verify_ssl
        self.rate_limit_delay = rate_limit_delay
        self._token = token
        self._pid: Optional[int] = None

        self._session = requests.Session()
        retry = Retry(
            total=5, backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,
        )
        self._session.mount("https://", HTTPAdapter(max_retries=retry))
        self._session.mount("http://", HTTPAdapter(max_retries=retry))

    # ------------------------------------------------------------------
    # Token resolution
    # ------------------------------------------------------------------

    def resolve_token(self) -> str:
        if self._token:
            return self._token
        t = os.environ.get("GITLAB_TOKEN")
        if t:
            return t
        raise GitLabError(
            "No GitLab token configured. Set 'token' in the gitlab config section "
            "or export GITLAB_TOKEN=<your-token>."
        )

    def _auth_headers(self) -> dict:
        return {"PRIVATE-TOKEN": self.resolve_token()}

    # ------------------------------------------------------------------
    # Project
    # ------------------------------------------------------------------

    def get_project_id(self) -> int:
        if self._pid is not None:
            return self._pid
        encoded = quote(str(self.project), safe="")
        r = self._get(f"/projects/{encoded}")
        self._pid = r["id"]
        log.info("Resolved GitLab project '%s' → ID %d", self.project, self._pid)
        return self._pid

    # ------------------------------------------------------------------
    # Milestones
    # ------------------------------------------------------------------

    def list_milestones(self) -> list[dict]:
        pid = self.get_project_id()
        results: list[dict] = []
        page = 1
        while True:
            r = self._get(f"/projects/{pid}/milestones",
                          params={"per_page": 100, "page": page})
            if not r:
                break
            results.extend(r)
            page += 1
        return results

    def create_milestone(
        self,
        title: str,
        description: str = "",
        due_date: str = "",
    ) -> int:
        pid = self.get_project_id()
        data: dict = {"title": title}
        if description:
            data["description"] = description
        if due_date:
            data["due_date"] = due_date
        r = self._post(f"/projects/{pid}/milestones", json=data)
        log.debug("Created milestone '%s' → id=%d", title, r["id"])
        return r["id"]

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    def create_issue(
        self,
        title: str,
        description: str,
        labels: Optional[list[str]] = None,
        milestone_id: Optional[int] = None,
        created_at: str = "",
    ) -> dict:
        pid = self.get_project_id()
        data: dict = {"title": title, "description": description}
        if labels:
            data["labels"] = ",".join(labels)
        if milestone_id:
            data["milestone_id"] = milestone_id
        if created_at:
            data["created_at"] = created_at
        return self._post(f"/projects/{pid}/issues", json=data)

    def create_note(
        self,
        issue_iid: int,
        body: str,
        created_at: str = "",
    ) -> None:
        pid = self.get_project_id()
        data: dict = {"body": body}
        if created_at:
            data["created_at"] = created_at
        self._post(f"/projects/{pid}/issues/{issue_iid}/notes", json=data)

    # ------------------------------------------------------------------
    # File uploads
    # ------------------------------------------------------------------

    def upload_file(self, filename: str, content: bytes, mime_type: str) -> str:
        """
        Upload a file to the project and return the GitLab markdown embed string,
        e.g. ``![file.png](/uploads/abc123/file.png)``.
        """
        pid = self.get_project_id()
        r = self._post_raw(
            f"/projects/{pid}/uploads",
            files={"file": (filename, content, mime_type)},
        )
        return r.get("markdown", "")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.server}/api/v4{path}"

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        self._throttle()
        r = self._session.get(
            self._url(path),
            params=params,
            headers=self._auth_headers(),
            verify=self.verify,
        )
        self._check(r, f"GET {path}")
        return r.json()

    def _post(self, path: str, **kwargs) -> dict:
        self._throttle()
        r = self._session.post(
            self._url(path),
            headers=self._auth_headers(),
            verify=self.verify,
            **kwargs,
        )
        self._check(r, f"POST {path}")
        return r.json()

    def _post_raw(self, path: str, **kwargs) -> dict:
        """POST without setting Content-Type (lets requests handle multipart)."""
        self._throttle()
        headers = self._auth_headers()
        r = self._session.post(
            self._url(path),
            headers=headers,
            verify=self.verify,
            **kwargs,
        )
        self._check(r, f"POST {path}")
        return r.json()

    def _check(self, r: requests.Response, context: str) -> None:
        if not r.ok:
            raise GitLabAPIError(
                f"GitLab API error in {context}: HTTP {r.status_code}",
                status_code=r.status_code,
                body=r.text[:500],
            )

    def _throttle(self) -> None:
        if self.rate_limit_delay > 0:
            time.sleep(self.rate_limit_delay)
