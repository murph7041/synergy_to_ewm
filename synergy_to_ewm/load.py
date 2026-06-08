"""
EWM load-only CLI.

Reads a Synergy JSON file produced by ``synergy-extract`` and loads its
contents into EWM without connecting to Synergy at all.

Usage::

    python -m synergy_to_ewm.load <config.yaml> <synergy_extract.json>
    synergy-load <config.yaml> <synergy_extract.json>

Only the ``ewm`` and ``migration`` sections of the config are required —
the ``synergy`` section is ignored if present.
"""
from __future__ import annotations

import base64
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from .ewm.client import EWMClient
from .ewm.loader import EWMLoader
from .ewm.models import EWMWorkItem
from .migrate import MigrationState
from .synergy.models import (
    SynergyAttachment,
    SynergyBaseline,
    SynergyComment,
    SynergyHistoryEntry,
    SynergyObject,
    SynergyTask,
)
from .transform.mapper import ArtifactMapper, BaselineMapper, TaskMapper, load_mapping

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Deserialisation
# ---------------------------------------------------------------------------

def _b64_to_bytes(value: object) -> Optional[bytes]:
    if value is None:
        return None
    if isinstance(value, str):
        return base64.b64decode(value)
    return bytes(value)


def _deserialize_comment(d: dict) -> SynergyComment:
    return SynergyComment(
        author=d.get("author", ""),
        timestamp=d.get("timestamp", ""),
        text=d.get("text", ""),
    )


def _deserialize_history(d: dict) -> SynergyHistoryEntry:
    return SynergyHistoryEntry(
        author=d.get("author", ""),
        timestamp=d.get("timestamp", ""),
        changes=d.get("changes", []),
    )


def _deserialize_attachment(d: dict) -> SynergyAttachment:
    return SynergyAttachment(
        name=d.get("name", ""),
        content=_b64_to_bytes(d.get("content")) or b"",
        mime_type=d.get("mime_type", "application/octet-stream"),
    )


def _deserialize_object(d: dict) -> SynergyObject:
    return SynergyObject(
        spec=d.get("spec", ""),
        name=d.get("name", ""),
        version=d.get("version", ""),
        obj_type=d.get("obj_type", ""),
        status=d.get("status", ""),
        owner=d.get("owner", ""),
        create_time=d.get("create_time", ""),
        modify_time=d.get("modify_time", ""),
        project=d.get("project", ""),
        path=d.get("path", ""),
        content=_b64_to_bytes(d.get("content")),
    )


def _deserialize_task(d: dict) -> SynergyTask:
    return SynergyTask(
        spec=d.get("spec", ""),
        task_number=d.get("task_number", ""),
        synopsis=d.get("synopsis", ""),
        description=d.get("description", ""),
        task_type=d.get("task_type", "task"),
        status=d.get("status", ""),
        priority=d.get("priority", ""),
        severity=d.get("severity", ""),
        submitter=d.get("submitter", ""),
        resolver=d.get("resolver", ""),
        release=d.get("release", ""),
        project=d.get("project", ""),
        create_time=d.get("create_time", ""),
        modify_time=d.get("modify_time", ""),
        custom_attrs=d.get("custom_attrs", {}),
        comments=[_deserialize_comment(c) for c in d.get("comments", [])],
        attachments=[_deserialize_attachment(a) for a in d.get("attachments", [])],
        history=[_deserialize_history(h) for h in d.get("history", [])],
    )


def _deserialize_baseline(d: dict) -> SynergyBaseline:
    return SynergyBaseline(
        spec=d.get("spec", ""),
        name=d.get("name", ""),
        project=d.get("project", ""),
        release=d.get("release", ""),
        status=d.get("status", ""),
        create_time=d.get("create_time", ""),
        description=d.get("description", ""),
        objects=[_deserialize_object(o) for o in d.get("objects", [])],
    )


