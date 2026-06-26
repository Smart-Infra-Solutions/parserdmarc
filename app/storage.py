"""Persistence of raw attachments, parsed reports and dedupe state on a volume.

Layout under STORAGE_DIR (default /data), partitioned per reported domain:

    <domain>/attachments/<YYYY>/<MM>/<DD>/<safe-original-name>
    <domain>/parsed/<YYYY>/<MM>/<DD>/<org>_<report_id>.json
    state/processed_reports.json      (set of "<org>|<report_id>" keys)

Attachments that cannot be parsed (so the domain is unknown) are kept under the
special "unparsed" domain so no raw report is ever lost.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone

log = logging.getLogger("parserdmarc.storage")

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(name):
    cleaned = _UNSAFE.sub("_", name or "").strip("._")
    return cleaned or "unknown"


def dedupe_key(org, report_id):
    return f"{safe_name(org)}|{safe_name(report_id)}"


class Storage:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = cfg.storage_dir
        self.state_path = os.path.join(self.root, "state", "processed_reports.json")
        self._processed = self._load_state()

    # -- dedupe state ------------------------------------------------------
    def _load_state(self):
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                return set(json.load(fh))
        except FileNotFoundError:
            return set()
        except Exception as exc:  # corrupt state should never be fatal
            log.warning("Could not read dedupe state (%s); starting empty", exc)
            return set()

    def _save_state(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(sorted(self._processed), fh)
        os.replace(tmp, self.state_path)

    def already_processed(self, key):
        return key in self._processed

    def mark_processed(self, key):
        self._processed.add(key)
        self._save_state()

    # -- files -------------------------------------------------------------
    def _dated_dir(self, domain, sub, when):
        when = when or datetime.now(timezone.utc)
        path = os.path.join(
            self.root,
            safe_name(domain),
            sub,
            when.strftime("%Y"),
            when.strftime("%m"),
            when.strftime("%d"),
        )
        os.makedirs(path, exist_ok=True)
        return path

    def save_attachment(self, domain, filename, data, received=None):
        if not self.cfg.save_attachments:
            return None
        directory = self._dated_dir(domain, "attachments", received)
        path = os.path.join(directory, safe_name(filename))
        base, ext = os.path.splitext(path)
        counter = 1
        while os.path.exists(path):  # never clobber an existing report
            path = f"{base}_{counter}{ext}"
            counter += 1
        with open(path, "wb") as fh:
            fh.write(data)
        log.debug("Saved attachment %s (%d bytes)", path, len(data))
        return path

    def iter_unparsed_attachments(self):
        """Yield (path, filename, received) for every file kept under 'unparsed'.

        The received date is recovered from the YYYY/MM/DD path layout so a
        reprocessed report lands in the same date bucket it originally arrived.
        """
        base = os.path.join(self.root, "unparsed", "attachments")
        if not os.path.isdir(base):
            return
        for dirpath, _dirs, files in os.walk(base):
            for name in sorted(files):
                path = os.path.join(dirpath, name)
                yield path, name, self._date_from_path(base, path)

    @staticmethod
    def _date_from_path(base, path):
        parts = os.path.relpath(os.path.dirname(path), base).split(os.sep)
        try:
            return datetime(int(parts[0]), int(parts[1]), int(parts[2]),
                            tzinfo=timezone.utc)
        except (ValueError, IndexError):
            return None

    def remove_file(self, path):
        try:
            os.remove(path)
        except OSError as exc:
            log.warning("Could not remove %s: %s", path, exc)

    def save_parsed(self, domain, report_type, org, report_id, report, received=None):
        if not self.cfg.save_parsed_json:
            return None
        directory = self._dated_dir(domain, "parsed", received)
        name = f"{safe_name(org)}_{safe_name(report_id)}.json"
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {"report_type": report_type, "report": report},
                fh,
                indent=2,
                default=str,
            )
        return path
