# parserdmarc

[![status-badge](https://ci.si.solutions/api/badges/16/status.svg)](https://ci.si.solutions/repos/16)

Container that fetches **DMARC reports** from a shared **Office 365 mailbox**,
downloads and archives the attachments, parses them with
[`parsedmarc`](https://github.com/domainaware/parsedmarc), and exposes
**Prometheus metrics** ready for a Grafana dashboard.

```
O365 (shared mailbox)
   │  Microsoft Graph (app-only / client credentials)
   ▼
parserdmarc ──► /data/<domain>/attachments/…  (raw attachments archived)
   │         ──► /data/<domain>/parsed/…       (parsed reports as JSON)
   │            (storage partitioned by DMARC domain)
   │
   └──► :9797/metrics  ◄── Prometheus  ◄── Grafana
        (metrics labeled by policy_domain)
```

## How it works

1. Every `POLL_INTERVAL_SECONDS`, the service lists the **unread** messages in
   the configured folder of the shared mailbox via Microsoft Graph.
2. For each message, it downloads the attachments, **saves** them to the
   `/data` volume, explicitly **decompresses** `.gz` / `.zip` files (using the
   stdlib, tolerant of multi-member gzip streams), then parses the XML with
   `parsedmarc`.
3. **Aggregate** reports feed the Prometheus counters; the parsed JSON is also
   saved.
4. The message is then **moved** to the archive folder (configurable:
   `move` / `mark_read` / `delete` / `none`) so it isn't reprocessed.

Any unreadable attachment is kept under `unparsed/` (nothing is ever lost). On
startup, if `REPROCESS_UNPARSED=true` (default), the service **replays** these
files: those that now parse successfully are reclassified under their domain,
counted, then removed from `unparsed/`; the operation is idempotent.

Deduplication relies on both the post-processing action **and** the
`report_id` (tracked in `/data/state/processed_reports.json`), which prevents
double counting even when reprocessing.

## Microsoft Entra ID prerequisites (app registration)

Authentication uses the **client credentials** flow (app-only), which is
long-lived and requires no user password.

### 1. Create the app registration

**Entra admin center** ([entra.microsoft.com](https://entra.microsoft.com)) →
*Identity → Applications → App registrations → New registration*.

| Field | Value |
|---|---|
| **Name** | `parserdmarc` |
| **Supported account types** | *Accounts in this organizational directory only (Single tenant)* |
| **Redirect URI** | *(leave empty)* |

On the *Overview* page, note the **Application (client) ID** and the
**Directory (tenant) ID**.

### 2. Create the secret

*Certificates & secrets → Client secrets → New client secret* → expiration 12
or 24 months. **Copy the value (`Value`) immediately** — it won't be shown
again.

### 3. Graph permissions (application)

*API permissions → Add a permission → Microsoft Graph → **Application
permissions*** :

| Permission | Why |
|---|---|
| **`Mail.ReadWrite`** | Read mail **and** move/mark-read/delete after processing |

Then **Grant admin consent for \<tenant\>** (the status must turn green ✅).

> ⚠️ Choose **Application** (not *Delegated*). If `POST_PROCESS_ACTION=none`,
> `Mail.Read` is enough. An *application* permission grants access to **all**
> mailboxes in the tenant → hence the restriction in step 4.

### 4. Restrict to a single mailbox (Exchange Online PowerShell)

Example for mailbox **`abuse@example.com`**. Replace `<CLIENT_ID>` with the
Application (client) ID.

```powershell
# Connect
Connect-ExchangeOnline -UserPrincipalName admin@example.com

# 1. Mail-enabled security group containing the target mailbox
New-DistributionGroup -Name "DMARC-App-Scope" -Type Security `
  -PrimarySmtpAddress dmarc-app-scope@example.com `
  -Members abuse@example.com

# 2. The app can only access members of the group
New-ApplicationAccessPolicy -AppId "<CLIENT_ID>" `
  -PolicyScopeGroupId dmarc-app-scope@example.com `
  -AccessRight RestrictAccess `
  -Description "parserdmarc - restricted access to abuse@example.com"

# 3. Verify (Granted within scope, Denied outside)
Test-ApplicationAccessPolicy -Identity abuse@example.com  -AppId "<CLIENT_ID>"
Test-ApplicationAccessPolicy -Identity admin@example.com  -AppId "<CLIENT_ID>"
```

> Propagation can take up to ~30 minutes. To add another mailbox to the
> scope: `Add-DistributionGroupMember -Identity dmarc-app-scope@example.com -Member other@example.com`.

### Required rights for setup

| Action | Required role |
|---|---|
| Create the app registration | *Application Developer* (or any user if unrestricted) |
| **Grant admin consent** (step 3) | *Global Administrator* or *Privileged Role Administrator* |
| Application Access Policy (step 4) | *Exchange Administrator* |

> DMARC reports arrive in the shared mailbox. With *application* permissions,
> it's targeted via `users/{GRAPH_MAILBOX}` — no license or interactive login
> needed.

## Configuration

Copy `.env.example` to `.env` and fill in at least:

| Variable | Description |
|---|---|
| `GRAPH_TENANT_ID` | Directory (tenant) ID |
| `GRAPH_CLIENT_ID` | Application (client) ID |
| `GRAPH_CLIENT_SECRET` | App registration secret |
| `GRAPH_MAILBOX` | SMTP address of the shared mailbox (e.g. `abuse@example.com`) |

Main options (see `.env.example` for the full list):

| Variable | Default | Role |
|---|---|---|
| `REPORTS_FOLDER` | `inbox` | Source folder (*well-known* name or Graph folder ID) |
| `ARCHIVE_FOLDER` | `archive` | Destination folder after processing |
| `POST_PROCESS_ACTION` | `move` | `move` / `mark_read` / `delete` / `none` |
| `POLL_INTERVAL_SECONDS` | `300` | Polling frequency |
| `EXPORT_SOURCE_IP_METRICS` | `false` | Per-source-IP metrics (high cardinality) |
| `OFFLINE_DNS` | `false` | Disables reverse-DNS / GeoIP |
| `METRICS_PORT` | `9797` | `/metrics` endpoint port |

> ⚠️ `REPORTS_FOLDER`/`ARCHIVE_FOLDER` accept a **well-known folder name**
> (`inbox`, `archive`, `deleteditems`, …) or a **Graph folder ID**, not an
> arbitrary display name. For a custom subfolder, provide its ID (retrievable
> via `GET /users/{mailbox}/mailFolders`).

## Running it

```bash
cp .env.example .env      # then edit .env
docker compose up -d --build
docker compose logs -f
curl http://localhost:9797/metrics
```

Quick test without the loop (single pass): `RUN_ONCE=true` in `.env`.

## Exposed metrics

| Metric | Type | Labels | Description |
|---|---|---|---|
| `dmarc_aggregate_messages_total` | counter | `reporter_org`, `policy_domain`, `disposition`, `dkim_aligned`, `spf_aligned`, `dmarc_aligned` | Volume of messages described in aggregate reports |
| `dmarc_aggregate_source_messages_total` | counter | `policy_domain`, `source_ip`, `source_host`, `disposition`, `dmarc_aligned` | Same, per source IP (if `EXPORT_SOURCE_IP_METRICS=true`) |
| `dmarc_reports_processed_total` | counter | `report_type` | Reports parsed (aggregate / failure / smtp_tls) |
| `dmarc_emails_processed_total` | counter | — | Messages processed |
| `dmarc_processing_errors_total` | counter | `stage` | Errors per stage |
| `dmarc_last_poll_timestamp_seconds` | gauge | — | Last poll |
| `dmarc_last_successful_poll_timestamp_seconds` | gauge | — | Last successful poll |
| `dmarc_last_report_end_timestamp_seconds` | gauge | — | End of the most recent report's date range |
| `dmarc_last_poll_duration_seconds` | gauge | — | Duration of the last poll |
| `dmarc_exporter_up` | gauge | — | 1 if the exporter is running |

`dkim_aligned` / `spf_aligned` / `dmarc_aligned` are `pass` or `fail` (DMARC
alignment), `disposition` is `none` / `quarantine` / `reject`.

### Example Grafana / PromQL queries

```promql
# DMARC compliance rate over 24h (per domain)
sum by (policy_domain) (increase(dmarc_aggregate_messages_total{dmarc_aligned="pass"}[24h]))
/
sum by (policy_domain) (increase(dmarc_aggregate_messages_total[24h]))

# Messages failing DMARC per domain
sum by (policy_domain) (increase(dmarc_aggregate_messages_total{dmarc_aligned="fail"}[24h]))

# Top failing sources (if EXPORT_SOURCE_IP_METRICS=true)
topk(10, sum by (source_ip, source_host)
  (increase(dmarc_aggregate_source_messages_total{dmarc_aligned="fail"}[24h])))
```

> Counters reset to zero when the container restarts; use `increase()` /
> `rate()` on the Prometheus side, which natively handle these resets.

## Prometheus

Add the job from `prometheus.example.yml` to your `prometheus.yml`, or put
`parserdmarc` and Prometheus on the same Docker network and target
`parserdmarc:9797`.

## Grafana

A ready-to-use dashboard is provided: `grafana/dashboards/dmarc-overview.json`.

**Manual import**: Grafana → *Dashboards* → *New* → *Import* → *Upload JSON
file* → select the file → choose your Prometheus datasource.

**Automatic provisioning** (recommended): mount the provided folders into the
Grafana container — the dashboard and datasource are then loaded at startup:

```yaml
  grafana:
    image: grafana/grafana:latest
    ports:
      - "3000:3000"
    volumes:
      - ./grafana/provisioning:/etc/grafana/provisioning
      - ./grafana/dashboards:/var/lib/grafana/dashboards
```

> Adjust `url` in `grafana/provisioning/datasources/datasource.yml` to match
> your Prometheus address.

The dashboard offers two variables: **Datasource** (Prometheus source
selection) and **Domain** (multi-select filter on `policy_domain`). Panels:
compliance rate, volumes by DMARC result and disposition, DKIM/SPF alignment,
top failing sources (requires `EXPORT_SOURCE_IP_METRICS=true`), and per-domain
detail.

## Storage

Files are **partitioned by domain** (the DMARC policy domain of the report):
a mailbox receiving reports for several domains stores each one in its own
folder.

```
/data/
├── <domain-1>/
│   ├── attachments/<YYYY>/<MM>/<DD>/<file>      # raw DMARC attachments
│   └── parsed/<YYYY>/<MM>/<DD>/<org>_<id>.json  # parsed reports
├── <domain-2>/
│   ├── attachments/<YYYY>/<MM>/<DD>/<file>
│   └── parsed/<YYYY>/<MM>/<DD>/<org>_<id>.json
├── unparsed/attachments/<YYYY>/<MM>/<DD>/…       # unreadable attachments (nothing is lost)
└── state/processed_reports.json                  # deduplication state
```

> Since the domain is only known after parsing, the attachment is analyzed in
> memory and then stored under the domain's folder. An attachment that fails
> to parse is kept under `unparsed/`.
