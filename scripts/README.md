# Database Migration Scripts

## Amex Type Migration (`migrate_amex_type.sql`)

One-time data fix for American Express transactions in PostgreSQL.

### Background
During the 2025 imports, Amex transactions were imported with `type = 'credit'` (reflecting transaction direction rather than account type). Semantically, these transactions belong to the `CreditCard` account category.

### Why Barclays 'debit' is Excluded
Barclays 2025 imports contain `type = 'debit'` rows (~391 rows). These are intentionally **not** migrated by this script because Barclays accounts span both `Savings` and `Current` categories. Splitting them correctly requires additional source data that is not present in the database. Instead, these rows remain excluded from reconciliation via the `VALID_ACCOUNT_TYPES` filter (`Current`, `CreditCard`, `Savings`, `Income`).

### Idempotency
The script is fully idempotent. If re-run, the `UPDATE` query will match 0 rows and make no further changes.

### How to Run
Execute the script against the production database via the Kubernetes deployment pod:

```bash
kubectl exec -n apps deploy/reconcile-finance -- psql "$DATABASE_URL" -f /dev/stdin < scripts/migrate_amex_type.sql
```

Or using the pod's PostgreSQL environment variables:

```bash
kubectl exec -i -n apps deploy/reconcile-finance -- sh -c 'PGPASSWORD="$PG_PASSWORD" psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" -f -' < scripts/migrate_amex_type.sql
```
