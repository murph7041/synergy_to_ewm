"""
Thin wrapper around the Synergy 'ccm' CLI.

All commands are run via subprocess; the session is started once and reused.
CCM writes its data to stdout; errors go to stderr.
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Chosen to be unlikely in any real attribute value.  Pipe characters or commas
# appear in descriptions; this triple-bar sequence does not appear in CCM output.
_DELIM = "|||"


class CCMError(RuntimeError):
    pass


class CCMClient:
    """Stateful wrapper around the ccm executable."""

    def __init__(
        self,
        server: str,
        database: str,
        user: str,
        password: Optional[str] = None,
        ccm_exe: str = "ccm",
    ) -> None:
        self.server = server
        self.database = database
        self.user = user
        self.password = password
        self.ccm_exe = ccm_exe
        self._started = False
        # CCM_HOME is printed by `ccm start` and must be threaded back into
        # every subsequent ccm call so that parallel sessions don't conflict.
        self._home: Optional[str] = None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open a Synergy session."""
        cmd = [
            self.ccm_exe, "start",
            "-s", self.server,
            "-d", self.database,
            "-n", self.user,
            "-q",           # suppress interactive prompts
            "-nogui",
        ]
        if self.password:
            cmd += ["-p", self.password]
        result = self._run_raw(cmd)
        # `ccm start` prints the CCM_HOME path on success; empty on failure.
        home_line = result.stdout.strip()
        if home_line:
            self._home = home_line
        self._started = True
        log.info("Synergy session started (CCM_HOME=%s)", self._home)

    def stop(self) -> None:
        if not self._started:
            return
        self._run(["stop"])
        self._started = False
        log.info("Synergy session stopped")

    def __enter__(self) -> "CCMClient":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(self, query_string: str, attributes: list[str]) -> list[dict]:
        """
        Run a CCM query and return rows as dicts.

        Parameters
        ----------
        query_string:
            CCM query expression, e.g. "type='task' and release='R1.0'"
        attributes:
            List of attribute names to retrieve, e.g. ["task_number","synopsis"]
        """
        fmt = _DELIM.join(f"%{a}" for a in attributes)
        cmd = ["query", query_string, "-u", "-f", fmt]
        raw = self._run(cmd)
        rows = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(_DELIM)
            # Pad rows that are shorter than the attribute list (CCM omits
            # trailing empty values) so every row has the same number of keys.
            padded = parts + [""] * (len(attributes) - len(parts))
            rows.append(dict(zip(attributes, padded)))
        return rows

    # ------------------------------------------------------------------
    # Attribute access
    # ------------------------------------------------------------------

    def get_attributes(self, object_spec: str) -> dict[str, str]:
        """Return all attributes of a CCM object as a flat dict."""
        raw = self._run(["attribute", "-list", object_spec])
        attrs: dict[str, str] = {}
        for line in raw.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                attrs[key.strip()] = val.strip()
        return attrs

    def get_attribute(self, attr_name: str, object_spec: str) -> str:
        raw = self._run(["attribute", "-show", attr_name, object_spec])
        return raw.strip()

    # ------------------------------------------------------------------
    # Task notes / comments
    # ------------------------------------------------------------------

    def get_task_notes(self, task_spec: str) -> list[dict]:
        """
        Return task notes as a list of {author, timestamp, text} dicts.
        CCM prints notes in a structured block format; see _parse_notes().
        """
        raw = self._run(["task", "-show_notes", task_spec])
        return _parse_notes(raw)

    # ------------------------------------------------------------------
    # Task change history
    # ------------------------------------------------------------------

    def get_task_history(self, task_spec: str) -> list[dict]:
        """
        Return change history for a task as a list of
        {author, timestamp, changes: [str]} dicts.

        Uses `ccm history` which records every attribute modification.
        See _parse_history() for the expected output format.
        """
        raw = self._run(["history", task_spec])
        return _parse_history(raw)

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    def get_attachments(self, task_spec: str) -> list[dict]:
        """
        Return attachment metadata for a task.
        Each dict has keys: name, spec.
        Content is not fetched here — use get_file_content().
        """
        raw = self._run(["attribute", "-show", "attachments", task_spec])
        result = []
        for line in raw.splitlines():
            line = line.strip()
            if line:
                # Attachment specs look like "filename.txt~attach~1:user:db";
                # the filename is always the first tilde-delimited segment.
                result.append({"spec": line, "name": line.split("~")[0]})
        return result

    # ------------------------------------------------------------------
    # Source control
    # ------------------------------------------------------------------

    def list_projects(self) -> list[dict]:
        """Return a list of project dicts with keys: name, spec, status, release."""
        fmt = _DELIM.join(["%displayname", "%objectname", "%status", "%release"])
        raw = self._run(["project", "-list", "-format", fmt])
        keys = ["name", "spec", "status", "release"]
        return _parse_delimited(raw, keys)

    def list_objects(self, project_spec: str, recursive: bool = True) -> list[dict]:
        """List versioned objects within a project."""
        flags = ["-r"] if recursive else []
        fmt = _DELIM.join(["%name", "%version", "%type", "%status", "%owner",
                           "%create_time", "%modify_time", "%objectname"])
        cmd = ["ls"] + flags + ["-format", fmt, project_spec]
        raw = self._run(cmd)
        # Note: CCM uses "%type" in format strings but the key is stored as
        # "obj_type" in the returned dict to avoid shadowing Python's built-in
        # `type`.  Callers that use get_attributes() will see the key as "type"
        # instead — the extractor handles this mapping explicitly.
        keys = ["name", "version", "obj_type", "status", "owner",
                "create_time", "modify_time", "spec"]
        return _parse_delimited(raw, keys)

    def get_file_content(self, object_spec: str) -> bytes:
        """Retrieve the raw content of a versioned file object."""
        # CCM requires an output file path; it cannot write to stdout for
        # binary content.  We use a temp file and read it back.
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name
        try:
            self._run(["cat", "-out", tmp_path, object_spec])
            return Path(tmp_path).read_bytes()
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def get_object_path(self, object_spec: str, project_spec: str) -> str:
        """Return the logical path of an object within its project."""
        raw = self._run(["object", "-show_path", "-project", project_spec,
                         object_spec])
        return raw.strip()

    def get_object_versions(self, object_spec: str) -> list[str]:
        """
        Return all version specs for a versioned object, ordered oldest-first.

        Walks the predecessor chain by repeatedly querying the ``predecessor``
        attribute.  This is more reliable than parsing ``ccm history`` output
        because the predecessor attribute is part of the CCM data model and its
        format does not change between server versions.

        Stops when:
          - the predecessor attribute is empty or the literal string "None"
          - a spec already seen is returned (cycle guard against corrupt data)
          - get_attribute() raises (object has no predecessor attribute)
        """
        chain = [object_spec]
        seen = {object_spec}
        current = object_spec
        while True:
            try:
                pred = self.get_attribute("predecessor", current).strip()
            except Exception:
                break
            # CCM returns the empty string or the word "None" when there is no
            # further predecessor (i.e., this is the root version).
            if not pred or pred.lower() == "none" or pred in seen:
                break
            seen.add(pred)
            chain.append(pred)
            current = pred
        # The chain was built newest-first (current → root); reverse so callers
        # receive versions in chronological (oldest-first) order for sequential
        # check-in into EWM.
        chain.reverse()
        return chain

    # ------------------------------------------------------------------
    # Baselines
    # ------------------------------------------------------------------

    def list_baselines(self, project_spec: str) -> list[dict]:
        fmt = _DELIM.join(["%displayname", "%objectname", "%release",
                           "%status", "%create_time", "%description"])
        cmd = ["baseline", "-list", "-project", project_spec, "-format", fmt]
        raw = self._run(cmd)
        keys = ["name", "spec", "release", "status", "create_time", "description"]
        return _parse_delimited(raw, keys)

    def list_baseline_members(self, baseline_spec: str) -> list[dict]:
        fmt = _DELIM.join(["%name", "%version", "%type", "%objectname"])
        cmd = ["baseline", "-show_members", baseline_spec, "-format", fmt]
        raw = self._run(cmd)
        keys = ["name", "version", "obj_type", "spec"]
        return _parse_delimited(raw, keys)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run(self, args: list[str]) -> str:
        cmd = [self.ccm_exe] + args
        result = self._run_raw(cmd)
        return result.stdout

    def _run_raw(self, cmd: list[str]) -> subprocess.CompletedProcess:
        # Inject CCM_HOME so this session's commands don't interfere with
        # other ccm processes running under the same OS user.
        env = None
        if self._home:
            import os
            env = os.environ.copy()
            env["CCM_HOME"] = self._home

        log.debug("ccm: %s", " ".join(cmd))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            # CCM returns exit code 1 for empty result sets (e.g. a query with
            # no matches), which is not an error.  Only treat exit code > 1 as
            # a hard failure.  Always log stderr so the caller can diagnose.
            if result.stderr.strip():
                log.warning("ccm stderr: %s", result.stderr.strip())
            if result.returncode > 1:
                raise CCMError(
                    f"ccm command failed (rc={result.returncode}): "
                    f"{' '.join(cmd)}\n{result.stderr}"
                )
        return result


