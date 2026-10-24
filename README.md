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
| `/api/account-types` | GET | Read-only | Returns distinct Beancount-derived account types with counts |
| `/api/accounts` | GET | Read-only | Lists distinct PG `account_name` values and discovered Beancount accounts with types |
| `/api/reconcile` | POST | Read-only | Runs the reconciliation between PG and Beancount for a given account + date range |
| `/api/export` | GET | Read-only | Streams the current reconciliation result as a CSV download |
| `/api/debug` | GET | Read-only | Returns raw PG rows, BQL rows, query strings and counts for troubleshooting |
| `/api/import` | POST | **WRITE** | Inserts selected `BEAN_ONLY` rows into the PG `transactions` table (`INSERT ... ON CONFLICT DO NOTHING`). Used to import transactions that exist only in the Beancount ledger back into PostgreSQL. |

## Account Type Selection

Beancount's account hierarchy serves as the primary, authoritative reference for account types. Account paths structured as `Root:Type:AccountName` (e.g. `Liabilities:CreditCard:BarclaysPersonal` or `Assets:Current:Revolut`) inherently provide clean, typed classification without data contamination.

- **Dual-Dropdown UI**: The web interface provides separate dropdowns for **Account Type** and **Account Name**.
  - Selecting an **Account Type** filters the **Account Name** list to accounts matching that type.
  - The currently selected `(type, account_name)` pair is clearly indicated in the UI and debug inspector.
- **Precise BQL Matching**: When an `account_type` is selected, reconciliation builds a root-agnostic regex:
  `.*:<type>:<account_name>`
  This matches the target account cleanly under its Beancount root (e.g., `Liabilities:CreditCard:BarclaysPersonal`) while excluding other accounts sharing the same name under different roots.
- **Optional Selection & Fallback**: Account Type selection is optional. If not selected ("All Types"), the application falls back to discovering the account type from PostgreSQL's `type` column or using the hardcoded `ACCOUNT_MAP` fallback.

## Tolerance

Amount matching between PostgreSQL and Beancount uses a configurable tolerance (in currency units):

- **Default**: `0.01` (one cent)
- **Configurable via**: the `tolerance` field in the `/api/reconcile` request body and the `/api/export` query parameter
- **Comparison rules**:
  - **Pass A (external_id match)**: uses `abs(|pg| - |bean|) <= tolerance` (compares absolute values so sign conventions across data sources do not cause false mismatches).
  - **Pass B (date,payee composite)**: uses `|pg - bean| <= tolerance` (signed difference comparison on aggregated amounts).
  - If the computed difference is `<= tolerance`, the row is classified `MATCH`; otherwise it is classified `MISMATCH`.

## Known Limitations

- **Clean Beancount Types vs Contaminated PG Data**: Beancount-derived types are clean by design (derived from ledger hierarchy like `CreditCard`, `Current`, `Savings`, `Income`), avoiding legacy or direction-based values present in PostgreSQL (such as `type = 'credit'`, `type = 'debit'`, or typos).
- **Database Column `type` vs API Parameter `account_type`**: The PostgreSQL schema stores transaction classification in the `type` column (not `account_type`). Clean allowed values are `Current`, `CreditCard`, `Savings`, and `Income` (defined as `VALID_ACCOUNT_TYPES` in `app.py`). The API parameter and frontend state remain named `account_type` for backward compatibility.
- **Dynamic account mapping with hardcoded fallback**: when the PG `transactions` table exposes the `type` column, the app builds a **root-agnostic** Beancount regex from `(account_type, account_name)` — `.*:<Type>:<AccountName>` with both fields regex-escaped — and derives the sign multiplier from the type (`-1` for `CreditCard`, `+1` otherwise). When the `type` column is **not** available (older schema), the app falls back to the hardcoded `ACCOUNT_MAP` in `app.py` and surfaces a `warning` field in API responses. Adding support for a brand-new PG account type requires no code change as long as it matches `VALID_ACCOUNT_TYPES`.
- **Accounts spanning multiple Beancount types**: the regex `.*:<Type>:<AccountName>` reconciles ONE account type at a time. A single account (e.g. `BarclaysPersonal`) that spans multiple Beancount types (e.g. `Liabilities:CreditCard:BarclaysPersonal` and `Assets:Current:BarclaysPersonal`) cannot be reconciled in a single pass — accounts spanning multiple Beancount types require separate reconciliation runs per type. Selecting the Type dropdown restricts reconciliation cleanly to that specific type.
- **Contaminated type values excluded**: Transactions imported with legacy or direction-based values (such as `type = 'credit'`, `type = 'debit'`, or parsing typos) are strictly excluded from reconciliation queries via the `VALID_ACCOUNT_TYPES` filter until migrated. For American Express imports with `type = 'credit'`, refer to `scripts/migrate_amex_type.sql` to migrate them to `CreditCard`. Barclays `debit` rows (~391 rows) remain excluded as they span both Savings and Current and cannot be split without additional source data.
- **Account discovery**: `/api/accounts` returns Beancount accounts as structured objects `{account, account_name, type}` alongside PG account names, and `/api/account-types` returns distinct Beancount types with counts.

