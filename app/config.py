"""Environment-driven configuration (12-factor, container friendly)."""

import logging
import os

log = logging.getLogger("parserdmarc.config")


def _bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self, env=None):
        env = env if env is not None else os.environ

        # --- Microsoft Graph / Azure AD app registration -------------------
        self.tenant_id = env.get("GRAPH_TENANT_ID", "")
        self.client_id = env.get("GRAPH_CLIENT_ID", "")
        self.client_secret = env.get("GRAPH_CLIENT_SECRET", "")
        # Shared mailbox UPN / primary SMTP address (e.g. dmarc@example.com)
        self.mailbox = env.get("GRAPH_MAILBOX", "")
        self.authority = env.get(
            "GRAPH_AUTHORITY", "https://login.microsoftonline.com"
        ).rstrip("/")
        self.graph_base_url = env.get(
            "GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0"
        ).rstrip("/")
        self.graph_scope = env.get("GRAPH_SCOPE", "https://graph.microsoft.com/.default")

        # --- Mailbox processing -------------------------------------------
        # Well-known name ("inbox", "archive", ...) or a Graph folder ID.
        self.reports_folder = env.get("REPORTS_FOLDER", "inbox")
        self.archive_folder = env.get("ARCHIVE_FOLDER", "archive")
        # What to do with a message once processed: move | mark_read | delete | none
        self.post_process_action = env.get("POST_PROCESS_ACTION", "move").lower()
        self.batch_size = int(env.get("BATCH_SIZE", "50"))

        # --- Scheduling ----------------------------------------------------
        self.poll_interval = int(env.get("POLL_INTERVAL_SECONDS", "300"))
        self.run_once = _bool(env.get("RUN_ONCE"), False)

        # --- Storage -------------------------------------------------------
        self.storage_dir = env.get("STORAGE_DIR", "/data")
        self.save_attachments = _bool(env.get("SAVE_ATTACHMENTS"), True)
        self.save_parsed_json = _bool(env.get("SAVE_PARSED_JSON"), True)

        # --- Parsing -------------------------------------------------------
        # Skip reverse-DNS / GeoIP lookups (faster, no outbound DNS needed).
        self.offline_dns = _bool(env.get("OFFLINE_DNS"), False)

        # --- Metrics -------------------------------------------------------
        self.metrics_port = int(env.get("METRICS_PORT", "9797"))
        # Per-source-IP metrics are high cardinality; opt-in only.
        self.export_source_ip = _bool(env.get("EXPORT_SOURCE_IP_METRICS"), False)

        self.log_level = env.get("LOG_LEVEL", "INFO").upper()

    @property
    def authority_url(self):
        return f"{self.authority}/{self.tenant_id}"

    def validate(self):
        required = {
            "GRAPH_TENANT_ID": self.tenant_id,
            "GRAPH_CLIENT_ID": self.client_id,
            "GRAPH_CLIENT_SECRET": self.client_secret,
            "GRAPH_MAILBOX": self.mailbox,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise SystemExit(
                "Missing required environment variables: " + ", ".join(missing)
            )
        if self.post_process_action not in ("move", "mark_read", "delete", "none"):
            raise SystemExit(
                "POST_PROCESS_ACTION must be one of: move, mark_read, delete, none"
            )
