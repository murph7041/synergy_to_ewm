"""
HTTP client for IBM EWM (Engineering Workflow Management) using:
  - Jazz Form-based authentication
  - OSLC Change Management 2.0 for work items
  - EWM SCM REST service for source control
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

_OSLC_CM_JSON = "application/x-oslc-cm-change-request+json"
_JSON = "application/json"
_FORM = "application/x-www-form-urlencoded"


class EWMAuthError(RuntimeError):
    pass


class EWMAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class EWMClient:
    """
    EWM REST client.

    Usage::

        with EWMClient("https://host:9443/ccm", "user", "pass") as client:
            project_id = client.find_project_area("My Project")
            wi_url = client.create_work_item(project_id, {...})
    """

    def __init__(
        self,
        server: str,
        user: str,
        password: Optional[str] = None,
        verify_ssl: bool = True,
        ca_bundle: Optional[str] = None,
        rate_limit_delay: float = 0.1,
        connect_timeout: float = 15.0,
        read_timeout: float = 120.0,
    ) -> None:
        self.server = server.rstrip("/")
        self.user = user
        self.password = password
        self.verify: Any = ca_bundle if ca_bundle else verify_ssl
        self.rate_limit_delay = rate_limit_delay
        # (connect_timeout, read_timeout) passed to every request
        self._timeout: tuple = (connect_timeout, read_timeout)

        self._session = requests.Session()
        retry = Retry(
            total=5,
            connect=3,          # retry on connection-refused / DNS failure
            read=3,             # retry on read timeout before first byte
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,
        )
        self._session.mount("https://", HTTPAdapter(max_retries=retry))
        self._session.mount("http://", HTTPAdapter(max_retries=retry))
        self._authenticated = False

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    _KEYRING_SERVICE = "synergy_to_ewm:ewm"

    def _resolve_password(self) -> str:
        """Return the EWM password, prompting and optionally saving if absent."""
        if self.password:
            return self.password

        import keyring
        import keyring.errors

        # Try the system credential store first (works silently on subsequent runs).
        try:
            stored = keyring.get_password(self._KEYRING_SERVICE, self.user)
            if stored:
                return stored
            _keyring_available = True
        except keyring.errors.NoKeyringError:
            _keyring_available = False

        import getpass
        import sys
        pwd = getpass.getpass(f"EWM password for {self.user}: ")

        if _keyring_available:
            store_name = (
                "Windows Credential Manager"
                if sys.platform == "win32"
                else "system keyring"
            )
            answer = input(f"Save password to {store_name}? [y/N] ").strip().lower()
            if answer == "y":
                keyring.set_password(self._KEYRING_SERVICE, self.user, pwd)
                print("Password saved.")

        return pwd

    def authenticate(self) -> None:
        """
        Jazz form-based authentication.
        Handles the redirect dance required by Jazz servers.
        """
        # 1. Trigger the auth redirect by hitting a protected resource
        auth_url = self._url("/authenticated/identity")
        r = self._session.get(auth_url, verify=self.verify,
                              allow_redirects=True, timeout=self._timeout)

        if r.status_code == 200 and "authenticated" in r.url:
            self._authenticated = True
            log.info("Already authenticated to EWM")
            return

        # 2. POST credentials to the Jazz security check endpoint
        login_url = self._url("/authenticated/j_security_check")
        data = {"j_username": self.user, "j_password": self._resolve_password()}
        r = self._session.post(
            login_url,
            data=data,
            headers={"Content-Type": _FORM},
            verify=self.verify,
            allow_redirects=True,
            timeout=self._timeout,
        )

        if r.status_code not in (200, 302):
            raise EWMAuthError(
                f"Authentication failed (HTTP {r.status_code}): {r.text[:200]}"
            )

        # 3. Verify we are now authenticated
        r2 = self._session.get(auth_url, verify=self.verify, timeout=self._timeout)
        if r2.status_code != 200:
            raise EWMAuthError("Authentication check failed after login")

        self._authenticated = True
        log.info("Authenticated to EWM as %s", self.user)

    def __enter__(self) -> "EWMClient":
        self.authenticate()
        return self

    def __exit__(self, *_: object) -> None:
        self._session.close()

    # ------------------------------------------------------------------
    # Project areas
    # ------------------------------------------------------------------

    def list_project_areas(self) -> list[dict]:
        """Return all project areas visible to the authenticated user."""
        url = self._url("/process/project-areas")
        return self._get_json(url).get("projectAreas", [])

    def find_project_area(self, name: str) -> str:
        """Return the project area UUID for the given name."""
        areas = self.list_project_areas()
        for area in areas:
            if area.get("title", "") == name:
                pid = area.get("key") or area.get("itemId") or area.get("uuid", "")
                log.info("Found project area '%s' → %s", name, pid)
                return pid
        raise EWMAPIError(f"Project area '{name}' not found")

    def list_work_item_types(self, project_area_id: str) -> list[dict]:
        url = self._url(f"/oslc/contexts/{project_area_id}/workitems/types")
        return self._get_json(url).get("oslc_cm:types", [])

    # ------------------------------------------------------------------
    # Work items
    # ------------------------------------------------------------------

    def create_work_item(self, project_area_id: str, payload: dict) -> str:
        """
        Create a work item via OSLC CM and return its URL.

        Parameters
        ----------
        project_area_id:
            Project area UUID obtained from find_project_area().
        payload:
            OSLC CM JSON payload dict.  The caller is responsible for
            setting at minimum: dc:title, dc:type, rtc_cm:filedAgainst.
        """
        url = self._url(f"/oslc/contexts/{project_area_id}/workitems")
        r = self._request("POST", url, json=payload, headers={
            "Content-Type": _OSLC_CM_JSON,
            "Accept": _OSLC_CM_JSON,
            "OSLC-Core-Version": "2.0",
            "X-Jazz-CSRF-Prevent": self.user,
        })
        self._check(r, "create_work_item")
        location = r.headers.get("Location", "")
        log.debug("Created work item: %s", location)
        return location

    def update_work_item(self, work_item_url: str, payload: dict) -> None:
        """Update an existing work item (PATCH)."""
        r = self._request("PATCH", work_item_url, json=payload, headers={
            "Content-Type": _OSLC_CM_JSON,
            "Accept": _OSLC_CM_JSON,
            "OSLC-Core-Version": "2.0",
            "X-Jazz-CSRF-Prevent": self.user,
        })
        self._check(r, "update_work_item")

    def add_comment(self, work_item_url: str, text: str, author: str = "") -> None:
        """Append a comment to a work item."""
        # EWM stores comments as part of the work item's discussion thread
        comment_url = work_item_url.rstrip("/") + "/rtc_cm:comments"
        payload = {
            "dc:description": text,
            "dc:creator": {"dc:title": author} if author else {},
        }
        r = self._request("POST", comment_url, json=payload, headers={
            "Content-Type": _JSON,
            "Accept": _JSON,
            "X-Jazz-CSRF-Prevent": self.user,
        })
        self._check(r, "add_comment")

    def upload_attachment(
        self,
        work_item_url: str,
        name: str,
        content: bytes,
        mime_type: str,
    ) -> str:
        """Upload a file and attach it to a work item. Returns attachment URL."""
        attach_url = self._url("/service/com.ibm.team.workitem.common.internal.rest."
                               "IAttachmentRestService/attachment")
        r = self._request("POST", attach_url,
                          files={"content": (name, content, mime_type)},
                          headers={"X-Jazz-CSRF-Prevent": self.user})
        self._check(r, "upload_attachment")
        attachment_url = r.headers.get("Location", "")

        # Link attachment to work item
        link_payload = {"rtc_cm:attachment": [{"rdf:resource": attachment_url}]}
        self.update_work_item(work_item_url, link_payload)
        return attachment_url

    # ------------------------------------------------------------------
    # SCM — components and streams
    # ------------------------------------------------------------------

    def list_components(self, project_area_id: str) -> list[dict]:
        url = self._url(f"/service/com.ibm.team.scm.rest.IScmRestService/"
                        f"repositories?projectAreaItemId={project_area_id}")
        return self._get_json(url).get("values", [])

    def create_component(self, name: str, project_area_id: str) -> str:
        """Create an SCM component and return its item ID."""
        url = self._url("/service/com.ibm.team.scm.rest.IScmRestService/components")
        payload = {"name": name, "projectAreaItemId": project_area_id}
        r = self._request("POST", url, json=payload, headers={
            "Content-Type": _JSON,
            "Accept": _JSON,
            "X-Jazz-CSRF-Prevent": self.user,
        })
        self._check(r, "create_component")
        return r.json().get("itemId", "")

    def create_stream(self, name: str, project_area_id: str,
                      component_id: str) -> str:
        """Create a stream targeting a component and return its item ID."""
        url = self._url("/service/com.ibm.team.scm.rest.IScmRestService/workspaces")
        payload = {
            "name": name,
            "type": "stream",
            "projectAreaItemId": project_area_id,
            "components": [{"itemId": component_id}],
        }
        r = self._request("POST", url, json=payload, headers={
            "Content-Type": _JSON,
            "Accept": _JSON,
            "X-Jazz-CSRF-Prevent": self.user,
        })
        self._check(r, "create_stream")
        return r.json().get("itemId", "")

    def checkin_file(
        self,
        workspace_id: str,
        component_id: str,
        path: str,
        content: bytes,
        mime_type: str = "application/octet-stream",
        comment: str = "",
        author: str = "",
    ) -> None:
        """
        Check a file into a workspace/stream inside a named changeset.

        Creates a changeset with the given comment/author, adds the file,
        then completes the changeset so it is visible in the stream history.
        """
        cs_id = self._create_changeset(workspace_id, component_id, comment, author)
        checkin_url = self._url(
            f"/service/com.ibm.team.scm.rest.IScmRestService/"
            f"workspaces/{workspace_id}/components/{component_id}/"
            f"changesets/{cs_id}/content"
        )
        r = self._request("POST", checkin_url,
                          files={"content": (path, content, mime_type)},
                          data={"path": path},
                          headers={"X-Jazz-CSRF-Prevent": self.user})
        self._check(r, "checkin_file")
        self._complete_changeset(workspace_id, component_id, cs_id)

    def _create_changeset(
        self,
        workspace_id: str,
        component_id: str,
        comment: str = "",
        author: str = "",
    ) -> str:
        """Create an EWM SCM changeset and return its item ID."""
        url = self._url(
            f"/service/com.ibm.team.scm.rest.IScmRestService/"
            f"workspaces/{workspace_id}/components/{component_id}/changesets"
        )
        payload: dict = {}
        if comment:
            payload["comment"] = comment
        if author:
            payload["author"] = author
        r = self._request("POST", url, json=payload, headers={
            "Content-Type": _JSON,
            "Accept": _JSON,
            "X-Jazz-CSRF-Prevent": self.user,
        })
        self._check(r, "create_changeset")
        return r.json().get("itemId", "")

    def _complete_changeset(
        self,
        workspace_id: str,
        component_id: str,
        changeset_id: str,
    ) -> None:
        """Mark a changeset as complete so its contents are visible in the stream."""
        url = self._url(
            f"/service/com.ibm.team.scm.rest.IScmRestService/"
            f"workspaces/{workspace_id}/components/{component_id}/"
            f"changesets/{changeset_id}/complete"
        )
        r = self._request("POST", url, headers={"X-Jazz-CSRF-Prevent": self.user})
        self._check(r, "complete_changeset")

    def create_baseline(
        self,
        stream_id: str,
        component_id: str,
        name: str,
        description: str = "",
    ) -> str:
        """Create an SCM baseline and return its item ID."""
        url = self._url(
            f"/service/com.ibm.team.scm.rest.IScmRestService/"
            f"workspaces/{stream_id}/components/{component_id}/baselines"
        )
        r = self._request("POST", url,
                          json={"name": name, "description": description},
                          headers={
                              "Content-Type": _JSON,
                              "Accept": _JSON,
                              "X-Jazz-CSRF-Prevent": self.user,
                          })
        self._check(r, "create_baseline")
        return r.json().get("itemId", "")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return urljoin(self.server + "/", path.lstrip("/"))

    def _get_json(self, url: str) -> dict:
        r = self._request("GET", url, headers={"Accept": _JSON, "OSLC-Core-Version": "2.0"})
        self._check(r, f"GET {url}")
        return r.json()

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Central request helper — injects timeout on every call."""
        kwargs.setdefault("timeout", self._timeout)
        kwargs.setdefault("verify", self.verify)
        self._throttle()
        return self._session.request(method, url, **kwargs)

    def _check(self, r: requests.Response, context: str) -> None:
        if not r.ok:
            raise EWMAPIError(
                f"EWM API error in {context}: HTTP {r.status_code}",
                status_code=r.status_code,
                body=r.text[:500],
            )

    def _throttle(self) -> None:
        if self.rate_limit_delay > 0:
            time.sleep(self.rate_limit_delay)
