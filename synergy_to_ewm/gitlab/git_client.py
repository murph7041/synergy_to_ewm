"""
Git CLI wrapper for pushing Synergy source artifacts to a GitLab repository.

Maintains a persistent local clone so runs can be resumed without re-pushing
already-committed content.  Each baseline becomes one commit and one tag;
loose artifacts are batched into a single commit.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class GitError(RuntimeError):
    pass


class GitClient:
    """
    Wraps the system ``git`` executable.

    Parameters
    ----------
    remote_url:
        Authenticated HTTPS URL, e.g.
        ``https://oauth2:<token>@gitlab.host/namespace/project.git``.
    branch:
        Target branch name (created if absent on the remote).
    author_name / author_email:
        Git author identity used for migration commits.
    git_exe:
        Path to the git executable (default: ``git``).
    """

    def __init__(
        self,
        remote_url: str,
        branch: str = "main",
        author_name: str = "Synergy Migration",
        author_email: str = "migration@localhost",
        git_exe: str = "git",
    ) -> None:
        self.remote_url = remote_url
        self.branch = branch
        self.author_name = author_name
        self.author_email = author_email
        self.git_exe = git_exe

    # ------------------------------------------------------------------
    # Repo initialisation
    # ------------------------------------------------------------------

    def init_or_clone(self, workdir: Path) -> None:
        """
        Prepare *workdir* for commits.

        If *workdir* already contains a git repo it is reused as-is (supports
        resuming an interrupted migration).  Otherwise the remote is cloned; if
        the remote is empty ``git init`` + ``git remote add`` is used instead
        so we can push an initial commit.
        """
        workdir.mkdir(parents=True, exist_ok=True)
        if (workdir / ".git").exists():
            log.info("Reusing existing git workdir %s", workdir)
            return

        # Try to clone; fall back to init for empty/new remote repos.
        try:
            self._run(["clone", "--branch", self.branch,
                       self.remote_url, str(workdir)])
            log.info("Cloned %s → %s", self.remote_url, workdir)
        except GitError:
            log.info("Clone failed (likely empty remote) — initialising local repo")
            self._run(["init", "-b", self.branch, str(workdir)])
            self._run(["remote", "add", "origin", self.remote_url], cwd=workdir)

        self._run(["config", "user.name", self.author_name], cwd=workdir)
        self._run(["config", "user.email", self.author_email], cwd=workdir)

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def write_artifacts(self, artifacts: list, workdir: Path) -> int:
        """
        Write *artifacts* (``EWMArtifact`` objects) to *workdir*, preserving
        their logical paths.  Returns the count of files actually written.
        """
        written = 0
        for artifact in artifacts:
            if artifact.content is None:
                continue
            dest = workdir / artifact.path.lstrip("/\\")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(artifact.content)
            written += 1
        return written

    def stage_and_commit(
        self,
        workdir: Path,
        message: str,
        author_name: str = "",
        author_email: str = "",
    ) -> bool:
        """
        Stage all changes and create a commit.  Returns False when there is
        nothing to commit (workdir already up to date).
        """
        self._run(["add", "."], cwd=workdir)

        # Check whether there is actually anything staged.
        result = subprocess.run(
            [self.git_exe, "diff", "--cached", "--quiet"],
            cwd=str(workdir),
            capture_output=True,
        )
        if result.returncode == 0:
            log.debug("Nothing to commit in %s", workdir)
            return False

        env = self._author_env(author_name or self.author_name,
                               author_email or self.author_email)
        self._run(["commit", "-m", message], cwd=workdir, extra_env=env)
        return True

    def tag(self, workdir: Path, name: str, message: str = "") -> None:
        """Create an annotated tag on the current HEAD."""
        self._run(["tag", "-a", name, "-m", message or name], cwd=workdir)

    def push(self, workdir: Path, push_tags: bool = True) -> None:
        """Push the current branch (and optionally all tags) to origin."""
        self._run(["push", "-u", "origin", self.branch], cwd=workdir)
        if push_tags:
            self._run(["push", "origin", "--tags"], cwd=workdir)
        log.info("Pushed to %s (branch: %s)", self.remote_url.split("@")[-1], self.branch)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _author_env(self, name: str, email: str) -> dict:
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = name
        env["GIT_AUTHOR_EMAIL"] = email
        env["GIT_COMMITTER_NAME"] = self.author_name
        env["GIT_COMMITTER_EMAIL"] = self.author_email
        return env

    def _run(
        self,
        args: list[str],
        cwd: Optional[Path] = None,
        extra_env: Optional[dict] = None,
        check: bool = True,
    ) -> str:
        cmd = [self.git_exe] + args
        log.debug("git: %s", " ".join(str(a) for a in cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(cwd) if cwd else None,
                env=extra_env,
            )
        except FileNotFoundError:
            raise GitError(
                f"git executable not found: {self.git_exe!r}\n"
                "  - Ensure git is installed and on your PATH, or set 'git_exe' in config."
            ) from None

        if result.stderr.strip():
            log.debug("git stderr: %s", result.stderr.strip())
        if check and result.returncode != 0:
            raise GitError(
                f"git command failed (rc={result.returncode}):\n"
                f"  cmd: {' '.join(str(a) for a in cmd)}\n"
                f"  stderr: {result.stderr.strip()}"
            )
        return result.stdout
