# parserdmarc

Container qui récupère les **rapports DMARC** d'une **boîte aux lettres partagée
Office 365**, télécharge et archive les pièces jointes, les parse avec
[`parsedmarc`](https://github.com/domainaware/parsedmarc) et expose des
**métriques Prometheus** prêtes pour un tableau de bord Grafana.

```
O365 (shared mailbox)
   │  Microsoft Graph (app-only / client credentials)
   ▼
parserdmarc ──► /data/<domaine>/attachments/…  (pièces jointes brutes archivées)
   │         ──► /data/<domaine>/parsed/…       (rapports parsés en JSON)
   │            (stockage partitionné par domaine DMARC)
   │
   └──► :9797/metrics  ◄── Prometheus  ◄── Grafana
        (métriques étiquetées par policy_domain)
```

## Fonctionnement

1. Toutes les `POLL_INTERVAL_SECONDS`, le service liste les messages **non lus**
   du dossier configuré de la boîte partagée via Microsoft Graph.
2. Pour chaque message, il télécharge les pièces jointes (fichiers), les
   **sauvegarde** sur le volume `/data`, puis les parse (zip/gz/xml gérés par
   `parsedmarc`).
3. Les rapports **agrégés** alimentent les compteurs Prometheus ; le JSON parsé
   est aussi sauvegardé.
4. Le message est ensuite **déplacé** vers le dossier d'archive (configurable :
   `move` / `mark_read` / `delete` / `none`) pour ne pas être retraité.

La déduplication s'appuie sur l'action de post-traitement **et** sur le
`report_id` (mémorisé dans `/data/state/processed_reports.json`), ce qui évite
tout double comptage même en cas de retraitement.

## Pré-requis Microsoft Entra ID (app registration)

L'authentification utilise le flux **client credentials** (app-only), pérenne et
sans mot de passe utilisateur.

### 1. Créer l'app registration

