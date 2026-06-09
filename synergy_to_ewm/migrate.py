"""
Main migration orchestrator.

Ties together:
  SynergyExtractor → TaskMapper/ArtifactMapper/BaselineMapper → EWMLoader

Supports:
  - Resumable migration via a JSON state file
  - Dry-run mode (no writes to EWM)
  - Per-phase enable/disable flags
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Optional

from .config import MigrationConfig
from .ewm.client import EWMClient
from .ewm.loader import EWMLoader
from .ewm.scm_cli import JazzSCMClient
from .ewm.models import EWMWorkItem
from .synergy.client import CCMClient
from .synergy.extractor import SynergyExtractor
from .transform.mapper import ArtifactMapper, BaselineMapper, TaskMapper, load_mapping

log = logging.getLogger(__name__)


class MigrationState:
    """
    Persists migration progress to a JSON file so runs can be resumed.

    State file layout::

        {
          "done":   { "<source_id>": "<ewm_ref>", ... },
          "errors": { "<source_id>": "<last_error_reason>", ... }
        }

    Legacy flat files (``{ "<source_id>": "<ewm_ref>" }``) are loaded
    transparently and upgraded to the new layout on the next save.

    Items in ``errors`` are retried on the next run because they are NOT
    present in ``done`` — no special "retry" flag is needed.  When a
    previously-failed item succeeds it is moved out of ``errors`` into
    ``done`` automatically by mark_done().
    """

    # State-key conventions used by Migrator:
    #   tasks/defects  →  task_number          (e.g. "1234")
    #   baselines      →  "baseline:<spec>"    (e.g. "baseline:MyBaseline~baseline~1:admin:db")
    #   artifacts      →  "artifact:<spec>"    (e.g. "artifact:foo.c~3:jsmith:db")
    # Using the full CCM spec as the key for baselines/artifacts ensures that
    # two objects with the same name but different versions/databases never
    # collide in the state file.

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._done: dict = {}
        self._errors: dict = {}

        if self._path.exists():
            try:
                with self._path.open() as f:
                    data = json.load(f)
                # Distinguish the new {"done":…, "errors":…} envelope from
                # the original flat dict written by earlier versions of this tool.
                if "done" in data and isinstance(data["done"], dict):
                    self._done = data["done"]
                    self._errors = data.get("errors", {})
                else:
                    # Legacy format: every key is a source_id mapping to an ewm_ref.
                    self._done = data
                log.info(
                    "Loaded state from %s  (%d done, %d previously failed)",
                    path, len(self._done), len(self._errors),
                )
                if self._errors:
                    # Surface the list early so operators can decide whether to
                    # fix the root cause before re-running.
                    log.warning(
                        "%d item(s) failed in a previous run and will be retried: %s",
                        len(self._errors), sorted(self._errors),
                    )
            except (json.JSONDecodeError, ValueError, KeyError):
                # A partial write (e.g. process killed mid-save) can leave the
                # file truncated or with invalid JSON.  Preserve it as .bak so
                # the operator can inspect it, then start fresh.
                log.warning(
                    "State file %s is corrupted — backing it up and starting fresh",
                    path,
                )
                _backup_path = self._path.with_suffix(".bak")
                try:
                    self._path.rename(_backup_path)
                    log.warning("Backed up corrupted state to %s", _backup_path)
                except OSError:
                    log.warning("Could not back up corrupted state file")

    def is_done(self, source_id: str) -> bool:
        return source_id in self._done

    def mark_done(self, source_id: str, ewm_ref: str) -> None:
        self._done[source_id] = ewm_ref
        # If this item previously failed and is now succeeding, remove it from
        # the error record so it no longer appears in the "still failing" list.
        self._errors.pop(source_id, None)
        self._save()

    def mark_failed(self, source_id: str, reason: str) -> None:
        """
        Record a failure for visibility.  The item is NOT added to ``done``,
        so it will be retried automatically on the next run.
        """
        self._errors[source_id] = reason
        self._save()

    def error_count(self) -> int:
        return len(self._errors)

    def _save(self) -> None:
        # Write to a sibling .tmp file then atomically rename over the real
        # file.  This guarantees the on-disk state is always either the old
        # complete file or the new complete file — never a partial write.
        # os.replace() (called by Path.replace()) is atomic on POSIX and
        # effectively atomic on Windows (MoveFileExW) when src and dst are on
        # the same filesystem, which is always true here.
        tmp = self._path.with_suffix(".tmp")
        try:
            with tmp.open("w") as f:
                json.dump({"done": self._done, "errors": self._errors}, f, indent=2)
            tmp.replace(self._path)
        except OSError:
            log.exception("Failed to save state file %s", self._path)


class Migrator:
    """
    Top-level migration controller.

    Usage::

        config = MigrationConfig(synergy=..., ewm=...)
        migrator = Migrator(config)
        stats = migrator.run()
        print(stats)
    """

    def __init__(self, config: MigrationConfig) -> None:
        self.cfg = config
        self._state = MigrationState(config.state_file)
        self._mapping = load_mapping(
            override_file=config.mapping_file,
            extra_overrides=config.field_overrides or {},
        )
        self._task_mapper = TaskMapper(self._mapping)
        self._artifact_mapper = ArtifactMapper()
        self._baseline_mapper = BaselineMapper(self._artifact_mapper)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> dict:
        """
        Execute the full migration.

        Returns
        -------
        dict with keys: tasks_ok, tasks_skipped, tasks_failed,
                        baselines_ok, artifacts_ok, errors
        """
        stats: dict = dict(
            tasks_ok=0, tasks_skipped=0, tasks_failed=0,
            baselines_ok=0, artifacts_ok=0, errors=[]
        )

        syn_cfg = self.cfg.synergy
        ewm_cfg = self.cfg.ewm

        try:
            # Both context managers call stop/close in __exit__, so a mid-run
            # exception still shuts down the CCM session and closes the HTTP
            # session cleanly before the except/finally blocks run.
            with CCMClient(
                server=syn_cfg.server,
                database=syn_cfg.database,
                user=syn_cfg.user,
                password=syn_cfg.password,
                ccm_exe=syn_cfg.ccm_exe,
            ) as ccm_client:

                extractor = SynergyExtractor(
                    client=ccm_client,
                    project=syn_cfg.project,
                    release=syn_cfg.release,
                    extra_query=syn_cfg.query_extra,
                    fetch_file_content=self.cfg.migrate_artifacts,
                )

                with EWMClient(
                    server=ewm_cfg.server,
                    user=ewm_cfg.user,
                    password=ewm_cfg.password,
                    verify_ssl=ewm_cfg.verify_ssl,
                    ca_bundle=ewm_cfg.ca_bundle,
                ) as ewm_client:

                    project_area_id = ewm_client.find_project_area(ewm_cfg.project_area)

                    use_cli = ewm_cfg.scm_backend == "cli"
                    jazz_scm: Optional[JazzSCMClient] = None
                    if use_cli:
                        jazz_scm = JazzSCMClient(
                            server=ewm_cfg.server,
                            user=ewm_cfg.user,
                            password=ewm_cfg.password,
                            scm_exe=ewm_cfg.scm_exe,
                            verify_ssl=ewm_cfg.verify_ssl,
                        )
                        jazz_scm.login()

                    try:
                        component_ref, stream_ref = self._ensure_scm_targets(
                            ewm_client, project_area_id, ewm_cfg, jazz_scm
                        )

                        loader = EWMLoader(
                            client=ewm_client,
                            project_area_id=project_area_id,
                            component_id=None if use_cli else component_ref,
                            stream_id=None if use_cli else stream_ref,
                            dry_run=self.cfg.dry_run,
                        )

                        # Build the canonical release list.
                        # `releases` (multi-value) supersedes the legacy `release` field.
                        # [None] means "no release filter" — extract_tasks() treats None
                        # as "omit the release clause from the CCM query".
                        effective_releases = (
                            syn_cfg.releases
                            or ([syn_cfg.release] if syn_cfg.release else [None])
                        )
                        if len(effective_releases) > 1:
                            log.info("Migrating %d releases: %s", len(effective_releases),
                                     effective_releases)

                        # Tasks and defects are separate CCM object types but share
                        # the same EWM work-item pipeline, so we run both per release.
                        if self.cfg.migrate_tasks:
                            for rel in effective_releases:
                                self._migrate_tasks(extractor, loader, stats,
                                                    task_type="task", release=rel)
                                self._migrate_tasks(extractor, loader, stats,
                                                    task_type="defect", release=rel)

                        # Baselines are project-wide (not per-release in CCM), so we
                        # query them once and filter by the effective release list.
                        if self.cfg.migrate_baselines and syn_cfg.project:
                            if jazz_scm:
                                self._migrate_baselines_cli(
                                    jazz_scm, extractor, stats,
                                    component_ref, stream_ref,
                                    releases=effective_releases,
                                )
                            else:
                                self._migrate_baselines(extractor, loader, stats,
                                                        releases=effective_releases)

                        # Loose-artifact migration is mutually exclusive with baseline
                        # migration: when baselines are enabled the artifact content is
                        # already captured inside each baseline snapshot, so running
                        # both phases would check in the same files twice.
                        if self.cfg.migrate_artifacts and syn_cfg.project \
                                and not self.cfg.migrate_baselines:
                            if jazz_scm:
                                self._migrate_artifacts_cli(
                                    jazz_scm, extractor, stats,
                                    stream_ref, since=syn_cfg.since,
                                )
                            else:
                                self._migrate_artifacts(extractor, loader, stats,
                                                        since=syn_cfg.since)
                    finally:
                        if jazz_scm:
                            jazz_scm.logout()

        except KeyboardInterrupt:
            # Caught separately so we can log a friendlier message; the state
            # file already contains everything migrated before the interrupt.
            log.warning("Migration interrupted — partial progress saved to %s",
                        self.cfg.state_file)
            stats["errors"].append("interrupted")
        except Exception:
            # Any unhandled exception (auth failure, CCM crash, etc.) lands here.
            # Per-item progress is already in the state file; re-running after
            # fixing the root cause will skip completed items automatically.
            log.exception("Fatal error during migration — partial progress saved to %s",
                          self.cfg.state_file)
            stats["errors"].append("fatal")
        finally:
            # Always report the persistent error count so operators know
            # whether a re-run is needed even if the run completed without
            # a fatal exception.
            failed = self._state.error_count()
            if failed:
                log.warning(
                    "%d item(s) recorded as failed in %s — fix the cause and re-run "
                    "to retry them automatically.",
                    failed, self.cfg.state_file,
                )

        log.info("Migration complete: %s", stats)
        return stats

    # ------------------------------------------------------------------
    # Phase runners
    # ------------------------------------------------------------------

    def _migrate_tasks(
        self,
        extractor: SynergyExtractor,
        loader: EWMLoader,
        stats: dict,
        task_type: str = "task",
        release: object = None,
    ) -> None:
        label = f"{task_type}s" + (f" (release={release})" if release else "")
        log.info("=== Migrating %s ===", label)
        try:
            tasks = extractor.extract_tasks(task_type=task_type, release=release)
        except Exception:
            # If the entire CCM query fails (e.g. network loss, bad query syntax)
            # we abort this phase rather than partially processing results.
            # Already-migrated items in the state file are unaffected.
            log.exception("Failed to extract %s", label)
            stats["errors"].append(f"extract_{task_type}s")
            return

        for task in tasks:
            if self._state.is_done(task.task_number):
                log.debug("Skipping %s (already migrated)", task.task_number)
                stats["tasks_skipped"] += 1
                continue

            try:
                ewm_wi: EWMWorkItem = self._task_mapper.map(task)
                ewm_url = loader.load_work_item(ewm_wi)
                if ewm_url or self.cfg.dry_run:
                    self._state.mark_done(task.task_number, ewm_url or "dry-run")
                    stats["tasks_ok"] += 1
                else:
                    # load_work_item() returns None when create_work_item() raises
                    # EWMAPIError internally and catches it.  This is a permanent
                    # failure (bad payload, permission denied, etc.) — mark it so
                    # the operator can see it without reading the full log.
                    reason = "EWM loader returned None — check logs for API error"
                    log.error("Failed to load task %s: %s", task.task_number, reason)
                    self._state.mark_failed(task.task_number, reason)
                    stats["tasks_failed"] += 1
                    stats["errors"].append(task.task_number)
            except Exception as exc:
                # Catch mapping errors (unmappable field values, etc.) separately
                # from EWM API errors so a bad task doesn't abort the whole phase.
                reason = repr(exc)
                log.exception("Unhandled error for task %s", task.task_number)
                self._state.mark_failed(task.task_number, reason)
                stats["tasks_failed"] += 1
                stats["errors"].append(task.task_number)

    def _migrate_baselines(
        self,
        extractor: SynergyExtractor,
        loader: EWMLoader,
        stats: dict,
        releases: Optional[list] = None,
    ) -> None:
        log.info("=== Migrating baselines ===")
        component_name = (
            self.cfg.ewm.component_name
            or self._mapping.get("scm", {}).get("default_component",
                                                "Migrated from Synergy")
        )
        try:
            baselines = extractor.extract_baselines(releases=releases)
        except Exception:
            log.exception("Failed to extract baselines")
            stats["errors"].append("extract_baselines")
            return

        for baseline in baselines:
            # Key includes the full CCM spec to prevent collisions between
            # baselines with the same name in different databases or versions.
            state_key = f"baseline:{baseline.spec}"
            if self._state.is_done(state_key):
                log.debug("Skipping baseline %s (already migrated)", baseline.name)
                continue
            try:
                ewm_baseline = self._baseline_mapper.map(baseline, component_name)
                bid = loader.load_baseline(ewm_baseline)
                if bid or self.cfg.dry_run:
                    self._state.mark_done(state_key, bid or "dry-run")
                    stats["baselines_ok"] += 1
                    # Artifact count comes from the mapped baseline, not the
                    # raw Synergy list, because the mapper filters out dirs.
                    stats["artifacts_ok"] += len(ewm_baseline.artifacts)
                else:
                    reason = "EWM loader returned None — check logs for API error"
                    log.error("Failed to load baseline %s: %s", baseline.name, reason)
                    self._state.mark_failed(state_key, reason)
                    stats["errors"].append(state_key)
            except Exception as exc:
                log.exception("Unhandled error for baseline %s", baseline.name)
                self._state.mark_failed(state_key, repr(exc))
                stats["errors"].append(state_key)

    def _migrate_artifacts(
        self,
        extractor: SynergyExtractor,
        loader: EWMLoader,
        stats: dict,
        since: Optional[str] = None,
    ) -> None:
        log.info("=== Migrating loose artifacts ===")
        try:
            # extract_artifacts() already expands every file to its full
            # predecessor chain, ordered oldest-first, so checking them in
            # sequentially here builds the correct version history in EWM.
            objects = extractor.extract_artifacts(since=since)
        except Exception:
            log.exception("Failed to extract artifacts")
            stats["errors"].append("extract_artifacts")
            return

        for obj in objects:
            # Each version of each file has a unique CCM spec, so the state key
            # is also unique and resumability works at per-version granularity.
            state_key = f"artifact:{obj.spec}"
            if self._state.is_done(state_key):
                continue
            artifact = self._artifact_mapper.map(obj)
            if artifact is None:
                # Mapper returns None for directories and objects with no content;
                # skip silently rather than recording as a failure.
                continue
            try:
                loader.load_artifact(artifact)
                self._state.mark_done(state_key, "loaded")
                stats["artifacts_ok"] += 1
            except Exception as exc:
                log.exception("Failed to load artifact %s", obj.spec)
                self._state.mark_failed(state_key, repr(exc))
                stats["errors"].append(state_key)

    # ------------------------------------------------------------------
    # CLI SCM phase runners
    # ------------------------------------------------------------------

    def _migrate_baselines_cli(
        self,
        jazz_scm: JazzSCMClient,
        extractor: SynergyExtractor,
        stats: dict,
        component_name: str,
        stream_name: str,
        releases: Optional[list] = None,
    ) -> None:
        log.info("=== Migrating baselines via Jazz SCM CLI ===")
        try:
            baselines = extractor.extract_baselines(releases=releases)
        except Exception:
            log.exception("Failed to extract baselines")
            stats["errors"].append("extract_baselines")
            return

        for baseline in baselines:
            state_key = f"baseline:{baseline.spec}"
            if self._state.is_done(state_key):
                log.debug("Skipping baseline %s (already migrated)", baseline.name)
                continue
            try:
                ewm_baseline = self._baseline_mapper.map(baseline, component_name)
                if self.cfg.dry_run:
                    log.info("[DRY RUN] Would deliver %d artifacts and create baseline '%s'",
                             len(ewm_baseline.artifacts), baseline.name)
                    self._state.mark_done(state_key, "dry-run")
                    stats["baselines_ok"] += 1
                    stats["artifacts_ok"] += len(ewm_baseline.artifacts)
                    continue

                comment = f"Baseline: {baseline.name}"
                jazz_scm.deliver_artifacts(ewm_baseline.artifacts, stream_name, comment=comment)
                jazz_scm.create_baseline_snapshot(
                    ewm_baseline.name, stream_name, component_name, ewm_baseline.description
                )
                self._state.mark_done(state_key, baseline.name)
                stats["baselines_ok"] += 1
                stats["artifacts_ok"] += len(ewm_baseline.artifacts)
            except Exception as exc:
                log.exception("Unhandled error for baseline %s", baseline.name)
                self._state.mark_failed(state_key, repr(exc))
                stats["errors"].append(state_key)

    def _migrate_artifacts_cli(
        self,
        jazz_scm: JazzSCMClient,
        extractor: SynergyExtractor,
        stats: dict,
        stream_name: str,
        since: Optional[str] = None,
    ) -> None:
        log.info("=== Migrating loose artifacts via Jazz SCM CLI ===")
        try:
            objects = extractor.extract_artifacts(since=since)
        except Exception:
            log.exception("Failed to extract artifacts")
            stats["errors"].append("extract_artifacts")
            return

        pending = [o for o in objects if not self._state.is_done(f"artifact:{o.spec}")]
        if not pending:
            log.info("All artifacts already migrated")
            return

        mapped = [(o, self._artifact_mapper.map(o)) for o in pending]
        pairs = [(o, a) for o, a in mapped if a is not None]

        if self.cfg.dry_run:
            for obj, artifact in pairs:
                log.info("[DRY RUN] Would deliver artifact %s", artifact.path)
                self._state.mark_done(f"artifact:{obj.spec}", "dry-run")
                stats["artifacts_ok"] += 1
            return

        try:
            jazz_scm.deliver_artifacts([a for _, a in pairs], stream_name)
            for obj, _ in pairs:
                self._state.mark_done(f"artifact:{obj.spec}", "loaded")
                stats["artifacts_ok"] += 1
        except Exception as exc:
            log.exception("Failed to deliver loose artifacts via Jazz SCM CLI")
            for obj, _ in pairs:
                self._state.mark_failed(f"artifact:{obj.spec}", repr(exc))
            stats["errors"].append("artifact_delivery")

    # ------------------------------------------------------------------
    # SCM setup helpers
    # ------------------------------------------------------------------

    def _ensure_scm_targets(
        self,
        ewm_client: EWMClient,
        project_area_id: str,
        ewm_cfg,
        jazz_scm: Optional[JazzSCMClient] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Return (component_ref, stream_ref), creating them if not present.

        For the REST backend, refs are UUIDs passed to EWMLoader.
        For the CLI backend, refs are names passed to JazzSCMClient.
        Returns (None, None) when SCM migration is disabled entirely.
        """
        if not (self.cfg.migrate_artifacts or self.cfg.migrate_baselines):
            return None, None

        scm_cfg = self._mapping.get("scm", {})
        component_name = ewm_cfg.component_name or scm_cfg.get(
            "default_component", "Migrated from Synergy"
        )
        stream_name = ewm_cfg.stream_name or scm_cfg.get(
            "default_stream", "Migrated from Synergy Stream"
        )

        if self.cfg.dry_run:
            log.info("[DRY RUN] Would ensure component '%s' and stream '%s'",
                     component_name, stream_name)
            return None, None

        if jazz_scm:
            jazz_scm.ensure_component(component_name)
            jazz_scm.ensure_stream(stream_name, component_name)
            return component_name, stream_name

        log.info("Ensuring SCM component '%s'", component_name)
        component_id = ewm_client.create_component(component_name, project_area_id)
        log.info("Ensuring SCM stream '%s'", stream_name)
        stream_id = ewm_client.create_stream(stream_name, project_area_id, component_id)
        return component_id, stream_id


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------

