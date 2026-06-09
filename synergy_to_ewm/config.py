"""
Configuration dataclasses for the Synergy → EWM migration module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SynergyConfig:
    """Connection settings for IBM Rational Synergy (ccm CLI)."""

    server: str               # e.g. "http://synergy-host:8400"
    database: str             # Synergy database path or name
    user: str
    password: Optional[str] = None  # omit to use credentials stored via `ccm set_password`
    ccm_exe: str = "ccm"      # full path if ccm is not on PATH
    # Optional scope filters
    project: Optional[str] = None    # Synergy project spec to scope extraction
    release: Optional[str] = None    # Single release (legacy; use releases for multi)
    releases: list = field(default_factory=list)  # Multiple releases; supersedes release
    since: Optional[str] = None      # ISO date "YYYY-MM-DD"; skip items unmodified before this
    query_extra: str = ""            # Extra CCM query fragment ANDed in


@dataclass
class EWMConfig:
    """Connection settings for IBM Engineering Workflow Management (EWM / RTC)."""

    server: str               # e.g. "https://ewm-host:9443/ccm"
    user: str
    project_area: str         # EWM Project Area name
    password: Optional[str] = None  # omit to be prompted; credential can be saved to the system keyring
    # Optional component/stream to target for SCM migration
    component_name: Optional[str] = None
    stream_name: Optional[str] = None
    # TLS
    verify_ssl: bool = True
    ca_bundle: Optional[str] = None
    # SCM backend: 'rest' (default) or 'cli' (IBM Jazz SCM command-line tool)
    scm_backend: str = "rest"
    scm_exe: str = "scm"    # path to the 'scm' executable; only used when scm_backend='cli'


@dataclass
class GitLabConfig:
    """Connection settings for GitLab (issue tracking + git source control)."""

    server: str               # e.g. "https://gitlab.com" or self-hosted URL
    project: str              # "namespace/project-name" or numeric project ID
    token: Optional[str] = None   # Personal/Project Access Token; falls back to GITLAB_TOKEN env var
    default_branch: str = "main"
    git_workdir: str = "gitlab_migration_repo"  # persistent local clone directory
    verify_ssl: bool = True
    ca_bundle: Optional[str] = None


@dataclass
class MigrationConfig:
    """Top-level migration settings."""

    synergy: SynergyConfig
    ewm: EWMConfig
    mapping_file: Optional[str] = None     # path to override YAML mapping
    state_file: str = "migration_state.json"
    log_file: Optional[str] = "migration.log"
    batch_size: int = 50                   # work items per API batch
    # What to migrate
    migrate_tasks: bool = True
    migrate_artifacts: bool = True
    migrate_baselines: bool = True
    migrate_attachments: bool = True
    migrate_comments: bool = True
    dry_run: bool = False
    # Extra mapping overrides as plain dicts (merged on top of YAML)
    field_overrides: dict = field(default_factory=dict)