**Entra admin center** ([entra.microsoft.com](https://entra.microsoft.com)) →
*Identity → Applications → App registrations → New registration*.

| Champ | Valeur |
|---|---|
| **Name** | `parserdmarc` |
| **Supported account types** | *Accounts in this organizational directory only (Single tenant)* |
| **Redirect URI** | *(laisser vide)* |

Sur la page *Overview*, notez l'**Application (client) ID** et le
**Directory (tenant) ID**.

### 2. Créer le secret

*Certificates & secrets → Client secrets → New client secret* → expiration 12 ou
24 mois. **Copiez immédiatement la valeur (`Value`)** — elle ne sera plus affichée.

### 3. Permissions Graph (application)

*API permissions → Add a permission → Microsoft Graph → **Application
permissions*** :

| Permission | Pourquoi |
|---|---|
| **`Mail.ReadWrite`** | Lire les mails **et** les déplacer/marquer-lus/supprimer après traitement |

Puis **Grant admin consent for \<tenant\>** (le statut doit passer au vert ✅).

> ⚠️ Choisir **Application** (pas *Delegated*). Si `POST_PROCESS_ACTION=none`,
> `Mail.Read` suffit. Une permission *application* donne accès à **toutes** les
> boîtes du tenant → d'où la restriction à l'étape 4.

### 4. Restreindre à la seule boîte (Exchange Online PowerShell)

Exemple pour la boîte **`abuse@example.com`**. Remplacez `<CLIENT_ID>` par
l'Application (client) ID.

```powershell
# Connexion
Connect-ExchangeOnline -UserPrincipalName admin@example.com

# 1. Groupe de sécurité mail-enabled contenant la boîte cible
New-DistributionGroup -Name "DMARC-App-Scope" -Type Security `
  -PrimarySmtpAddress dmarc-app-scope@example.com `
  -Members abuse@example.com

# 2. L'app ne peut accéder QU'aux membres du groupe
New-ApplicationAccessPolicy -AppId "<CLIENT_ID>" `
  -PolicyScopeGroupId dmarc-app-scope@example.com `
  -AccessRight RestrictAccess `
  -Description "parserdmarc - acces restreint a abuse@example.com"

# 3. Vérifier (Granted dans le scope, Denied en dehors)
Test-ApplicationAccessPolicy -Identity abuse@example.com  -AppId "<CLIENT_ID>"
Test-ApplicationAccessPolicy -Identity admin@example.com  -AppId "<CLIENT_ID>"
```

> La propagation peut prendre jusqu'à ~30 min. Pour ajouter une autre boîte au
> périmètre : `Add-DistributionGroupMember -Identity dmarc-app-scope@example.com -Member autre@example.com`.

### Droits requis pour le paramétrage

| Action | Rôle nécessaire |
|---|---|
| Créer l'app registration | *Application Developer* (ou tout utilisateur si non restreint) |
| **Grant admin consent** (étape 3) | *Global Administrator* ou *Privileged Role Administrator* |
| Application Access Policy (étape 4) | *Exchange Administrator* |

> Les rapports DMARC arrivent dans la boîte partagée. Avec les permissions
> *application*, on cible la boîte via `users/{GRAPH_MAILBOX}` — pas besoin de
> licence ni de connexion interactive.

## Configuration

Copiez `.env.example` vers `.env` et renseignez au minimum :

| Variable | Description |
|---|---|
| `GRAPH_TENANT_ID` | Directory (tenant) ID |
| `GRAPH_CLIENT_ID` | Application (client) ID |
| `GRAPH_CLIENT_SECRET` | Secret de l'app registration |
| `GRAPH_MAILBOX` | Adresse SMTP de la boîte partagée (ex. `abuse@example.com`) |

Principales options (voir `.env.example` pour la liste complète) :

| Variable | Défaut | Rôle |
|---|---|---|
| `REPORTS_FOLDER` | `inbox` | Dossier source (nom *well-known* ou ID de dossier Graph) |
| `ARCHIVE_FOLDER` | `archive` | Dossier de destination après traitement |
| `POST_PROCESS_ACTION` | `move` | `move` / `mark_read` / `delete` / `none` |
| `POLL_INTERVAL_SECONDS` | `300` | Fréquence de relève |
| `EXPORT_SOURCE_IP_METRICS` | `false` | Métriques par IP source (forte cardinalité) |
| `OFFLINE_DNS` | `false` | Désactive reverse-DNS / GeoIP |
| `METRICS_PORT` | `9797` | Port de l'endpoint `/metrics` |

> ⚠️ `REPORTS_FOLDER`/`ARCHIVE_FOLDER` acceptent un **nom de dossier well-known**
> (`inbox`, `archive`, `deleteditems`, …) ou un **ID de dossier Graph**, pas un
> nom d'affichage arbitraire. Pour un sous-dossier personnalisé, fournissez son
> ID (récupérable via `GET /users/{mailbox}/mailFolders`).

## Lancement

```bash
cp .env.example .env      # puis éditez .env
docker compose up -d --build
docker compose logs -f
curl http://localhost:9797/metrics
```

Test rapide sans boucle (un seul passage) : `RUN_ONCE=true` dans `.env`.

## Métriques exposées

| Métrique | Type | Labels | Description |
|---|---|---|---|
| `dmarc_aggregate_messages_total` | counter | `reporter_org`, `policy_domain`, `disposition`, `dkim_aligned`, `spf_aligned`, `dmarc_aligned` | Volume de messages décrits dans les rapports agrégés |
| `dmarc_aggregate_source_messages_total` | counter | `policy_domain`, `source_ip`, `source_host`, `disposition`, `dmarc_aligned` | Idem par IP source (si `EXPORT_SOURCE_IP_METRICS=true`) |
| `dmarc_reports_processed_total` | counter | `report_type` | Rapports parsés (aggregate / failure / smtp_tls) |
| `dmarc_emails_processed_total` | counter | — | Messages traités |
| `dmarc_processing_errors_total` | counter | `stage` | Erreurs par étape |
| `dmarc_last_poll_timestamp_seconds` | gauge | — | Dernière relève |
| `dmarc_last_successful_poll_timestamp_seconds` | gauge | — | Dernière relève réussie |
| `dmarc_last_report_end_timestamp_seconds` | gauge | — | Fin de la plage du rapport le plus récent |
| `dmarc_last_poll_duration_seconds` | gauge | — | Durée de la dernière relève |
| `dmarc_exporter_up` | gauge | — | 1 si l'exporter tourne |

`dkim_aligned` / `spf_aligned` / `dmarc_aligned` valent `pass` ou `fail`
(alignement DMARC), `disposition` vaut `none` / `quarantine` / `reject`.

### Exemples de requêtes Grafana / PromQL

```promql
# Taux de conformité DMARC sur 24h (par domaine)
sum by (policy_domain) (increase(dmarc_aggregate_messages_total{dmarc_aligned="pass"}[24h]))
/
sum by (policy_domain) (increase(dmarc_aggregate_messages_total[24h]))

# Messages en échec DMARC par domaine
sum by (policy_domain) (increase(dmarc_aggregate_messages_total{dmarc_aligned="fail"}[24h]))

# Top sources en échec (si EXPORT_SOURCE_IP_METRICS=true)
topk(10, sum by (source_ip, source_host)
  (increase(dmarc_aggregate_source_messages_total{dmarc_aligned="fail"}[24h])))
```

> Les compteurs repartent de zéro au redémarrage du conteneur ; utilisez
> `increase()` / `rate()` côté Prometheus, qui gèrent nativement ces remises à
> zéro.

## Prometheus

Ajoutez le job de `prometheus.example.yml` à votre `prometheus.yml`, ou placez
`parserdmarc` et Prometheus sur le même réseau Docker et ciblez
`parserdmarc:9797`.

## Grafana

Un tableau de bord prêt à l'emploi est fourni : `grafana/dashboards/dmarc-overview.json`.

**Import manuel** : Grafana → *Dashboards* → *New* → *Import* → *Upload JSON file*
→ sélectionnez le fichier → choisissez votre datasource Prometheus.

**Provisioning automatique** (recommandé) : montez les dossiers fournis dans le
conteneur Grafana — le dashboard et la datasource sont alors chargés au démarrage :

```yaml
  grafana:
    image: grafana/grafana:latest
    ports:
      - "3000:3000"
    volumes:
      - ./grafana/provisioning:/etc/grafana/provisioning
      - ./grafana/dashboards:/var/lib/grafana/dashboards
```

> Ajustez `url` dans `grafana/provisioning/datasources/datasource.yml` selon
> l'adresse de votre Prometheus.

Le dashboard propose deux variables : **Datasource** (sélection de la source
Prometheus) et **Domaine** (filtre multi-sélection sur `policy_domain`). Panneaux :
taux de conformité, volumes par résultat DMARC et par disposition, alignement
DKIM/SPF, top des sources en échec (nécessite `EXPORT_SOURCE_IP_METRICS=true`) et
détail par domaine.

## Stockage

Les fichiers sont **partitionnés par domaine** (le domaine de la politique DMARC
du rapport) : une boîte recevant plusieurs domaines range donc chacun dans son
propre dossier.

```
/data/
├── <domaine-1>/
│   ├── attachments/<AAAA>/<MM>/<JJ>/<fichier>   # pièces jointes DMARC brutes
│   └── parsed/<AAAA>/<MM>/<JJ>/<org>_<id>.json  # rapports parsés
├── <domaine-2>/
│   ├── attachments/<AAAA>/<MM>/<JJ>/<fichier>
│   └── parsed/<AAAA>/<MM>/<JJ>/<org>_<id>.json
├── unparsed/attachments/<AAAA>/<MM>/<JJ>/…       # PJ illisibles (rien n'est perdu)
└── state/processed_reports.json                 # état de déduplication
```

> Le domaine n'étant connu qu'après parsing, la pièce jointe est analysée en
> mémoire puis rangée dans le dossier du domaine. Une PJ qui ne parse pas est
> conservée sous `unparsed/`.
