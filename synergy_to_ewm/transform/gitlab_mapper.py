"""
Transforms Synergy model objects into GitLab model objects.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..synergy.models import SynergyBaseline, SynergyTask
from ..gitlab.models import GitLabAttachment, GitLabIssue, GitLabMilestone, GitLabNote

log = logging.getLogger(__name__)


class GitLabMapper:
    """Maps Synergy tasks and baselines to their GitLab equivalents."""

    def __init__(self, mapping: dict) -> None:
        self._m = mapping.get("work_item", {})

    def map_task(
        self,
        task: SynergyTask,
        milestone_map: Optional[dict[str, int]] = None,
    ) -> GitLabIssue:
        """
        Convert a SynergyTask to a GitLabIssue.

        Parameters
        ----------
        milestone_map:
            Dict mapping Synergy release names → GitLab milestone IDs.
            Used to attach issues to the corresponding milestone.
        """
        labels = self._labels_for(task)
        milestone_id = (milestone_map or {}).get(task.release)

        notes = []
        # History entries prepended (oldest-first) as a chronological audit trail.
        for h in reversed(task.history):
            if h.changes:
                body = "[History] " + " | ".join(h.changes)
            else:
                body = "[History entry]"
            notes.append(GitLabNote(body=body, created_at=h.timestamp))
        for c in task.comments:
            notes.append(GitLabNote(body=c.text, created_at=c.timestamp))

        attachments = [
            GitLabAttachment(name=a.name, content=a.content, mime_type=a.mime_type)
            for a in task.attachments
        ]

        return GitLabIssue(
            title=f"[{task.task_number}] {task.synopsis}",
            description=_build_description(task),
            labels=labels,
            milestone_id=milestone_id,
            created_at=task.create_time,
            notes=notes,
            attachments=attachments,
            source_id=task.task_number,
        )

    def map_baseline(self, baseline: SynergyBaseline) -> GitLabMilestone:
        return GitLabMilestone(
            title=baseline.name,
            description=(
                f"Migrated from Synergy baseline {baseline.name} "
                f"(release: {baseline.release}). {baseline.description}"
            ).strip(),
            source_spec=baseline.spec,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _labels_for(self, task: SynergyTask) -> list[str]:
        labels: list[str] = []

        type_map = self._m.get("type_map", {})
        ewm_type = type_map.get(task.task_type.lower(), type_map.get("default", ""))
        if ewm_type:
            labels.append(f"type::{ewm_type.lower().replace(' ', '-')}")

        status_map = self._m.get("status_map", {})
        ewm_status = status_map.get(task.status.lower(), "")
        if ewm_status:
            labels.append(f"status::{ewm_status.lower().replace(' ', '-')}")

        priority_map = self._m.get("priority_map", {})
        ewm_priority = priority_map.get(task.priority.lower(), "")
        if ewm_priority:
            labels.append(f"priority::{ewm_priority.lower()}")

        labels.append("migrated-from-synergy")
        return labels


def _build_description(task: SynergyTask) -> str:
    lines = [task.description or ""]
    lines += [
        "",
        "---",
        "**Migrated from IBM Rational Synergy**",
        f"- Task number: {task.task_number}",
        f"- Type: {task.task_type}",
        f"- Original status: {task.status}",
        f"- Release: {task.release}",
        f"- Submitter: {task.submitter}",
        f"- Resolver: {task.resolver}",
    ]
    if task.custom_attrs:
        lines.append("")
        lines.append("**Additional attributes:**")
        for k, v in sorted(task.custom_attrs.items()):
            lines.append(f"- {k}: {v}")
    return "\n".join(lines)
