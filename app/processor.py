"""Orchestration: poll mailbox -> save attachments -> parse -> update metrics."""

import logging
import time
from datetime import datetime, timezone

from parsedmarc import parse_report_file

from . import metrics
from .storage import dedupe_key

log = logging.getLogger("parserdmarc.processor")


def _parse_received(value):
    """Parse a Graph receivedDateTime (ISO 8601) into an aware datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _report_domain(result):
    """Best-effort reported domain used to partition stored files."""
    report = result.get("report", {}) or {}
    if result.get("report_type") == "aggregate":
        return (report.get("policy_published", {}) or {}).get("domain") or "unknown"
    # Failure / SMTP TLS reports: try a few sensible keys, else "unknown".
    for key in ("reported_domain", "policy_published_domain", "domain"):
        if report.get(key):
            return report[key]
    identifiers = report.get("identifiers", {}) or {}
    return identifiers.get("header_from") or "unknown"


def _to_epoch(value):
    """Parse a parsedmarc date string into a Unix timestamp."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Processor:
    def __init__(self, cfg, graph, storage):
        self.cfg = cfg
        self.graph = graph
        self.storage = storage

    def poll_once(self):
        start = time.time()
        metrics.LAST_POLL.set(start)
        processed = 0
        try:
            for message in self.graph.iter_messages():
                if self._process_message(message):
                    processed += 1
            metrics.LAST_SUCCESSFUL_POLL.set(time.time())
        except Exception as exc:  # network/auth failure: keep the loop alive
            log.exception("Poll failed: %s", exc)
            metrics.PROCESSING_ERRORS.labels("poll").inc()
        finally:
            metrics.POLL_DURATION.set(time.time() - start)
        log.info("Poll complete: %d message(s) processed", processed)
        return processed

    def _process_message(self, message):
        message_id = message["id"]
        received = _parse_received(message.get("receivedDateTime"))
        subject = message.get("subject", "")

        try:
            attachments = self.graph.get_file_attachments(message_id)
        except Exception as exc:
            log.error("Failed to fetch attachments for %r: %s", subject, exc)
            metrics.PROCESSING_ERRORS.labels("fetch_attachments").inc()
            return False  # leave message untouched so it is retried next poll

        if not attachments:
            log.info("Message %r has no file attachments; skipping", subject)

        for filename, data in attachments:
            # Parse first so we know which domain the report is about, then
            # store the raw attachment under that domain's folder.
            try:
                result = parse_report_file(data, offline=self.cfg.offline_dns)
            except Exception as exc:
                log.warning("Could not parse attachment %r: %s", filename, exc)
                metrics.PROCESSING_ERRORS.labels("parse").inc()
                # Keep the raw file anyway so nothing is lost.
                self.storage.save_attachment("unparsed", filename, data, received)
                continue
            domain = _report_domain(result)
            self.storage.save_attachment(domain, filename, data, received)
            self._handle_report(domain, result, received)

        self._post_process(message_id)
        metrics.EMAILS_PROCESSED.inc()
        return True

    def _handle_report(self, domain, result, received):
        report_type = result.get("report_type", "unknown")
        report = result.get("report", {}) or {}
        metrics.REPORTS_PROCESSED.labels(report_type).inc()

        if report_type != "aggregate":
            # Failure / SMTP TLS reports are stored but not turned into metrics.
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
            self.storage.save_parsed(domain, report_type, "unknown", stamp, report, received)
            return

        meta = report.get("report_metadata", {}) or {}
        org = meta.get("org_name") or "unknown"
        report_id = meta.get("report_id") or ""
        key = dedupe_key(org, report_id)

        # Always persist the parsed JSON, even for duplicates.
        self.storage.save_parsed(domain, report_type, org, report_id, report, received)

        if report_id and self.storage.already_processed(key):
            log.info("Duplicate aggregate report %s/%s; metrics skipped", org, report_id)
            return

        end_date = metrics.record_aggregate(report, self.cfg.export_source_ip)
        metrics.note_report_end(_to_epoch(end_date))

        if report_id:
            self.storage.mark_processed(key)

    def _post_process(self, message_id):
        action = self.cfg.post_process_action
        try:
            if action == "move":
                self.graph.move(message_id, self.cfg.archive_folder)
            elif action == "mark_read":
                self.graph.mark_read(message_id)
            elif action == "delete":
                self.graph.delete(message_id)
            # "none": leave the message as-is (testing only — it will be
            # refetched every poll; metrics are still deduped by report_id).
        except Exception as exc:
            log.error("Post-process (%s) failed for message: %s", action, exc)
            metrics.PROCESSING_ERRORS.labels("post_process").inc()
