"""
High-level loading logic: takes EWM model objects and persists them
via the EWMClient, handling comments and attachments automatically.
"""
from __future__ import annotations

import logging
from typing import Optional

from .client import EWMClient, EWMAPIError
from .models import EWMArtifact, EWMBaseline, EWMWorkItem

log = logging.getLogger(__name__)


class EWMLoader:
    """
    Loads transformed data into EWM.

    Parameters
    ----------
    client:
        An authenticated EWMClient.
    project_area_id:
        EWM project area UUID.
    component_id:
        EWM SCM component item ID (for artifact/baseline loading).
    stream_id:
        EWM SCM stream/workspace item ID.
    dry_run:
        When True, log all operations but make no API calls.
    """

    def __init__(
        self,
        client: EWMClient,
        project_area_id: str,
        component_id: Optional[str] = None,
        stream_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> None:
        self.client = client
        self.project_area_id = project_area_id
        self.component_id = component_id
        self.stream_id = stream_id
        self.dry_run = dry_run

    # ------------------------------------------------------------------
    # Work items
    # ------------------------------------------------------------------

    def load_work_item(self, wi: EWMWorkItem) -> Optional[str]:
        """
        Create a work item in EWM, then add its comments and attachments.

        Returns
        -------
        The work item URL, or None in dry-run mode.
        """
        payload = self._work_item_payload(wi)

        if self.dry_run:
            log.info("[DRY RUN] Would create work item: %s", wi.title)
            return None

        try:
            wi_url = self.client.create_work_item(self.project_area_id, payload)
            log.info("Created work item: %s → %s", wi.source_id, wi_url)
        except EWMAPIError:
            log.exception("Failed to create work item %s", wi.source_id)
            return None

        wi.ewm_url = wi_url

        for comment in wi.comments:
            self._load_comment(wi_url, comment.text, comment.author,
                               comment.timestamp)

        for attachment in wi.attachments:
            self._load_attachment(wi_url, attachment.name,
                                  attachment.content, attachment.mime_type)

        return wi_url

    def _work_item_payload(self, wi: EWMWorkItem) -> dict:
        payload: dict = {
            "dc:title": wi.title,
            "dc:description": wi.description,
            "dc:type": {"dc:title": wi.work_item_type},
            "rtc_cm:priority": {"dc:title": wi.priority},
            "rtc_cm:severity": {"dc:title": wi.severity},
            "dc:creator": {"dc:title": wi.submitted_by},
            "rtc_cm:ownedBy": {"dc:title": wi.owned_by},
            "rtc_cm:filedAgainst": {"dc:title": wi.filed_against},
            "rtc_cm:state": {"dc:title": wi.status},
            "dc:created": wi.created,
            "dc:modified": wi.modified,
        }

        if wi.tags:
            payload["oslc_cmx:tags"] = ", ".join(wi.tags)

        # Merge custom attributes directly into the payload
        payload.update(wi.custom_attrs)

        return payload

    def _load_comment(
        self, wi_url: str, text: str, author: str, timestamp: str
    ) -> None:
        body = text
        if timestamp:
            body = f"[{timestamp}] {body}"
        if self.dry_run:
            log.info("[DRY RUN] Would add comment to %s", wi_url)
            return
        try:
            self.client.add_comment(wi_url, body, author)
        except EWMAPIError:
            log.warning("Failed to add comment to %s", wi_url)

    def _load_attachment(
        self, wi_url: str, name: str, content: bytes, mime_type: str
    ) -> None:
        if self.dry_run:
            log.info("[DRY RUN] Would attach %s to %s", name, wi_url)
            return
        try:
            self.client.upload_attachment(wi_url, name, content, mime_type)
        except EWMAPIError:
            log.warning("Failed to upload attachment %s to %s", name, wi_url)

    # ------------------------------------------------------------------
    # Source artifacts
    # ------------------------------------------------------------------

    def load_artifact(self, artifact: EWMArtifact) -> None:
        """Check a single file into the configured stream/component."""
        if not self.stream_id or not self.component_id:
            raise ValueError("stream_id and component_id are required for artifact loading")

        if self.dry_run:
            log.info("[DRY RUN] Would check in %s", artifact.path)
            return

        try:
            self.client.checkin_file(
                self.stream_id,
                self.component_id,
                artifact.path,
                artifact.content,
                artifact.mime_type,
                comment=artifact.comment,
                author=artifact.author,
            )
            log.debug("Checked in: %s", artifact.path)
        except EWMAPIError:
            log.exception("Failed to check in %s", artifact.path)

    # ------------------------------------------------------------------
    # Baselines
    # ------------------------------------------------------------------

    def load_baseline(self, baseline: EWMBaseline) -> Optional[str]:
        """
        Load all baseline artifacts, then create the EWM baseline snapshot.
        Returns the EWM baseline item ID, or None in dry-run mode.
        """
        if not self.stream_id or not self.component_id:
            raise ValueError("stream_id and component_id are required for baseline loading")

        log.info("Loading baseline '%s' (%d artifacts)",
                 baseline.name, len(baseline.artifacts))

        for artifact in baseline.artifacts:
            self.load_artifact(artifact)

        if self.dry_run:
            log.info("[DRY RUN] Would create baseline '%s'", baseline.name)
            return None

        try:
            bid = self.client.create_baseline(
                self.stream_id,
                self.component_id,
                baseline.name,
                baseline.description,
            )
            log.info("Created baseline '%s' → %s", baseline.name, bid)
            baseline.ewm_id = bid
            return bid
        except EWMAPIError:
            log.exception("Failed to create baseline '%s'", baseline.name)
            return None
