"""
High-level extraction logic: reads from Synergy via CCMClient and produces
typed model objects ready for transformation.
"""
from __future__ import annotations

import datetime
import logging
import mimetypes
from typing import Optional

from .client import CCMClient
from .models import (
    SynergyAttachment,
    SynergyBaseline,
    SynergyComment,
    SynergyHistoryEntry,
    SynergyObject,
    SynergyTask,
)

log = logging.getLogger(__name__)

# Sentinel object used to distinguish "caller did not pass a release" from
# "caller explicitly passed None (= no release filter)".  We cannot use None
# itself because None is a valid value meaning "omit the release clause".
_UNSET: object = object()

# CCM timestamp formats tried in order when applying a `since` date filter.
# Multiple formats exist because different CCM versions and locales produce
# different output from the same %create_time / %modify_time format token.
_CCM_TS_FORMATS = [
    "%d %b %Y %H:%M:%S",       # 15 Mar 2025 10:30:00  (most common)
    "%a %b %d %H:%M:%S %Y",    # Mon Mar 15 10:30:00 2025  (C ctime style)
    "%Y-%m-%dT%H:%M:%S",       # 2025-03-15T10:30:00  (ISO 8601)
    "%Y-%m-%d %H:%M:%S",       # 2025-03-15 10:30:00
    "%Y-%m-%d",                 # 2025-03-15  (date-only fallback)
]

# Attributes fetched for every task/defect row via ccm query.
# Kept at module level so _build_task() can use it to separate known from
# custom attributes without duplicating the list.
_TASK_QUERY_ATTRS = [
    "task_number", "displayname", "synopsis", "description",
    "status", "priority", "severity", "task_subsystem",
    "submitter", "resolver", "release", "project",
    "create_time", "modify_time",
]


def _is_on_or_after(timestamp: str, since: str) -> bool:
    """
    Return True when ``timestamp`` falls on or after the ``since`` date.

    Fails open: returns True if either value cannot be parsed so that items
    are never silently dropped due to an unrecognised timestamp format.
    Including an item unnecessarily is less harmful than missing it.
    """
    try:
        since_date = datetime.datetime.strptime(since, "%Y-%m-%d").date()
    except ValueError:
        return True  # malformed since — include everything
    for fmt in _CCM_TS_FORMATS:
        try:
            return datetime.datetime.strptime(timestamp.strip(), fmt).date() >= since_date
        except ValueError:
            continue
    return True  # unrecognised timestamp format — include rather than silently drop


