# reconcile-finance

Web app for reconciling PostgreSQL finance transactions against a Beancount ledger.

## Architecture

- **Backend**: FastAPI (Python 3.12)
- **Frontend**: Vanilla HTML/JS (no framework)
- **Data sources**:
  - PostgreSQL `transactions` table (via in-cluster service)
  - Beancount ledger file (via PVC mount)

## Deployment

Deployed in k3s `apps` namespace via Flux GitOps. Manifests live in the `k3s/apps` module.

- **Ingress**: http://reconcile.k3s
- **Image**: `ghcr.io/kenny000666-apps/reconcile-finance:v0.1.0`

## Local Development

```bash
pip install -r requirements.txt
PG_PASSWORD=secret BEAN_PATH=/path/to/main.bean uvicorn app:app --reload --port 8000
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PG_HOST` | `postgres.datastores.svc.cluster.local` | PostgreSQL host |
| `PG_PORT` | `5432` | PostgreSQL port |
| `PG_DB` | `finance` | Database name |
| `PG_USER` | `postgres` | Database user |
| `PG_PASSWORD` | *(required)* | Database password |
| `BEAN_PATH` | `/data/main.bean` | Path to Beancount ledger |

## Debug Endpoint

- **Endpoint**: `GET /api/debug`
- **Query Parameters**:
  - `account_name` (string, default: `BarclaysPersonal`)
  - `date_from` (string, default: `2025-01-01`)
  - `date_to` (string, default: `2025-01-31`)
- **Description**: Read-only endpoint that executes the underlying PostgreSQL query and Beancount BQL query for the specified account and date range, returning raw rows, query strings, parameters, and counts. If either data source fails, errors are captured per-source (`pg_error` / `bean_error`) returning HTTP 200 with an error dict rather than 500.

## API Endpoints

| Endpoint | Method | Type | Description |
|----------|--------|------|-------------|
| `/api/accounts` | GET | Read-only | Lists distinct PG `account_name` values and discovered Beancount accounts |
| `/api/reconcile` | POST | Read-only | Runs the reconciliation between PG and Beancount for a given account + date range |
| `/api/export` | GET | Read-only | Streams the current reconciliation result as a CSV download |
| `/api/debug` | GET | Read-only | Returns raw PG rows, BQL rows, query strings and counts for troubleshooting |
| `/api/import` | POST | **WRITE** | Inserts selected `BEAN_ONLY` rows into the PG `transactions` table (`INSERT ... ON CONFLICT DO NOTHING`). Used to import transactions that exist only in the Beancount ledger back into PostgreSQL. |

## Tolerance

Amount matching between PostgreSQL and Beancount uses a configurable tolerance (in currency units):

- **Default**: `0.01` (one cent)
- **Configurable via**: the `tolerance` field in the `/api/reconcile` request body and the `/api/export` query parameter
- **Comparison rule**: if `|pg_amount - bean_amount| ≤ tolerance` the row is classified `MATCH`; if the absolute diff is `> tolerance` the row is classified `MISMATCH`

## Known Limitations

- **Dynamic account mapping with hardcoded fallback**: when the PG `transactions` table exposes an `account_type` column, the app builds a **root-agnostic** Beancount regex from `(account_type, account_name)` — `.*:<Type>:<AccountName>` with both fields regex-escaped — and derives the sign multiplier from the type (`-1` for `CreditCard`, `+1` otherwise). When `account_type` is **not** available (older schema), the app falls back to the hardcoded `ACCOUNT_MAP` in `app.py` and surfaces a `warning` field in API responses. Adding support for a brand-new PG account type still requires no code change as long as `account_type` is present.
- **One PG account can span multiple Beancount roots**: a single PG `account_name` (e.g. `BarclaysPersonal`) can appear under several Beancount roots at once — for example `Liabilities:CreditCard:BarclaysPersonal`, `Assets:Current:BarclaysPersonal` and `Assets:Savings:BarclaysPersonal`. The dynamic regex `.*:<Type>:<AccountName>` intentionally matches the account under **any** root, so all matching roots are reconciled together. When the hardcoded `ACCOUNT_MAP` fallback is in effect, only ONE root is reconciled and PG rows belonging to other roots will appear as `PG_ONLY`.
- **Account discovery**: `/api/accounts` lists all Beancount accounts across every root (no longer restricted to `Liabilities:CreditCard:`) and, when the PG schema supports it, returns distinct `(account_name, account_type)` pairs used to populate the dropdown as `Name (Type)`.

