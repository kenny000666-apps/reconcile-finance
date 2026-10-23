# Test Plan - Debug Screen & /api/debug Endpoint

## 1. Syntax Compilation (`py_compile`)
- **What**: Verify `app.py` compiles successfully without syntax errors.
- **How**: Run `python3 -m py_compile app.py`.
- **Expected Result**: Exit code 0, no syntax errors.

## 2. GET /api/debug with Valid Mapped Account
- **What**: Test `GET /api/debug?account_name=BarclaysPersonal&date_from=2025-01-01&date_to=2025-01-31`.
- **How**: Send HTTP GET request to the FastAPI application (or mock test client).
- **Expected Result**: HTTP 200 JSON response containing `pg_query`, `pg_params`, `pg_rows`, `bean_query`, `bean_rows`, `pg_count`, and `bean_count`.

## 3. Unmapped Account Validation
- **What**: Test `GET /api/debug?account_name=NonExistentAccount`.
- **How**: Send HTTP GET request with an account not present in `ACCOUNT_MAP`.
- **Expected Result**: JSON response containing an `error` message stating no mapping found, without leaking internal details or credentials.

## 4. JSON Serialisability of Decimal and Date Objects
- **What**: Verify that PostgreSQL query results containing `Decimal` amounts and `date`/`datetime` objects are fully JSON-serialisable.
- **How**: Inspect serialization of `pg_rows` and `bean_rows` in API responses.
- **Expected Result**: No `TypeError` (decimals converted to strings, dates to ISO/string format).

## 5. Frontend Debug Modal & Reconcile Flow
- **What**: Verify frontend Debug button and modal integration.
- **How**: Load UI, wait for accounts to load (enabling Reconcile and Debug buttons), click Debug button, verify modal opens, fetches `/api/debug`, displays queries with Copy buttons and raw rows tables side by side, and check that existing Reconcile, Export, and Import flows remain fully operational.
- **Expected Result**: Modal renders correctly, copy buttons copy queries to clipboard, and existing reconciliation workflows are unaffected.

## 6. Read-Only Guarantee (GET + /api/debug scope only)
- **What**: Ensure no modification queries (`INSERT`, `UPDATE`, `DELETE`, `DROP`) exist in the GET endpoints (`/api/accounts`, `/api/export`, `/api/debug`) or in the frontend code paths that invoke them. **Scope note:** `/api/import` is intentionally a WRITE path and is excluded from this guarantee — see Test #8.
- **How**: Code review and static analysis of `app.py` and `static/index.html`, limited to the read-only endpoints above.
- **Expected Result**: Only `SELECT` statements are executed against PostgreSQL and Beancount on the read-only paths, and connections are safely closed in `finally` blocks.

## 7. Multi-root Account Reconciliation
- **What**: Verify the documented limitation that a single PG `account_name` (e.g. `BarclaysPersonal`) can map to multiple Beancount roots (`Liabilities:CreditCard:…`, `Assets:Current:…`, `Assets:Savings:…`), and that the current implementation only queries ONE root (the one in `ACCOUNT_MAP`).
- **How**: Run `/api/debug?account_name=BarclaysPersonal` and inspect the generated BQL query string. Confirm the regex matches only the single Beancount root from `ACCOUNT_MAP`. Cross-check by running a manual BQL query against the other roots to confirm PG rows exist that belong to those roots.
- **Expected Result**: The user understands that only the mapped Beancount root is queried; PG rows that logically belong to other roots (e.g. `Assets:Current:BarclaysPersonal`) will appear as `PG_ONLY` in the reconciliation output.

## 8. /api/import Write Behavior
- **What**: Verify the `/api/import` endpoint correctly inserts `BEAN_ONLY` reconciliation records into the PostgreSQL `transactions` table.
- **How**:
  1. Trigger a reconciliation that produces at least one `BEAN_ONLY` row.
  2. POST those rows to `/api/import` with the corresponding `pg_account`.
  3. Re-run the same `/api/import` request a second time with the identical payload.
- **Expected Result**:
  - First call inserts the rows using `INSERT ... ON CONFLICT (external_id) DO NOTHING` and returns `imported > 0, skipped = 0`.
  - `external_id` generation for rows missing one is deterministic — derived from a `sha256` hash of `date|payee|amount|bean_account`, truncated to 16 chars, and prefixed with `bean-import-`.
  - Second call with the same payload returns `skipped > 0, imported = 0` (no duplicates created).
