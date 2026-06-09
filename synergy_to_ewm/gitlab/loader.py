"""
High-level GitLab loader.

Orchestrates milestone, issue, and source-code loading from pre-extracted
Synergy data.  Work items go through the REST API; source files are pushed
via the git CLI.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

from ..migrate import MigrationState
from ..transform.mapper import ArtifactMapper, BaselineMapper, load_mapping
from ..transform.gitlab_mapper import GitLabMapper
from .client import GitLabClient, GitLabAPIError
from .git_client import GitClient

log = logging.getLogger(__name__)


class GitLabLoader:
    """
    Loads Synergy data extracted by ``synergy-extract`` into GitLab.

    Milestones are created from baselines before issues so that issues can
    reference their corresponding milestone.  Source code is pushed via git
    after all issues are created.
    """

    def __init__(self, gitlab_cfg, migration_raw: dict) -> None:
        self.cfg = gitlab_cfg
        self.dry_run = migration_raw.get("dry_run", False)
        self.migrate_tasks = migration_raw.get("migrate_tasks", True)
        self.migrate_artifacts = migration_raw.get("migrate_artifacts", True)
        self.migrate_baselines = migration_raw.get("migrate_baselines", True)
        self._state = MigrationState(migration_raw.get("state_file", "migration_state.json"))
        self._mapping = load_mapping(
            override_file=migration_raw.get("mapping_file"),
            extra_overrides=migration_raw.get("field_overrides") or {},
        )
        self._mapper = GitLabMapper(self._mapping)
        self._artifact_mapper = ArtifactMapper()
        self._baseline_mapper = BaselineMapper(self._artifact_mapper)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(
        self,
        tasks: list,
        defects: list,
        artifacts: list,
        baselines: list,
    ) -> dict:
        stats: dict = dict(
            tasks_ok=0, tasks_skipped=0, tasks_failed=0,
            milestones_ok=0, baselines_ok=0, artifacts_ok=0, errors=[],
        )

        try:
            client = GitLabClient(
                server=self.cfg.server,
                project=self.cfg.project,
                token=self.cfg.token,
                verify_ssl=self.cfg.verify_ssl,
                ca_bundle=self.cfg.ca_bundle,
            )
            # Resolve project ID early so auth problems surface immediately.
            client.get_project_id()

            # 1. Create milestones so issues can reference them by ID.
            milestone_map: dict[str, int] = {}
            if self.migrate_baselines and baselines:
                milestone_map = self._create_milestones(client, baselines, stats)

            # 2. Issues from tasks and defects.
            if self.migrate_tasks:
                self._load_issues(client, tasks, stats, milestone_map)
                self._load_issues(client, defects, stats, milestone_map)

            # 3. Source code via git.
            needs_git = (
                (self.migrate_baselines and baselines)
                or (self.migrate_artifacts and artifacts)
            )
            if needs_git:
                self._push_source(client, artifacts, baselines, stats)

        except KeyboardInterrupt:
            log.warning("GitLab load interrupted — partial progress saved to state file")
            stats["errors"].append("interrupted")
        except Exception:
            log.exception("Fatal error during GitLab load")
            stats["errors"].append("fatal")
        finally:
            failed = self._state.error_count()
            if failed:
                log.warning(
                    "%d item(s) failed — fix the cause and re-run to retry automatically.",
                    failed,
                )

        log.info("GitLab load complete: %s", stats)
        return stats

    # ------------------------------------------------------------------
    # Milestones
    # ------------------------------------------------------------------

    def _create_milestones(
        self,
        client: GitLabClient,
        baselines: list,
        stats: dict,
    ) -> dict[str, int]:
        log.info("=== Creating %d GitLab milestones ===", len(baselines))
        milestone_map: dict[str, int] = {}

        # Pre-load existing milestones so we don't duplicate them on a re-run.
        try:
            for m in client.list_milestones():
                milestone_map[m["title"]] = m["id"]
        except Exception:
            log.warning("Could not list existing milestones — may create duplicates on re-run")

        for baseline in baselines:
            state_key = f"gitlab_milestone:{baseline.name}"
            if self._state.is_done(state_key):
                # Ensure the ID is in the map even though we already created it.
                if baseline.name not in milestone_map:
                    log.debug("Milestone '%s' already in state but not on server — will re-create",
                               baseline.name)
                else:
                    continue

            if baseline.name in milestone_map:
                log.debug("Milestone '%s' already exists — reusing id=%d",
                          baseline.name, milestone_map[baseline.name])
                self._state.mark_done(state_key, str(milestone_map[baseline.name]))
                stats["milestones_ok"] += 1
                continue

            try:
                gl_milestone = self._mapper.map_baseline(baseline)
                if self.dry_run:
                    log.info("[DRY RUN] Would create milestone '%s'", gl_milestone.title)
                    self._state.mark_done(state_key, "dry-run")
                    stats["milestones_ok"] += 1
                    continue

                mid = client.create_milestone(
                    gl_milestone.title,
                    gl_milestone.description,
                    gl_milestone.due_date or None,
                )
                milestone_map[baseline.name] = mid
                self._state.mark_done(state_key, str(mid))
                stats["milestones_ok"] += 1
                log.info("Created milestone '%s' → id=%d", baseline.name, mid)
            except Exception as exc:
                log.exception("Failed to create milestone for baseline %s", baseline.name)
                self._state.mark_failed(state_key, repr(exc))
                stats["errors"].append(state_key)

        return milestone_map

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    def _load_issues(
        self,
        client: GitLabClient,
        tasks: list,
        stats: dict,
        milestone_map: dict[str, int],
    ) -> None:
        log.info("=== Loading %d issues ===", len(tasks))
        for task in tasks:
            state_key = f"gitlab_issue:{task.task_number}"
            if self._state.is_done(state_key):
                log.debug("Skipping issue %s (already created)", task.task_number)
                stats["tasks_skipped"] += 1
                continue

            try:
                issue = self._mapper.map_task(task, milestone_map)

                if self.dry_run:
                    log.info("[DRY RUN] Would create issue: %s", issue.title)
                    self._state.mark_done(state_key, "dry-run")
                    stats["tasks_ok"] += 1
                    continue

                # Upload attachments and collect markdown links to append to description.
                attachment_links: list[str] = []
                for att in issue.attachments:
                    try:
                        link = client.upload_file(att.name, att.content, att.mime_type)
                        if link:
                            attachment_links.append(link)
                    except Exception:
                        log.warning("Failed to upload attachment %s for task %s",
                                    att.name, task.task_number)

                description = issue.description
                if attachment_links:
                    description += "\n\n**Attachments:**\n" + "\n".join(attachment_links)

                result = client.create_issue(
                    title=issue.title,
                    description=description,
                    labels=issue.labels,
                    milestone_id=issue.milestone_id,
                    created_at=issue.created_at,
                )
                iid = result["iid"]
                issue_url = result.get("web_url", "")

                for note in issue.notes:
                    try:
                        client.create_note(iid, note.body, note.created_at)
                    except Exception:
                        log.warning("Failed to add note to issue %s", iid)

                self._state.mark_done(state_key, issue_url)
                stats["tasks_ok"] += 1
                log.info("Created issue #%d: %s", iid, issue.title)

            except Exception as exc:
                log.exception("Unhandled error for task %s", task.task_number)
                self._state.mark_failed(state_key, repr(exc))
                stats["tasks_failed"] += 1
                stats["errors"].append(task.task_number)

    # ------------------------------------------------------------------
    # Source code via git
    # ------------------------------------------------------------------

    def _push_source(
        self,
        client: GitLabClient,
        artifacts: list,
        baselines: list,
        stats: dict,
    ) -> None:
        log.info("=== Pushing source code to GitLab via git ===")

        remote_url = self._build_git_url(client)
        workdir = Path(self.cfg.git_workdir).resolve()

        git = GitClient(
            remote_url=remote_url,
            branch=self.cfg.default_branch,
        )

        if self.dry_run:
            log.info("[DRY RUN] Would push source to %s (branch: %s)",
                     self.cfg.project, self.cfg.default_branch)
            return

        git.init_or_clone(workdir)
        pushed_something = False

        # Baselines — one commit + tag per baseline.
        if self.migrate_baselines and baselines:
            for baseline in baselines:
                state_key = f"gitlab_git:{baseline.spec}"
                if self._state.is_done(state_key):
                    log.debug("Skipping git baseline %s (already pushed)", baseline.name)
                    continue

                ewm_baseline = self._baseline_mapper.map(
                    baseline,
                    self.cfg.project.split("/")[-1],
                )
                written = git.write_artifacts(ewm_baseline.artifacts, workdir)
                if written == 0:
                    log.debug("Baseline '%s' has no file content — skipping commit", baseline.name)
                    self._state.mark_done(state_key, "no-content")
                    stats["baselines_ok"] += 1
                    continue

                committed = git.stage_and_commit(
                    workdir,
                    message=f"Baseline: {baseline.name}\n\n{baseline.description}".strip(),
                )
                if committed:
                    git.tag(workdir, baseline.name, baseline.description or baseline.name)
                    self._state.mark_done(state_key, baseline.name)
                    stats["baselines_ok"] += 1
                    stats["artifacts_ok"] += written
                    pushed_something = True

        # Loose artifacts — single commit.
        elif self.migrate_artifacts and artifacts:
            pending = [o for o in artifacts
                       if not self._state.is_done(f"gitlab_git_artifact:{o.spec}")]
            if pending:
                mapped = [(o, self._artifact_mapper.map(o)) for o in pending]
                pairs = [(o, a) for o, a in mapped if a is not None]
                written = git.write_artifacts([a for _, a in pairs], workdir)
                if written:
                    committed = git.stage_and_commit(workdir, "Migration from Synergy")
                    if committed:
                        for obj, _ in pairs:
                            self._state.mark_done(f"gitlab_git_artifact:{obj.spec}", "pushed")
                            stats["artifacts_ok"] += 1
                        pushed_something = True

        if pushed_something:
            git.push(workdir)
            log.info("Source code pushed to GitLab project '%s'", self.cfg.project)

    def _build_git_url(self, client: GitLabClient) -> str:
        token = client.resolve_token()
        parsed = urlparse(self.cfg.server)
        netloc = f"oauth2:{token}@{parsed.netloc}"
        path = f"/{self.cfg.project}.git"
        return urlunparse((parsed.scheme, netloc, path, "", "", ""))
