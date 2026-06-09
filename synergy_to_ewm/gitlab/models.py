"""Dataclasses representing GitLab entities ready for loading via the API."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GitLabNote:
    body: str
    created_at: str = ""    # ISO-8601; GitLab back-dates the note when provided


@dataclass
class GitLabAttachment:
    name: str
    content: bytes
    mime_type: str = "application/octet-stream"


@dataclass
class GitLabIssue:
    title: str
    description: str
    labels: list[str] = field(default_factory=list)
    milestone_id: Optional[int] = None
    created_at: str = ""
    notes: list[GitLabNote] = field(default_factory=list)
    attachments: list[GitLabAttachment] = field(default_factory=list)
    source_id: str = ""     # Synergy task_number — used as the state-file key


@dataclass
class GitLabMilestone:
    title: str
    description: str = ""
    due_date: str = ""      # YYYY-MM-DD; optional
    source_spec: str = ""