def _configure_logging(log_file: Optional[str]) -> None:
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=handlers,
    )


def main() -> None:
    """
    Minimal CLI: reads config from a YAML file passed as the first argument.

    Usage:
        python -m synergy_to_ewm.migrate config.yaml
        python -m synergy_to_ewm.migrate config.yaml --dry-run
    """
    import yaml

    if len(sys.argv) < 2:
        print("Usage: python -m synergy_to_ewm.migrate <config.yaml> [--dry-run]")
        sys.exit(1)

    config_path = sys.argv[1]
    dry_run = "--dry-run" in sys.argv

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    from .config import MigrationConfig, SynergyConfig, EWMConfig

    syn = SynergyConfig(**raw["synergy"])
    ewm = EWMConfig(**raw["ewm"])
    migration_raw = raw.get("migration", {})
    if dry_run:
        migration_raw["dry_run"] = True

    cfg = MigrationConfig(synergy=syn, ewm=ewm, **migration_raw)
    _configure_logging(cfg.log_file)

    migrator = Migrator(cfg)
    stats = migrator.run()

    print("\n=== Migration Summary ===")
    for k, v in stats.items():
        if k != "errors":
            print(f"  {k}: {v}")
    if stats["errors"]:
        print(f"  failed IDs: {stats['errors']}")
    sys.exit(1 if stats["errors"] else 0)


if __name__ == "__main__":
    main()
