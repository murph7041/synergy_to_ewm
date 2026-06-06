"""
Transforms Synergy model objects into EWM model objects using a field mapping.

Mapping is loaded from the bundled default YAML and optionally merged with
a user-supplied YAML file and/or a plain dict of overrides.
"""
from __future__ import annotations

import importlib.resources
import logging
import mimetypes
from pathlib import Path
from typing import Optional

import yaml

from ..synergy.models import SynergyBaseline, SynergyObject, SynergyTask
from ..ewm.models import EWMArtifact, EWMBaseline, EWMComment, EWMAttachment, EWMWorkItem

log = logging.getLogger(__name__)

# Path to the bundled default mapping YAML (sibling of this package)
_DEFAULT_MAPPING = Path(__file__).parent.parent / "mapping_default.yaml"


def load_mapping(
    override_file: Optional[str] = None,
    extra_overrides: Optional[dict] = None,
) -> dict:
    """
    Load and merge the field mapping.

    Priority (highest wins): extra_overrides > override_file > default YAML.
    """
    with _DEFAULT_MAPPING.open() as f:
        mapping = yaml.safe_load(f)

    if override_file:
        with open(override_file) as f:
            user_mapping = yaml.safe_load(f) or {}
        mapping = _deep_merge(mapping, user_mapping)

    if extra_overrides:
        mapping = _deep_merge(mapping, extra_overrides)

    return mapping


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


# ------------------------------------------------------------------
# Task → WorkItem
# ------------------------------------------------------------------

class TaskMapper:
    def __init__(self, mapping: dict) -> None:
        self._m = mapping.get("work_item", {})

    def map(self, task: SynergyTask) -> EWMWorkItem:
        type_map = self._m.get("type_map", {})
        ewm_type = type_map.get(task.task_type.lower(),
                                type_map.get("default", "Task"))

        status_map = self._m.get("status_map", {})
        ewm_status = status_map.get(task.status.lower(),
                                    status_map.get("default", "New"))

        priority_map = self._m.get("priority_map", {})
        ewm_priority = priority_map.get(task.priority.lower(),
                                        priority_map.get("default", "Medium"))

        severity_map = self._m.get("severity_map", {})
        ewm_severity = severity_map.get(task.severity.lower(),
                                        severity_map.get("default", "Normal"))

        filed_against = task.project or self._m.get("default_filed_against", "Unassigned")

        # Custom fields
        custom_field_map: dict = self._m.get("custom_field_map", {})
        custom_attrs = {}
        for ewm_attr, syn_attr in custom_field_map.items():
            val = task.custom_attrs.get(syn_attr)
            if val is not None:
                custom_attrs[ewm_attr] = val

        history_comments = [
            EWMComment(
                author=h.author,
                timestamp=h.timestamp,
                text="[History] " + " | ".join(h.changes) if h.changes else "[History entry]",
            )
            for h in task.history
        ]

        comments = history_comments + [
            EWMComment(
                author=c.author,
                timestamp=c.timestamp,
                text=c.text,
            )
            for c in task.comments
        ]

        attachments = [
            EWMAttachment(
                name=a.name,
                content=a.content,
                mime_type=a.mime_type,
            )
            for a in task.attachments
        ]

        return EWMWorkItem(
            title=f"[{task.task_number}] {task.synopsis}",
            description=_build_description(task),
            work_item_type=ewm_type,
            status=ewm_status,
            priority=ewm_priority,
            severity=ewm_severity,
            filed_against=filed_against,
            owned_by=task.resolver or task.submitter,
            submitted_by=task.submitter,
            created=task.create_time,
            modified=task.modify_time,
            custom_attrs=custom_attrs,
            comments=comments,
            attachments=attachments,
            source_id=task.task_number,
        )


def _build_description(task: SynergyTask) -> str:
    """Build rich description preserving Synergy metadata."""
    lines = [task.description or ""]
    lines.append("")
    lines.append("---")
    lines.append(f"**Migrated from IBM Rational Synergy**")
    lines.append(f"- Task number: {task.task_number}")
    lines.append(f"- Type: {task.task_type}")
    lines.append(f"- Original status: {task.status}")
    lines.append(f"- Release: {task.release}")
    lines.append(f"- Submitter: {task.submitter}")
    lines.append(f"- Resolver: {task.resolver}")
    if task.custom_attrs:
        lines.append("")
        lines.append("**Additional attributes:**")
        for k, v in sorted(task.custom_attrs.items()):
            lines.append(f"- {k}: {v}")
    return "\n".join(lines)


# ------------------------------------------------------------------
# SynergyObject → EWMArtifact
# ------------------------------------------------------------------

class ArtifactMapper:
    def map(self, obj: SynergyObject) -> Optional[EWMArtifact]:
        if obj.obj_type in ("dir", "project"):
            return None   # directories are implicit in EWM
        if obj.content is None:
            log.debug("Skipping %s — no content fetched", obj.spec)
            return None

        mime, _ = mimetypes.guess_type(obj.name)
        comment_parts = [f"Migrated from Synergy: {obj.spec}"]
        if obj.version:
            comment_parts.append(f"version={obj.version}")
        if obj.create_time:
            comment_parts.append(f"date={obj.create_time}")
        return EWMArtifact(
            path=obj.path.lstrip("/") or obj.name,
            name=obj.name,
            content=obj.content,
            mime_type=mime or "application/octet-stream",
            source_spec=obj.spec,
            comment=" | ".join(comment_parts),
            author=obj.owner,
        )


# ------------------------------------------------------------------
# SynergyBaseline → EWMBaseline
# ------------------------------------------------------------------

class BaselineMapper:
    def __init__(self, artifact_mapper: Optional[ArtifactMapper] = None) -> None:
        self._artifact_mapper = artifact_mapper or ArtifactMapper()

    def map(self, baseline: SynergyBaseline, component_name: str) -> EWMBaseline:
        artifacts = []
        for obj in baseline.objects:
            artifact = self._artifact_mapper.map(obj)
            if artifact:
                artifacts.append(artifact)

        return EWMBaseline(
            name=baseline.name,
            description=(
                f"Migrated from Synergy baseline {baseline.name} "
                f"(release {baseline.release}). {baseline.description}"
            ).strip(),
            component_name=component_name,
            created=baseline.create_time,
            artifacts=artifacts,
            source_spec=baseline.spec,
        )
