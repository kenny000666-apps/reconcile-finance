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

## 7. Account Mapping Resolution (`resolve_mapping`)
- **What**: Verify `resolve_mapping` behavior when `account_type` is provided vs absent.
- **How**: Test `resolve_mapping("BarclaysPersonal", "CreditCard")` and `resolve_mapping("BarclaysPersonal", None)`:
  1. When `account_type` exists: verify BQL regex is root-agnostic `.*:<Type>:<AccountName>` (e.g. `.*:CreditCard:BarclaysPersonal`), sign multiplier is derived correctly (`-1` for CreditCard, `1` otherwise), and warning is `None`.
  2. When `account_type` is absent: verify fallback to hardcoded `ACCOUNT_MAP` with an appropriate warning returned.
- **Expected Result**: Matching BQL regex matches only the specific account type under any root; missing `account_type` falls back gracefully to `ACCOUNT_MAP`.

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

## 9. PG Type Filter
- **What**: Verify that when `account_type` is provided, PG queries include `AND account_type = %s` in the `WHERE` clause and return only matching rows.
- **How**:
  1. Call `GET /api/debug?account_name=BarclaysPersonal&account_type=CreditCard`.
  2. Inspect `pg_query` and `pg_params` in the JSON response.
  3. Verify `pg_query` contains `AND account_type = %s`.
  4. Verify `pg_params` includes `CreditCard` as a parameter.
  5. Check `account_type` is returned in the debug JSON response.
- **Expected Result**: The PG query cleanly filters by `account_type`, preventing cross-type row pollution (e.g. excluding Current or Savings rows when reconciling CreditCard).
