"""Minimal Microsoft Graph client (app-only / client-credentials flow).

Only the handful of mail endpoints needed to read a shared mailbox, download
file attachments and tidy up processed messages are implemented. Token
acquisition and caching are delegated to MSAL.
"""

import base64
import logging
from urllib.parse import quote

import msal
import requests

log = logging.getLogger("parserdmarc.graph")

FILE_ATTACHMENT = "#microsoft.graph.fileAttachment"


class GraphClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self._app = msal.ConfidentialClientApplication(
            client_id=cfg.client_id,
            client_credential=cfg.client_secret,
            authority=cfg.authority_url,
        )
        self._session = requests.Session()

    # -- auth --------------------------------------------------------------
    def _token(self):
        # MSAL returns a cached token when one is still valid.
        result = self._app.acquire_token_for_client(scopes=[self.cfg.graph_scope])
        if "access_token" not in result:
            raise RuntimeError(
                "Failed to acquire Graph token: "
                f"{result.get('error')}: {result.get('error_description')}"
            )
        return result["access_token"]

    def _headers(self, json_body=False):
        headers = {"Authorization": f"Bearer {self._token()}"}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _user_base(self):
        return f"{self.cfg.graph_base_url}/users/{quote(self.cfg.mailbox, safe='@')}"

    # -- reads -------------------------------------------------------------
    def iter_messages(self):
        """Yield unread messages from the configured reports folder."""
        folder = quote(self.cfg.reports_folder, safe="")
        url = f"{self._user_base()}/mailFolders/{folder}/messages"
        params = {
            "$filter": "isRead eq false",
            "$top": str(self.cfg.batch_size),
            "$select": "id,subject,receivedDateTime,hasAttachments,isRead",
        }
        first = True
        while url:
            resp = self._session.get(
                url,
                headers=self._headers(),
                params=params if first else None,
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            for message in data.get("value", []):
                yield message
            url = data.get("@odata.nextLink")
            first = False

    def get_file_attachments(self, message_id):
        """Return a list of (filename, raw_bytes) for file attachments."""
        url = f"{self._user_base()}/messages/{message_id}/attachments"
        resp = self._session.get(url, headers=self._headers(), timeout=120)
        resp.raise_for_status()
        out = []
        for att in resp.json().get("value", []):
            if att.get("@odata.type") != FILE_ATTACHMENT:
                continue
            content = att.get("contentBytes")
            if not content:
                continue
            out.append((att.get("name") or "attachment", base64.b64decode(content)))
        return out

    # -- writes (post-processing) ------------------------------------------
    def mark_read(self, message_id):
        url = f"{self._user_base()}/messages/{message_id}"
        resp = self._session.patch(
            url, headers=self._headers(json_body=True), json={"isRead": True}, timeout=60
        )
        resp.raise_for_status()

    def move(self, message_id, destination_folder):
        url = f"{self._user_base()}/messages/{message_id}/move"
        resp = self._session.post(
            url,
            headers=self._headers(json_body=True),
            json={"destinationId": destination_folder},
            timeout=60,
        )
        resp.raise_for_status()

    def delete(self, message_id):
        url = f"{self._user_base()}/messages/{message_id}"
        resp = self._session.delete(url, headers=self._headers(), timeout=60)
        resp.raise_for_status()