class SynergyExtractor:
    """
    Extracts Synergy data and returns typed model objects.

    Parameters
    ----------
    client:
        An already-started CCMClient instance.
    project:
        Optional project spec to scope queries.
    release:
        Default release filter for task/defect queries.  Individual calls to
        extract_tasks() can override this with the ``release`` parameter.
    extra_query:
        Extra CCM query fragment ANDed into every task query.
    fetch_file_content:
        When True, fetch raw bytes for each source artifact (can be slow for
        large projects; set False when only metadata is needed).
    """

    def __init__(
        self,
        client: CCMClient,
        project: Optional[str] = None,
        release: Optional[str] = None,
        extra_query: str = "",
        fetch_file_content: bool = True,
    ) -> None:
        self.client = client
        self.project = project
        self.release = release
        self.extra_query = extra_query
        self.fetch_file_content = fetch_file_content

    # ------------------------------------------------------------------
    # Tasks / defects
    # ------------------------------------------------------------------

    def extract_tasks(self, task_type: str = "task", release: object = _UNSET) -> list[SynergyTask]:
        """
        Extract all tasks (or defects) matching the configured scope.

        Parameters
        ----------
        task_type:
            CCM type string: "task" or "defect".
        release:
            Override the instance-level release filter for this call.
            Pass ``None`` explicitly to suppress any release filter.
            Omit (or pass ``_UNSET``) to use the release set at construction.
        """
        # Resolve which release to filter by, honouring the sentinel.
        effective_release = self.release if release is _UNSET else release
        q = f"type='{task_type}'"
        if effective_release:
            q += f" and release='{effective_release}'"
        if self.project:
            q += f" and project='{self.project}'"
        if self.extra_query:
            q += f" and ({self.extra_query})"

        log.info("Querying Synergy: %s", q)
        rows = self.client.query(q, _TASK_QUERY_ATTRS)
        log.info("Found %d %s records", len(rows), task_type)

        tasks: list[SynergyTask] = []
        for row in rows:
            spec = row.get("displayname", "")
            try:
                task = self._build_task(spec, row)
                tasks.append(task)
            except Exception:
                log.exception("Failed to extract task %s — skipping", spec)
        return tasks

    def extract_defects(self) -> list[SynergyTask]:
        return self.extract_tasks(task_type="defect")

    def _build_task(self, spec: str, row: dict) -> SynergyTask:
        # Fetch all attributes to capture any custom fields not in the query.
        try:
            all_attrs = self.client.get_attributes(spec)
        except Exception:
            log.debug("Could not fetch full attributes for %s", spec)
            all_attrs = {}

        # Known attributes are handled by the mapper; everything else becomes
        # custom_attrs so it is preserved in EWM without explicit mapping rules.
        known = set(_TASK_QUERY_ATTRS + ["displayname"])
        custom = {k: v for k, v in all_attrs.items() if k not in known}

        comments = self._extract_comments(spec)
        attachments = self._extract_attachments(spec)
        history = self._extract_history(spec)
        change_requests = self._extract_change_requests(spec)

        return SynergyTask(
            spec=spec,
            task_number=row.get("task_number", ""),
            synopsis=row.get("synopsis", ""),
            description=row.get("description", ""),
            # The task type is embedded in the CCM spec after the first tilde
            # (e.g. "task1~defect~1:user:db" → "defect").  Fall back to "task"
            # when the spec doesn't follow this convention.
            task_type=row.get("displayname", "task").split("~")[1] if "~" in spec else "task",
            status=row.get("status", ""),
            priority=row.get("priority", ""),
            severity=row.get("severity", ""),
            submitter=row.get("submitter", ""),
            resolver=row.get("resolver", ""),
            release=row.get("release", ""),
            project=row.get("project", ""),
            create_time=row.get("create_time", ""),
            modify_time=row.get("modify_time", ""),
            custom_attrs=custom,
            comments=comments,
            attachments=attachments,
            history=history,
            change_requests=change_requests,
        )

    # ------------------------------------------------------------------
    # Comments / notes
    # ------------------------------------------------------------------

    def _extract_comments(self, task_spec: str) -> list[SynergyComment]:
        try:
            raw_notes = self.client.get_task_notes(task_spec)
            return [
                SynergyComment(
                    author=n.get("author", ""),
                    timestamp=n.get("timestamp", ""),
                    text=n.get("text", ""),
                )
                for n in raw_notes
            ]
        except Exception:
            # Notes are non-critical; a failure here should not abort the task.
            log.debug("Could not fetch notes for %s", task_spec)
            return []

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    def _extract_attachments(self, task_spec: str) -> list[SynergyAttachment]:
        attachments: list[SynergyAttachment] = []
        try:
            meta_list = self.client.get_attachments(task_spec)
        except Exception:
            log.debug("Could not fetch attachments for %s", task_spec)
            return []

        for meta in meta_list:
            try:
                content = self.client.get_file_content(meta["spec"])
                mime, _ = mimetypes.guess_type(meta["name"])
                attachments.append(
                    SynergyAttachment(
                        name=meta["name"],
                        content=content,
                        mime_type=mime or "application/octet-stream",
                    )
                )
            except Exception:
                # A single bad attachment should not abort the entire task.
                log.warning("Could not fetch attachment %s — skipping", meta.get("name"))
        return attachments

    # ------------------------------------------------------------------
    # Change history
    # ------------------------------------------------------------------

    def _extract_history(self, task_spec: str) -> list[SynergyHistoryEntry]:
        """
        Extract the field-change audit trail for a task.

        History entries are returned in the order CCM emits them (typically
        newest-first from ``ccm history``).  The mapper reverses this when
        prepending history as EWM comments so the oldest change appears first.
        """
        try:
            raw_entries = self.client.get_task_history(task_spec)
            return [
                SynergyHistoryEntry(
                    author=e.get("author", ""),
                    timestamp=e.get("timestamp", ""),
                    changes=e.get("changes", []),
                )
                for e in raw_entries
            ]
        except Exception:
            # History is optional metadata; failure here must not block migration.
            log.debug("Could not fetch history for %s", task_spec)
            return []

    # ------------------------------------------------------------------
    # Change requests
    # ------------------------------------------------------------------

    def _extract_change_requests(self, task_spec: str) -> list[str]:
        """
        Return the list of Change Request specs associated with a task.

        Queries Synergy for all objects whose ``has_associated_task`` relationship
        points to this task spec.  Each returned displayname is one CR object spec
        (e.g. ``cr42~problem_report~1:admin:mydb``).  An empty list is returned
        when no CRs are found or the query fails — CRs are optional and their
        absence must never block the migration.
        """
        try:
            rows = self.client.query(
                f"has_associated_task('{task_spec}')",
                ["displayname", "synopsis"],
            )
            crs = []
            for r in rows:
                spec = r.get("displayname", "")
                if not spec:
                    continue
                synopsis = r.get("synopsis", "").strip()
                crs.append(f"{spec}: {synopsis}" if synopsis else spec)
            return crs
        except Exception:
            log.debug("Could not fetch change requests for %s", task_spec)
            return []

    # ------------------------------------------------------------------
    # Source artifacts
    # ------------------------------------------------------------------

    def extract_artifacts(
        self,
        project_spec: Optional[str] = None,
        since: Optional[str] = None,
    ) -> list[SynergyObject]:
        """
        Extract all versioned source objects from a Synergy project, including
        every historical version of each file (oldest-first within each file).

        Parameters
        ----------
        since:
            ISO date string "YYYY-MM-DD".  When set, only objects whose
            modify_time is on or after this date are expanded.  Objects modified
            before this date are assumed already migrated and are skipped entirely
            — including their version chains — for performance.
        """
        proj = project_spec or self.project
        if not proj:
            raise ValueError("A project spec is required to extract artifacts")

        log.info("Listing objects in project %s", proj)
        rows = self.client.list_objects(proj, recursive=True)
        log.info("Found %d objects in project (will expand version chains)", len(rows))

        if since:
            before = len(rows)
            # Filter on the current version's modify_time.  If the current version
            # is old enough to be filtered out, all its predecessors are too, so
            # it is safe to skip the entire chain rather than inspecting each version.
            rows = [r for r in rows if _is_on_or_after(r.get("modify_time", ""), since)]
            log.info("After since=%s filter: %d/%d objects remain", since, len(rows), before)

        objects: list[SynergyObject] = []
        for row in rows:
            versions = self._expand_versions(row, proj)
            objects.extend(versions)

        log.info("Total versioned objects to migrate: %d", len(objects))
        return objects

    def _expand_versions(self, current_row: dict, proj: str) -> list[SynergyObject]:
        """
        Given one row from list_objects(), walk the predecessor chain to build
        a SynergyObject for every historical version, oldest-first.

        Directories and project objects have no meaningful content to check in
        and are excluded entirely — EWM creates directory entries implicitly
        when files are checked in at nested paths.
        """
        if current_row.get("obj_type", "") in ("dir", "project"):
            return []

        current_spec = current_row.get("spec", "")
        if not current_spec:
            return []

        try:
            version_specs = self.client.get_object_versions(current_spec)
        except Exception:
            # If predecessor traversal fails, fall back to the single current
            # version rather than losing the object entirely.
            log.debug("Could not expand versions for %s, using current only", current_spec)
            version_specs = [current_spec]

        # Resolve the logical path once — it is the same for all versions of a
        # file because the path is determined by project structure, not version.
        path = ""
        try:
            path = self.client.get_object_path(current_spec, proj)
        except Exception:
            path = current_row.get("name", "")

        results: list[SynergyObject] = []
        for spec in version_specs:
            if spec == current_spec:
                # Reuse the already-fetched row to avoid a redundant CCM call.
                attrs = current_row
                obj_type = attrs.get("obj_type", "")
            else:
                try:
                    raw = self.client.get_attributes(spec)
                    # get_attributes() returns "type" (the raw CCM attribute name)
                    # whereas list_objects() maps it to "obj_type".  Normalise here.
                    attrs = raw
                    obj_type = raw.get("type", current_row.get("obj_type", ""))
                except Exception:
                    log.debug("Could not get attributes for %s — skipping version", spec)
                    continue

            content: Optional[bytes] = None
            if self.fetch_file_content:
                try:
                    content = self.client.get_file_content(spec)
                except Exception:
                    # A version whose content cannot be fetched is skipped rather
                    # than blocking all subsequent versions of the same file.
                    log.warning("Could not fetch content for %s — skipping version", spec)
                    continue

            results.append(
                SynergyObject(
                    spec=spec,
                    name=attrs.get("name", current_row.get("name", "")),
                    version=attrs.get("version", ""),
                    obj_type=obj_type,
                    status=attrs.get("status", ""),
                    owner=attrs.get("owner", ""),
                    create_time=attrs.get("create_time", ""),
                    modify_time=attrs.get("modify_time", ""),
                    project=proj,
                    path=path,
                    content=content,
                )
            )
        return results

    # ------------------------------------------------------------------
    # Baselines
    # ------------------------------------------------------------------

    def extract_baselines(
        self,
        project_spec: Optional[str] = None,
        releases: Optional[list] = None,
    ) -> list[SynergyBaseline]:
        """
        Extract baselines for a Synergy project.

        Parameters
        ----------
        releases:
            When provided (and contains at least one non-None value), only
            baselines whose ``release`` attribute matches one of the listed
            values are returned.  Pass ``None`` or an all-None list to include
            all baselines regardless of release.
        """
        proj = project_spec or self.project
        if not proj:
            raise ValueError("A project spec is required to extract baselines")

        log.info("Listing baselines in project %s", proj)
        rows = self.client.list_baselines(proj)
        log.info("Found %d baselines", len(rows))

        # Build a set of release values to filter against, ignoring any None
        # entries (which represent "no release filter" from the releases list).
        release_filter = {r for r in (releases or []) if r is not None}
        if release_filter:
            rows = [r for r in rows if r.get("release", "") in release_filter]
            log.info("After release filter (%s): %d baselines", sorted(release_filter), len(rows))

        baselines: list[SynergyBaseline] = []
        for row in rows:
            spec = row.get("spec", "")
            try:
                member_rows = self.client.list_baseline_members(spec)
            except Exception:
                log.debug("Could not list members for baseline %s", spec)
                member_rows = []

            members = [
                SynergyObject(
                    spec=m.get("spec", ""),
                    name=m.get("name", ""),
                    version=m.get("version", ""),
                    obj_type=m.get("obj_type", ""),
                    status="",
                    owner="",
                    create_time="",
                    modify_time="",
                    project=proj,
                    # Baseline members don't always have a full path; use the
                    # name as a reasonable fallback.
                    path=m.get("name", ""),
                )
                for m in member_rows
            ]

            baselines.append(
                SynergyBaseline(
                    spec=spec,
                    name=row.get("name", ""),
                    project=proj,
                    release=row.get("release", ""),
                    status=row.get("status", ""),
                    create_time=row.get("create_time", ""),
                    description=row.get("description", ""),
                    objects=members,
                )
            )
        return baselines
