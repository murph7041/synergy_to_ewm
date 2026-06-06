"""
Dataclasses representing data extracted from IBM Rational Synergy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SynergyComment:
    author: str
    timestamp: str
    text: str


@dataclass
class SynergyHistoryEntry:
    """One entry in a task's field-change audit trail."""

    author: str
    timestamp: str
    # Each string describes one attribute change in the form
    # "attribute_name: old_value -> new_value" as emitted by `ccm history`.
    changes: list[str]


@dataclass
class SynergyAttachment:
    name: str
    content: bytes
    mime_type: str = "application/octet-stream"


@dataclass
class SynergyTask:
    spec: str                      # full CCM object spec, e.g. "task1~task~1:user:db"
    task_number: str               # used as the state-file key — unique within a database
    synopsis: str
    description: str
    task_type: str                 # "task", "defect", etc.
    status: str
    priority: str
    severity: str
    submitter: str
    resolver: str
    release: str
    project: str
    create_time: str
    modify_time: str
    custom_attrs: dict = field(default_factory=dict)
    comments: list[SynergyComment] = field(default_factory=list)
    attachments: list[SynergyAttachment] = field(default_factory=list)
    # Change history entries in the order CCM emits them (typically newest-first).
    # The mapper reverses them when writing EWM comments so the timeline reads
    # chronologically.
    history: list[SynergyHistoryEntry] = field(default_factory=list)


@dataclass
class SynergyObject:
    """A versioned source artifact (file or directory) in Synergy."""

    spec: str                      # full CCM object spec — unique per version
    name: str
    version: str
    obj_type: str                  # "ascii_file", "binary_file", "dir", etc.
    status: str
    owner: str
    create_time: str
    modify_time: str
    project: str
    path: str                      # logical path within the project (same for all versions)
    content: Optional[bytes] = None


@dataclass
class SynergyBaseline:
    spec: str
    name: str
    project: str
    release: str
    status: str
    create_time: str
    description: str
    objects: list[SynergyObject] = field(default_factory=list)
