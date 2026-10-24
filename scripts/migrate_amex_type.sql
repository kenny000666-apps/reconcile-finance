-- One-time data fix: Amex 2025 imports wrote type='credit' (transaction direction)
-- but semantically these are CreditCard account transactions.
-- Barclays 'debit' rows are intentionally NOT migrated — they span Savings and
-- Current and cannot be split without additional source data; they remain
-- excluded from reconciliation by the VALID_ACCOUNT_TYPES filter.
--
-- Idempotent: re-running is safe.
-- Expected rows affected: ~817 (Amex 2025 imports).

BEGIN;

-- Dry-run count first (for the operator to verify before COMMIT)
SELECT COUNT(*) AS amex_credit_rows_before
  FROM transactions
 WHERE type = 'credit'
   AND account_name = 'AmericanExpress';

UPDATE transactions
   SET type = 'CreditCard',
       updated_at = NOW()
 WHERE type = 'credit'
   AND account_name = 'AmericanExpress';

-- Verify
SELECT type, COUNT(*) AS n
  FROM transactions
 WHERE account_name = 'AmericanExpress'
 GROUP BY type
 ORDER BY n DESC;

COMMIT;
