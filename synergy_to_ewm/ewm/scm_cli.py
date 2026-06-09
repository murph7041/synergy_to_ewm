"""
IBM Jazz SCM CLI wrapper for source control migration.

Provides the same high-level operations as the REST-based SCM methods in
EWMClient, but delegates to the 'scm' command-line tool shipped with the
IBM EWM client.  Prefer this backend for large repositories where REST API
checkins are slow or unreliable.

Set ``scm_backend: cli`` in the ``ewm`` config section to activate it.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class JazzSCMError(RuntimeError):
    pass


class JazzSCMClient:
    """
    Thin subprocess wrapper around the IBM Jazz SCM ``scm`` CLI.

    Manages a login session for the duration of the migration and provides
    methods for creating components/streams, delivering file batches, and
    creating baseline snapshots.

    Usage::

        with JazzSCMClient("https://ewm:9443/ccm", "user") as scm:
            scm.ensure_component("MyComponent")
            scm.ensure_stream("MyStream", "MyComponent")
            scm.deliver_artifacts(ewm_artifacts, "MyStream")
            scm.create_baseline_snapshot("R1.0", "MyStream", "MyComponent")
    """

    def __init__(
        self,
        server: str,
        user: str,
        password: Optional[str] = None,
        scm_exe: str = "scm",
        verify_ssl: bool = True,
    ) -> None:
        self.server = server
        self.user = user
        self.password = password
        self.scm_exe = scm_exe
        self.verify_ssl = verify_ssl

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    def login(self) -> None:
        args = ["login", "-r", self.server, "-u", self.user]
        if self.password:
            args += ["-P", self.password]
        if not self.verify_ssl:
            args += ["--insecure"]
        self._run(args)
        log.info("Logged in to Jazz SCM repository %s as %s", self.server, self.user)

    def logout(self) -> None:
        self._run(["logout", "-r", self.server], check=False)
        log.info("Logged out from Jazz SCM repository %s", self.server)

    def __enter__(self) -> "JazzSCMClient":
        self.login()
        return self

    def __exit__(self, *_: object) -> None:
        self.logout()

    # ------------------------------------------------------------------
    # Component and stream setup
    # ------------------------------------------------------------------

    def ensure_component(self, name: str) -> str:
        """Create the component if it does not already exist. Returns name."""
        try:
            self._run(["create", "component", name, "-r", self.server])
            log.info("Created Jazz SCM component '%s'", name)
        except JazzSCMError as exc:
            if "already exist" in str(exc).lower():
                log.info("Jazz SCM component '%s' already exists", name)
            else:
                raise
        return name

    def ensure_stream(self, name: str, component_name: str) -> str:
        """Create the stream if it does not already exist. Returns name."""
        try:
            self._run(["create", "stream", name,
                       "-c", component_name, "-r", self.server])
            log.info("Created Jazz SCM stream '%s'", name)
        except JazzSCMError as exc:
            if "already exist" in str(exc).lower():
                log.info("Jazz SCM stream '%s' already exists", name)
            else:
                raise
        return name

    # ------------------------------------------------------------------
    # Artifact delivery
    # ------------------------------------------------------------------

    def deliver_artifacts(
        self,
        artifacts: list,
        stream_name: str,
        comment: str = "Migration from Synergy",
    ) -> None:
        """
        Write *artifacts* (``EWMArtifact`` objects) into a temporary workspace,
        check them in, and deliver to *stream_name*.

        A fresh temporary workspace is created for this delivery and deleted
        after the deliver completes (or fails), so the server is left clean.
        Artifacts whose ``content`` is ``None`` are silently skipped.
        """
        artifacts_with_content = [a for a in artifacts if a.content]
        if not artifacts_with_content:
            log.debug("deliver_artifacts: nothing to deliver (all content is None)")
            return

        workspace_name = f"syn-migration-{uuid.uuid4().hex[:12]}"
        tmpdir = Path(tempfile.mkdtemp())
        sandbox = tmpdir / "sandbox"
        sandbox.mkdir()

        try:
            self._run(["create", "workspace", workspace_name,
                       "-s", stream_name, "-r", self.server])
            try:
                self._run(["load", "-r", self.server, workspace_name,
                           "--dir", str(sandbox)])

                for artifact in artifacts_with_content:
                    dest = sandbox / artifact.path.lstrip("/\\")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(artifact.content)

                # Add all new/modified files in one shot, then check in and deliver.
                self._run(["add", "."], cwd=sandbox)
                self._run(["checkin", ".", "-c", comment], cwd=sandbox)
                self._run(["deliver"], cwd=sandbox)

                log.info(
                    "Delivered %d artifacts to stream '%s'",
                    len(artifacts_with_content), stream_name,
                )
            finally:
                self._run(
                    ["delete", "workspace", workspace_name, "-r", self.server],
                    check=False,
                )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Baseline snapshots
    # ------------------------------------------------------------------

    def create_baseline_snapshot(
        self,
        name: str,
        stream_name: str,
        component_name: str,
        description: str = "",
    ) -> None:
        """Create a baseline snapshot on *stream_name* after content has been delivered."""
        args = [
            "create", "baseline", name,
            "-s", stream_name,
            "-c", component_name,
            "-r", self.server,
        ]
        if description:
            args += ["-d", description]
        self._run(args)
        log.info("Created Jazz SCM baseline '%s' on stream '%s'", name, stream_name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run(
        self,
        args: list[str],
        cwd: Optional[Path] = None,
        check: bool = True,
    ) -> str:
        cmd = [self.scm_exe] + args
        log.debug("scm: %s", " ".join(str(a) for a in cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(cwd) if cwd else None,
            )
        except FileNotFoundError:
            raise JazzSCMError(
                f"Jazz SCM executable not found: {self.scm_exe!r}\n"
                "  - Ensure the EWM client is installed and 'scm' is on your PATH, or\n"
                "  - Set 'scm_exe' in the 'ewm' config section to the full path."
            ) from None

        if result.stderr.strip():
            log.debug("scm stderr: %s", result.stderr.strip())
        if check and result.returncode != 0:
            raise JazzSCMError(
                f"scm command failed (rc={result.returncode}):\n"
                f"  stderr: {result.stderr.strip()}"
            )
        return result.stdout
