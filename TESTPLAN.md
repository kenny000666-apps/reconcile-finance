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

## 6. Read-Only Guarantee
- **What**: Ensure no modification queries (`INSERT`, `UPDATE`, `DELETE`, `DROP`) exist in the new `/api/debug` endpoint or frontend code.
- **How**: Code review and static analysis of `app.py` and `static/index.html`.
- **Expected Result**: Only `SELECT` statements are executed against PostgreSQL and Beancount, and connections are safely closed in `finally` blocks.
