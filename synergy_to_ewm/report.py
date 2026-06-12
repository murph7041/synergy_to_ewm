"""
Post-migration traceability reports.

Currently produces a single CSV report mapping each Synergy task to its EWM
work item and listing the Change Requests associated with that task in Synergy.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from .migrate import MigrationState
    from .synergy.models import SynergyTask

log = logging.getLogger(__name__)


def _ewm_id_from_url(url: str) -> str:
    """
    Extract the numeric work item ID from an EWM URL.

    EWM work item URLs end with the item ID, e.g.:
      https://host:9443/ccm/resource/itemName/com.ibm.team.workitem.WorkItem/1234
    Returns the last non-empty path segment, or the full URL when the pattern
    does not match (dry-run placeholder, unexpected format, etc.).
    """
    if not url or url in ("dry-run", "fatal", "interrupted", "session_startup_failed"):
        return url
    segment = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return segment if segment else url


def write_cr_report(
    tasks: list["SynergyTask"],
    state: "MigrationState",
    output_path: str,
) -> None:
    """
    Write a CSV traceability report cross-referencing Synergy tasks, their EWM
    work item IDs, and the Change Requests associated with each task.

    Columns
    -------
    synergy_task_id   The Synergy task number (state-file key).
    ewm_work_item_id  Numeric EWM work item ID extracted from the EWM URL.
    ewm_url           Full EWM work item URL (empty when not yet migrated).
    change_requests   Semicolon-separated list of Synergy CR object specs.

    Tasks that have not yet been loaded into EWM (not in the state file's
    ``done`` map) are included with empty EWM columns so the report covers
    the full Synergy task population.
    """
    out = Path(output_path)
    rows_written = 0

    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["synergy_task_id", "ewm_work_item_id", "ewm_url", "change_requests"])

        for task in tasks:
            ewm_url = state._done.get(task.task_number, "")
            ewm_id = _ewm_id_from_url(ewm_url) if ewm_url else ""
            cr_list = "; ".join(task.change_requests) if task.change_requests else ""
            writer.writerow([task.task_number, ewm_id, ewm_url, cr_list])
            rows_written += 1

    log.info("CR traceability report written to %s (%d rows)", out, rows_written)
