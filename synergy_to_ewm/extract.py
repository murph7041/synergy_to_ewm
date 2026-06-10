"""
Standalone Synergy extraction CLI.

Connects to Synergy, extracts tasks/defects/artifacts/baselines according to
the synergy section of a config YAML, and writes the result to a JSON file.

Usage::

    python -m synergy_to_ewm.extract <config.yaml> [output.json]
    synergy-extract <config.yaml> [output.json]

Only the ``synergy`` section of the config is required — no EWM credentials needed.

Attachment and file contents are base64-encoded in the JSON output so the file
can be loaded back without any binary encoding issues.
"""
from __future__ import annotations

import base64
import dataclasses
import json
import logging
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _encode_bytes(obj: Any) -> Any:
    """Recursively replace bytes values with base64 strings."""
    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode()
    if isinstance(obj, dict):
        return {k: _encode_bytes(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_encode_bytes(i) for i in obj]
    return obj


def _to_serializable(obj: Any) -> Any:
    """Convert a dataclass (or list of dataclasses) to a JSON-safe structure."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _encode_bytes(dataclasses.asdict(obj))
    if isinstance(obj, list):
        return [_to_serializable(i) for i in obj]
    return obj


# ---------------------------------------------------------------------------
# Core extraction function
# ---------------------------------------------------------------------------

def extract(cfg: "SynergyConfig", output_path: str) -> dict:  # noqa: F821
    """
    Run the extraction and write results to *output_path* as JSON.

    Returns a summary dict with item counts.
    """
    from .synergy.client import CCMClient, CCMError
    from .synergy.extractor import SynergyExtractor

    summary: dict = dict(tasks=0, defects=0, artifacts=0, baselines=0)

    try:
        with CCMClient(
            server=cfg.server,
            database=cfg.database,
            user=cfg.user,
            password=cfg.password,
            ccm_exe=cfg.ccm_exe,
        ) as ccm:

            extractor = SynergyExtractor(
                client=ccm,
                project=cfg.project,
                release=cfg.release,
                extra_query=cfg.query_extra,
                fetch_file_content=True,
            )

            effective_releases = cfg.releases or ([cfg.release] if cfg.release else [None])
            if len(effective_releases) > 1:
                log.info("Extracting %d releases: %s", len(effective_releases), effective_releases)

            tasks = []
            defects = []
            for rel in effective_releases:
                tasks.extend(extractor.extract_tasks(task_type="task", release=rel))
                defects.extend(extractor.extract_tasks(task_type="defect", release=rel))

            artifacts = []
            baselines = []
            if cfg.project:
                baselines = extractor.extract_baselines(releases=effective_releases)
                artifacts = extractor.extract_artifacts(since=cfg.since)

    except CCMError as exc:
        log.error("Synergy session failed: %s", exc)
        raise

    summary["tasks"] = len(tasks)
    summary["defects"] = len(defects)
    summary["artifacts"] = len(artifacts)
    summary["baselines"] = len(baselines)

    payload = {
        "synergy_server": cfg.server,
        "synergy_database": cfg.database,
        "tasks": _to_serializable(tasks),
        "defects": _to_serializable(defects),
        "artifacts": _to_serializable(artifacts),
        "baselines": _to_serializable(baselines),
    }

    out = Path(output_path)
    with out.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    log.info("Wrote extraction to %s", out)
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import yaml
    from .config import SynergyConfig

    if len(sys.argv) < 2:
        print("Usage: python -m synergy_to_ewm.extract <config.yaml> [output.json]")
        sys.exit(1)

    config_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else "synergy_extract.json"

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler()],
    )

    from .synergy.client import CCMError

    cfg = SynergyConfig(**raw["synergy"])

    try:
        summary = extract(cfg, output_path)
    except CCMError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        log.exception("Unexpected error during extraction")
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)

    print("\n=== Extraction Summary ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print(f"  output: {output_path}")


if __name__ == "__main__":
    main()
