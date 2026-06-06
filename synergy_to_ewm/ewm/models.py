"""
Dataclasses representing EWM (IBM Engineering Workflow Management) entities
ready for loading via the OSLC / REST API.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class EWMComment:
    author: str
    timestamp: str
    text: str


@dataclass
class EWMAttachment:
    name: str
    content: bytes
    mime_type: str = "application/octet-stream"


@dataclass
class EWMWorkItem:
    """Maps to an EWM work item (story, defect, task, etc.)."""

    title: str
    description: str
    work_item_type: str          # EWM work item type slug, e.g. "defect", "task"
    status: str                  # EWM workflow state label
    priority: str
    severity: str
    filed_against: str           # EWM category / component
    owned_by: str                # EWM user ID
    submitted_by: str
    created: str                 # ISO-8601 timestamp
    modified: str
    tags: list[str] = field(default_factory=list)
    custom_attrs: dict = field(default_factory=dict)
    comments: list[EWMComment] = field(default_factory=list)
    attachments: list[EWMAttachment] = field(default_factory=list)
    # Populated after load
    ewm_id: Optional[str] = None
    ewm_url: Optional[str] = None
    # Back-reference for state tracking
    source_id: str = ""


@dataclass
class EWMArtifact:
    """A file to be checked in to an EWM SCM component."""

    path: str                    # logical path within the component
    name: str
    content: bytes
    mime_type: str = "application/octet-stream"
    source_spec: str = ""
    comment: str = ""            # changeset description (e.g. "Migrated from Synergy: ...")
    author: str = ""             # original author from Synergy


@dataclass
class EWMBaseline:
    """An EWM SCM baseline / snapshot."""

    name: str
    description: str
    component_name: str
    created: str
    artifacts: list[EWMArtifact] = field(default_factory=list)
    # Populated after load
    ewm_id: Optional[str] = None
    source_spec: str = ""