# ------------------------------------------------------------------
# Parsing helpers
# ------------------------------------------------------------------

def _parse_delimited(raw: str, keys: list[str]) -> list[dict]:
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(_DELIM)
        padded = parts + [""] * (len(keys) - len(parts))
        rows.append(dict(zip(keys, padded)))
    return rows


def _parse_history(raw: str) -> list[dict]:
    """
    Parse ``ccm history`` output for a task.

    Expected block format (Synergy 7.x+)::

        ---- Change by USER on TIMESTAMP ----
          attribute_name: old_value -> new_value
          attribute_name2: old_value2 -> new_value2

    Blocks are delimited by the ``---- Change by`` header line.  All
    indented lines between headers are collected as change strings verbatim.
    Lines that do not fit the pattern are included rather than dropped so no
    history is silently lost.
    """
    entries: list[dict] = []
    current: Optional[dict] = None
    change_lines: list[str] = []

    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("---- Change by "):
            # Save the completed previous block before starting a new one.
            if current is not None:
                current["changes"] = change_lines
                entries.append(current)
            # Header format: "---- Change by USER on TIMESTAMP ----"
            inner = stripped.strip("- ").replace("Change by ", "", 1)
            parts = inner.split(" on ", 1)
            current = {
                "author": parts[0].strip(),
                "timestamp": parts[1].strip() if len(parts) > 1 else "",
                "changes": [],
            }
            change_lines = []
        elif current is not None and stripped:
            change_lines.append(stripped)

    # Flush the last block, which has no trailing header to trigger the save.
    if current is not None:
        current["changes"] = change_lines
        entries.append(current)

    return entries


def _parse_notes(raw: str) -> list[dict]:
    """
    Parse ``ccm task -show_notes`` output.

    Expected format (approximate — varies by CCM version)::

        ---- Note by <user> on <timestamp> ----
        <text lines>
        (blank line separates notes)
    """
    notes: list[dict] = []
    current: Optional[dict] = None
    text_lines: list[str] = []

    for line in raw.splitlines():
        if line.startswith("---- Note by "):
            if current is not None:
                current["text"] = "\n".join(text_lines).strip()
                notes.append(current)
            parts = line.strip("- ").replace("Note by ", "").split(" on ", 1)
            current = {
                "author": parts[0].strip() if parts else "",
                "timestamp": parts[1].strip() if len(parts) > 1 else "",
                "text": "",
            }
            text_lines = []
        elif current is not None:
            text_lines.append(line)

    if current is not None:
        current["text"] = "\n".join(text_lines).strip()
        notes.append(current)

    return notes
