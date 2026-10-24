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
- **What**: Verify that when `account_type` is provided, PG queries include `AND type = %s` in the `WHERE` clause and return only matching rows.
- **How**:
  1. Call `GET /api/debug?account_name=BarclaysPersonal&account_type=CreditCard`.
  2. Inspect `pg_query` and `pg_params` in the JSON response.
  3. Verify `pg_query` contains `AND type = %s`.
  4. Verify `pg_params` includes `CreditCard` as a parameter.
  5. Check `account_type` is returned in the debug JSON response.
- **Expected Result**: The PG query cleanly filters by `type`, preventing cross-type row pollution (e.g. excluding Current or Savings rows when reconciling CreditCard).

## 10. PG Query Uses `type` Column (not `account_type`)
- **What**: Verify that all PostgreSQL queries reference the actual database column `type` and not `account_type`.
- **How**:
  1. Call `GET /api/debug?account_name=BarclaysPersonal&account_type=CreditCard`.
  2. Inspect the returned `pg_query` string in the JSON response.
  3. Verify the WHERE clause specifies `AND type = %s` (or `type IN (...)`) and contains no occurrence of `account_type = %s`.
  4. Call `GET /api/accounts` and verify `fetch_pg_account_types()` issues `SELECT DISTINCT account_name, type AS account_type FROM transactions WHERE type IN (...)`.
- **Expected Result**: Queries execute without column-not-found errors against the PostgreSQL `transactions` table.

## 11. Contaminated Rows Exclusion (`credit` / `debit`)
- **What**: Verify that rows with contaminated or non-standard `type` values (e.g. `credit`, `debit`, typos) are excluded from reconciliation counts.
- **How**:
  1. In PostgreSQL, identify or insert rows with `type = 'credit'` (Amex) or `type = 'debit'` (Barclays).
  2. Call `/api/reconcile` and `/api/debug` for the respective accounts.
  3. Verify that these rows are not present in `pg_rows` and do not contribute to `pg_count` or reconciliation status counts.
  4. Verify that requests supplying an invalid `account_type` parameter (e.g. `account_type=credit` or `account_type=debit`) are rejected with an explicit error.
- **Expected Result**: Only transactions with `type` in `VALID_ACCOUNT_TYPES` (`Current`, `CreditCard`, `Savings`, `Income`) are included in reconciliation; contaminated rows are safely excluded.

## 12. Beancount-Derived Account Types Endpoint
- **What**: Verify `/api/account-types` returns distinct Beancount-derived types with counts extracted from account paths.
- **How**:
  1. Call `GET /api/account-types`.
  2. Inspect the response payload: verify `types` is a list of objects with `type` and `count` keys.
  3. Verify known types from ledger (e.g. `CreditCard`, `Current`, `Savings`) are returned sorted by type.
- **Expected Result**: Distinct Beancount account types are returned with their posting counts; if ledger query fails, `error` string and empty `types` array are returned without crashing.

## 13. Structured Beancount Accounts in `/api/accounts`
- **What**: Verify `/api/accounts` returns `bean_accounts` as a list of structured objects containing `{account, account_name, type}`.
- **How**:
  1. Call `GET /api/accounts`.
  2. Inspect `bean_accounts` in the response JSON.
  3. Verify each entry is an object containing `account` (full path, e.g. `Liabilities:CreditCard:BarclaysPersonal`), `account_name` (leaf name, e.g. `BarclaysPersonal`), and `type` (second segment, e.g. `CreditCard`).
  4. Verify existing fields `pg_accounts` and `mappings` remain intact for backward compatibility.
- **Expected Result**: `bean_accounts` contains `{account, account_name, type}` objects derived from Beancount postings.

## 14. BQL Regex with Explicit `account_type` Parameter
- **What**: Verify `/api/reconcile` and `/api/debug` accept the `account_type` parameter and construct the precise BQL account regex `.*:<type>:<account_name>`.
- **How**:
  1. Call `GET /api/debug?account_name=BarclaysPersonal&account_type=CreditCard`.
  2. Inspect `bean_query` in the JSON response.
  3. Verify the WHERE clause specifies `account ~ '.*:CreditCard:BarclaysPersonal'`.
  4. Call `POST /api/reconcile` with `{"pg_account": "BarclaysPersonal", "account_type": "CreditCard"}` and confirm successful execution.
  5. Call `GET /api/debug?account_name=BarclaysPersonal` (without `account_type`) and verify fallback logic is invoked.
- **Expected Result**: Explicit `account_type` parameter builds the exact root-agnostic regex `.*:<type>:<account_name>`, while omitting it falls back cleanly to PG type discovery or `ACCOUNT_MAP`.

## 15. UI Dual-Dropdown Selection Behavior
- **What**: Verify the web UI provides two dropdowns (Account Type and Account Name) with dynamic filtering and visual indicator.
- **How**:
  1. Load the web interface at `/`.
  2. Verify the "Account Type" dropdown is populated from `/api/account-types` with "All Types" default.
  3. Verify the "Account Name" dropdown is populated from `/api/accounts`.
  4. Select a specific Type (e.g. `CreditCard`) and verify the Account Name dropdown updates to show only accounts of that type.
  5. Select an account and verify the selection badge displays the active `(type, account_name)` pair.
  6. Click "Reconcile" or "Debug" and verify both `account_type` and `pg_account`/`account_name` parameters are dispatched.
- **Expected Result**: User can filter by type or leave "All Types"; both selections are reflected in the UI and transmitted to reconciliation endpoints.

## 16. Account-Scoped Duplicate Check on Import
- **What**: Verify `/api/import` scopes duplicate detection to `(external_id, account_name)`.
- **How**:
  1. POST a record with `external_id="ext-123"` to `/api/import` under `pg_account="BarclaysPersonal"`.
  2. POST a record with the same `external_id="ext-123"` to `/api/import` under a different account `pg_account="AmericanExpress"`.
  3. POST the record again under `pg_account="BarclaysPersonal"`.
- **Expected Result**:
  - The duplicate check query `SELECT 1 FROM transactions WHERE external_id = %s AND account_name = %s` is scoped to the target account.
  - An existing `external_id` under a different account is not treated as a duplicate in application logic.
  - Submitting the same `external_id` for the same account is identified as a duplicate and skipped (`skipped += 1`).

