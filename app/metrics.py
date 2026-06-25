"""Prometheus metric definitions and helpers to populate them from reports."""

from prometheus_client import Counter, Gauge

# Aggregate message volume broken down by DMARC evaluation outcome.
# `_total` is appended automatically -> dmarc_aggregate_messages_total
AGGREGATE_MESSAGES = Counter(
    "dmarc_aggregate_messages",
    "Number of messages described in DMARC aggregate reports",
    ["reporter_org", "policy_domain", "disposition", "dkim_aligned", "spf_aligned", "dmarc_aligned"],
)

# Opt-in, high-cardinality breakdown by sending source.
AGGREGATE_SOURCE_MESSAGES = Counter(
    "dmarc_aggregate_source_messages",
    "Number of messages described in DMARC aggregate reports, by source IP",
    ["policy_domain", "source_ip", "source_host", "disposition", "dmarc_aligned"],
)

REPORTS_PROCESSED = Counter(
    "dmarc_reports_processed",
    "Number of DMARC reports parsed",
    ["report_type"],
)

EMAILS_PROCESSED = Counter(
    "dmarc_emails_processed",
    "Number of mailbox messages fetched and processed",
)

PROCESSING_ERRORS = Counter(
    "dmarc_processing_errors",
    "Number of errors encountered while processing",
    ["stage"],
)

LAST_POLL = Gauge(
    "dmarc_last_poll_timestamp_seconds",
    "Unix timestamp of the last mailbox poll attempt",
)
LAST_SUCCESSFUL_POLL = Gauge(
    "dmarc_last_successful_poll_timestamp_seconds",
    "Unix timestamp of the last fully successful mailbox poll",
)
LAST_REPORT_END = Gauge(
    "dmarc_last_report_end_timestamp_seconds",
    "Unix timestamp of the end of the most recent report date range seen",
)
POLL_DURATION = Gauge(
    "dmarc_last_poll_duration_seconds",
    "Duration of the last mailbox poll",
)
EXPORTER_UP = Gauge(
    "dmarc_exporter_up",
    "1 while the exporter loop is running",
)

_last_report_end = 0.0


def note_report_end(epoch):
    """Track the most recent report end timestamp (monotonic gauge)."""
    global _last_report_end
    if epoch and epoch > _last_report_end:
        _last_report_end = epoch
        LAST_REPORT_END.set(epoch)


def _pass_fail(value):
    return "pass" if value else "fail"


def record_aggregate(report, export_source_ip=False):
    """Increment counters from a parsed aggregate report dict.

    Returns the report's end_date string (for timestamp tracking).
    """
    meta = report.get("report_metadata", {}) or {}
    org = meta.get("org_name") or "unknown"
    policy = report.get("policy_published", {}) or {}
    policy_domain = policy.get("domain") or "unknown"

    for record in report.get("records", []) or []:
        count = int(record.get("count") or 0)
        if count <= 0:
            continue
        alignment = record.get("alignment", {}) or {}
        evaluated = record.get("policy_evaluated", {}) or {}
        identifiers = record.get("identifiers", {}) or {}

        domain = identifiers.get("header_from") or policy_domain
        disposition = evaluated.get("disposition") or "none"
        dkim_aligned = _pass_fail(alignment.get("dkim"))
        spf_aligned = _pass_fail(alignment.get("spf"))
        dmarc_aligned = _pass_fail(alignment.get("dmarc"))

        AGGREGATE_MESSAGES.labels(
            org, domain, disposition, dkim_aligned, spf_aligned, dmarc_aligned
        ).inc(count)

        if export_source_ip:
            source = record.get("source", {}) or {}
            AGGREGATE_SOURCE_MESSAGES.labels(
                domain,
                source.get("ip_address") or "unknown",
                source.get("reverse_dns") or source.get("name") or "unknown",
                disposition,
                dmarc_aligned,
            ).inc(count)

    return meta.get("end_date")