def load_json(json_path: str) -> tuple[list, list, list, list]:
    """
    Read a synergy_extract.json file and return
    (tasks, defects, artifacts, baselines) as typed model objects.
    """
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    tasks = [_deserialize_task(d) for d in data.get("tasks", [])]
    defects = [_deserialize_task(d) for d in data.get("defects", [])]
    artifacts = [_deserialize_object(d) for d in data.get("artifacts", [])]
    baselines = [_deserialize_baseline(d) for d in data.get("baselines", [])]

    log.info(
        "Loaded %d tasks, %d defects, %d artifacts, %d baselines from %s",
        len(tasks), len(defects), len(artifacts), len(baselines), json_path,
    )
    return tasks, defects, artifacts, baselines


# ---------------------------------------------------------------------------
# File-based loader (mirrors Migrator but reads data from JSON)
# ---------------------------------------------------------------------------

class FileLoader:
    """
    Loads pre-extracted Synergy data into EWM.

    Accepts the same migration settings as Migrator and reuses the same
    mappers, state file, and EWM client — the only difference is that data
    comes from a JSON file instead of a live Synergy connection.
    """

    def __init__(self, ewm_cfg, migration_raw: dict) -> None:
        self.ewm_cfg = ewm_cfg
        self.dry_run = migration_raw.get("dry_run", False)
        self.migrate_tasks = migration_raw.get("migrate_tasks", True)
        self.migrate_artifacts = migration_raw.get("migrate_artifacts", True)
        self.migrate_baselines = migration_raw.get("migrate_baselines", True)
        self._state = MigrationState(migration_raw.get("state_file", "migration_state.json"))
        self._mapping = load_mapping(
            override_file=migration_raw.get("mapping_file"),
            extra_overrides=migration_raw.get("field_overrides") or {},
        )
        self._task_mapper = TaskMapper(self._mapping)
        self._artifact_mapper = ArtifactMapper()
        self._baseline_mapper = BaselineMapper(self._artifact_mapper)

    def run(
        self,
        tasks: list,
        defects: list,
        artifacts: list,
        baselines: list,
    ) -> dict:
        stats: dict = dict(
            tasks_ok=0, tasks_skipped=0, tasks_failed=0,
            baselines_ok=0, artifacts_ok=0, errors=[],
        )

        try:
            with EWMClient(
                server=self.ewm_cfg.server,
                user=self.ewm_cfg.user,
                password=self.ewm_cfg.password,
                verify_ssl=self.ewm_cfg.verify_ssl,
                ca_bundle=self.ewm_cfg.ca_bundle,
            ) as ewm_client:

                project_area_id = ewm_client.find_project_area(self.ewm_cfg.project_area)
                component_id, stream_id = self._ensure_scm_targets(ewm_client, project_area_id)

                loader = EWMLoader(
                    client=ewm_client,
                    project_area_id=project_area_id,
                    component_id=component_id,
                    stream_id=stream_id,
                    dry_run=self.dry_run,
                )

                if self.migrate_tasks:
                    self._load_tasks(tasks, loader, stats)
                    self._load_tasks(defects, loader, stats)

                if self.migrate_baselines and baselines:
                    self._load_baselines(baselines, loader, stats)

                if self.migrate_artifacts and artifacts and not self.migrate_baselines:
                    self._load_artifacts(artifacts, loader, stats)

        except KeyboardInterrupt:
            log.warning("Load interrupted — partial progress saved to state file")
            stats["errors"].append("interrupted")
        except Exception:
            log.exception("Fatal error during load — partial progress saved to state file")
            stats["errors"].append("fatal")
        finally:
            failed = self._state.error_count()
            if failed:
                log.warning(
                    "%d item(s) failed — fix the cause and re-run to retry automatically.",
                    failed,
                )

        log.info("Load complete: %s", stats)
        return stats

    # ------------------------------------------------------------------
    # Phase runners
    # ------------------------------------------------------------------

    def _load_tasks(self, tasks: list, loader: EWMLoader, stats: dict) -> None:
        log.info("=== Loading %d tasks/defects ===", len(tasks))
        for task in tasks:
            if self._state.is_done(task.task_number):
                log.debug("Skipping %s (already migrated)", task.task_number)
                stats["tasks_skipped"] += 1
                continue
            try:
                ewm_wi: EWMWorkItem = self._task_mapper.map(task)
                ewm_url = loader.load_work_item(ewm_wi)
                if ewm_url or self.dry_run:
                    self._state.mark_done(task.task_number, ewm_url or "dry-run")
                    stats["tasks_ok"] += 1
                else:
                    reason = "EWM loader returned None — check logs for API error"
                    log.error("Failed to load task %s: %s", task.task_number, reason)
                    self._state.mark_failed(task.task_number, reason)
                    stats["tasks_failed"] += 1
                    stats["errors"].append(task.task_number)
            except Exception as exc:
                reason = repr(exc)
                log.exception("Unhandled error for task %s", task.task_number)
                self._state.mark_failed(task.task_number, reason)
                stats["tasks_failed"] += 1
                stats["errors"].append(task.task_number)

    def _load_baselines(self, baselines: list, loader: EWMLoader, stats: dict) -> None:
        log.info("=== Loading %d baselines ===", len(baselines))
        component_name = (
            self.ewm_cfg.component_name
            or self._mapping.get("scm", {}).get("default_component", "Migrated from Synergy")
        )
        for baseline in baselines:
            state_key = f"baseline:{baseline.spec}"
            if self._state.is_done(state_key):
                log.debug("Skipping baseline %s (already migrated)", baseline.name)
                continue
            try:
                ewm_baseline = self._baseline_mapper.map(baseline, component_name)
                bid = loader.load_baseline(ewm_baseline)
                if bid or self.dry_run:
                    self._state.mark_done(state_key, bid or "dry-run")
                    stats["baselines_ok"] += 1
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

    def _load_artifacts(self, artifacts: list, loader: EWMLoader, stats: dict) -> None:
        log.info("=== Loading %d loose artifacts ===", len(artifacts))
        for obj in artifacts:
            state_key = f"artifact:{obj.spec}"
            if self._state.is_done(state_key):
                continue
            artifact = self._artifact_mapper.map(obj)
            if artifact is None:
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
    # SCM setup
    # ------------------------------------------------------------------

    def _ensure_scm_targets(
        self,
        ewm_client: EWMClient,
        project_area_id: str,
    ) -> tuple[Optional[str], Optional[str]]:
        if not (self.migrate_artifacts or self.migrate_baselines):
            return None, None

        scm_cfg = self._mapping.get("scm", {})
        component_name = self.ewm_cfg.component_name or scm_cfg.get(
            "default_component", "Migrated from Synergy"
        )
        stream_name = self.ewm_cfg.stream_name or scm_cfg.get(
            "default_stream", "Migrated from Synergy Stream"
        )

        if self.dry_run:
            log.info("[DRY RUN] Would ensure component '%s' and stream '%s'",
                     component_name, stream_name)
            return None, None

        log.info("Ensuring SCM component '%s'", component_name)
        component_id = ewm_client.create_component(component_name, project_area_id)
        log.info("Ensuring SCM stream '%s'", stream_name)
        stream_id = ewm_client.create_stream(stream_name, project_area_id, component_id)
        return component_id, stream_id


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import yaml
    from .config import EWMConfig

    if len(sys.argv) < 3:
        print("Usage: python -m synergy_to_ewm.load <config.yaml> <synergy_extract.json>")
        sys.exit(1)

    config_path = sys.argv[1]
    json_path = sys.argv[2]

    if not Path(json_path).exists():
        print(f"Error: extract file not found: {json_path}")
        sys.exit(1)

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    log_file = raw.get("migration", {}).get("log_file", "migration.log")
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=handlers,
    )

    ewm_cfg = EWMConfig(**raw["ewm"])
    migration_raw = raw.get("migration", {})
    if "--dry-run" in sys.argv:
        migration_raw["dry_run"] = True

    tasks, defects, artifacts, baselines = load_json(json_path)

    file_loader = FileLoader(ewm_cfg, migration_raw)
    stats = file_loader.run(tasks, defects, artifacts, baselines)

    print("\n=== Load Summary ===")
    for k, v in stats.items():
        if k != "errors":
            print(f"  {k}: {v}")
    if stats["errors"]:
        print(f"  failed IDs: {stats['errors']}")
    sys.exit(1 if stats["errors"] else 0)


if __name__ == "__main__":
    main()
