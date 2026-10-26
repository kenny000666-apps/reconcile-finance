"""PG vs Beancount Reconciliation Web App."""
import csv
import io
import os
import re
from decimal import Decimal
from typing import Any

import beanquery
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import psycopg2
import psycopg2.extras

app = FastAPI(title="Reconcile Finance")
app.mount("/static", StaticFiles(directory="static"), name="static")

BEAN_PATH = os.environ.get("BEAN_PATH", "/data/main.bean")
PG_HOST = os.environ.get("PG_HOST", "postgres.datastores.svc.cluster.local")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_DB = os.environ.get("PG_DB", "finance")
PG_USER = os.environ.get("PG_USER", "postgres")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "")

# Account mapping: PG account name -> (bean account regex, sign multiplier)
# Used as a fallback when the PG transactions table has no type column.
ACCOUNT_MAP = {
    "AmericanExpress": ("Liabilities:CreditCard:AmericanExpress", -1),
    "BarclaysPersonal": ("Liabilities:CreditCard:BarclaysPersonal", -1),
    "HalifaxPersonal": ("Liabilities:CreditCard:HalifaxPersonal", -1),
    "Revolut": ("Assets:Current:Revolut", 1),
    "ZopaBank": ("Assets:Current:ZopaBank", 1),
    "Discover": ("Liabilities:CreditCard:Discover", -1),
}

VALID_ACCOUNT_TYPES = {
    "Current", "Savings", "CreditCard", "Loan", "Mortgage",
    "Pension", "Income", "Investment", "Cash", "Suspended", "Fixed",
}

# Whitelists for the audited custom-update admin endpoints. Only these columns
# may appear in SET / WHERE clauses respectively — anything else is rejected
# with a 400 before any SQL is built.
SET_COLUMNS = {"type", "account_name", "category", "payee", "description"}
WHERE_COLUMNS = {"account_name", "type", "external_id", "transaction_date", "amount", "payee", "description", "category"}


def extract_bean_type(bean_account: str) -> str | None:
    """Extract account type from beancount path (e.g., 'Assets:Current:Barclays' -> 'Current')."""
    parts = bean_account.split(":")
    if len(parts) >= 2 and parts[0] in ("Assets", "Liabilities") and parts[1] in VALID_ACCOUNT_TYPES:
        return parts[1]
    return None


def _build_bean_regex(account_type: str, account_name: str) -> str:
    """Build a balance-sheet BQL account regex from (account_type, account_name).

    Matches the account under Assets: or Liabilities: roots only, e.g.
    ('CreditCard', 'BarclaysPersonal') -> '(Assets|Liabilities):CreditCard:BarclaysPersonal'
    which matches Liabilities:CreditCard:BarclaysPersonal while excluding
    expense/income legs like Expenses:Unattributed:CreditCard:BarclaysPersonal.
    """
    return f"(Assets|Liabilities):{re.escape(account_type)}:{re.escape(account_name)}"


def _sign_for_type(account_type: str) -> int:
    """Sign multiplier: CreditCard liabilities are negative, everything else positive."""
    return -1 if account_type == "CreditCard" else 1


def fetch_pg_account_types():
    """Try to read distinct (account_name, account_type) pairs from PG.

    Returns (pairs, warning):
      - pairs: list of {"account_name": str, "account_type": str} on success, else None
      - warning: None on success, else a human-readable fallback warning string
    """
    conn = None
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        clean_placeholders = ", ".join(["%s"] * len(VALID_ACCOUNT_TYPES))
        cur.execute(
            f"SELECT DISTINCT account_name, type AS account_type FROM transactions "
            f"WHERE type IN ({clean_placeholders}) "
            f"ORDER BY account_name, account_type",
            tuple(sorted(VALID_ACCOUNT_TYPES)),
        )
        pairs = [
            {"account_name": row["account_name"], "account_type": row["account_type"]}
            for row in cur.fetchall()
        ]
        cur.close()
        return pairs, None
    except Exception as e:
        return None, (
            f"type column not available in PG transactions table "
            f"({e}); falling back to hardcoded ACCOUNT_MAP"
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def resolve_mapping(pg_account: str, account_type: str | None = None):
    """Resolve (bean_regex, sign_mult, warning) for a PG account.

    Preference order:
      1. Explicit (pg_account, account_type) -> (Assets|Liabilities) regex
      2. Lookup of account_type from live PG query (per account_name)
      3. Hardcoded ACCOUNT_MAP fallback (with warning)
    """
    # 1. Caller supplied account_type directly (frontend sends it when known)
    if account_type:
        if account_type not in VALID_ACCOUNT_TYPES:
            return None, None, f"Invalid account_type '{account_type}'. Must be one of {VALID_ACCOUNT_TYPES}"
        return _build_bean_regex(account_type, pg_account), _sign_for_type(account_type), None

    # 2. Try to discover account_type from PG for this account_name
    pairs, warn = fetch_pg_account_types()
    if pairs is not None:
        for p in pairs:
            if p["account_name"] == pg_account:
                at = p["account_type"]
                if at in VALID_ACCOUNT_TYPES:
                    return _build_bean_regex(at, pg_account), _sign_for_type(at), None
        # account_name not found in PG at all — fall through to hardcoded map
        mapping = ACCOUNT_MAP.get(pg_account)
        if mapping:
            return mapping[0], mapping[1], (
                f"account '{pg_account}' not present in PG transactions; "
                f"using hardcoded ACCOUNT_MAP fallback"
            )
        return None, None, f"No mapping for PG account: {pg_account}"

    # 3. type column unavailable — hardcoded fallback
    mapping = ACCOUNT_MAP.get(pg_account)
    if not mapping:
        return None, None, f"No mapping for PG account: {pg_account}"
    return mapping[0], mapping[1], warn


def get_pg_conn():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASSWORD,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def get_bean_conn():
    return beanquery.connect(f"beancount:///{BEAN_PATH}")


def _build_where(conditions: dict) -> tuple[str, list]:
    """Build 'col1 = %s AND col2 = %s' + params list. Keys must be in WHERE_COLUMNS.

    Empty/falsy values are skipped so that e.g. account_name='' means 'no filter'
    rather than 'match empty string'. Returns '1=1' when no conditions remain.
    """
    invalid = set(conditions.keys()) - WHERE_COLUMNS
    if invalid:
        raise ValueError(f"Invalid WHERE columns: {invalid}")
    parts = []
    params = []
    for col, val in conditions.items():
        if val in (None, "", []):
            continue  # treat empty as no-filter
        parts.append(f"{col} = %s")
        params.append(val)
    if not parts:
        return "1=1", []
    return " AND ".join(parts), params


def _json_safe(obj):
    import datetime, decimal
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


@app.on_event("startup")
def _ensure_update_history_table():
    """Idempotent DDL for the audited custom-update history table."""
    conn = None
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS transaction_update_history (
                id SERIAL PRIMARY KEY,
                executed_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
                source TEXT,
                description TEXT,
                set_clause JSONB,
                where_clause JSONB,
                rows_affected INTEGER,
                old_records JSONB,
                rolled_back_at TIMESTAMP WITHOUT TIME ZONE
            )
            """
        )
        conn.commit()
        cur.close()
    except Exception:
        # Table creation is best-effort at startup; endpoints will surface
        # errors if the table is genuinely missing at request time.
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html") as f:
        return HTMLResponse(content=f.read())


@app.get("/api/account-types")
async def get_account_types():
    """Return distinct Beancount-derived account types with counts."""
    bean_types = []
    try:
        bconn = get_bean_conn()
        cur = bconn.cursor()
        # Query all accounts, extract type from path
        cur.execute("SELECT DISTINCT account FROM postings ORDER BY account")
        type_counts = {}
        for row in cur.fetchall():
            account = row[0]
            bean_type = extract_bean_type(account)
            if bean_type:
                type_counts[bean_type] = type_counts.get(bean_type, 0) + 1
        cur.close()
        bconn.close()
        bean_types = [{"type": t, "count": c} for t, c in sorted(type_counts.items())]
    except Exception as e:
        return {"error": f"Bean query failed: {e}", "types": []}

    return {"types": bean_types}


_accounts_cache: list[dict[str, str]] | None = None


@app.get("/api/accounts")
async def get_accounts():
    """Return distinct Beancount accounts with their derived types as [{"name": ..., "type": ...}]."""
    global _accounts_cache
    if _accounts_cache is not None:
        return _accounts_cache

    accounts_map: dict[str, str] = {}
    try:
        bconn = get_bean_conn()
        cur = bconn.cursor()
        cur.execute("SELECT DISTINCT account FROM postings ORDER BY account")
        for row in cur.fetchall():
            account = row[0]
            bean_type = extract_bean_type(account)
            if not bean_type:
                continue
            account_name = account.split(":")[-1]
            if account_name and account_name not in accounts_map:
                accounts_map[account_name] = bean_type
        cur.close()
        bconn.close()
    except Exception:
        pass

    # Fallback to PG / ACCOUNT_MAP if Beancount is empty or unavailable
    if not accounts_map:
        try:
            pairs, _ = fetch_pg_account_types()
            if pairs:
                for p in pairs:
                    name = p.get("account_name")
                    t = p.get("account_type")
                    if name and name not in accounts_map:
                        accounts_map[name] = t
        except Exception:
            pass

    if not accounts_map:
        for name, (full_acc, _) in ACCOUNT_MAP.items():
            t = extract_bean_type(full_acc) or "Current"
            if name not in accounts_map:
                accounts_map[name] = t

    result = [{"name": name, "type": t} for name, t in sorted(accounts_map.items())]
    if result:
        _accounts_cache = result
    return result


@app.post("/api/reconcile")
async def reconcile(body: dict[str, Any]):
    date_from = body.get("date_from", "2025-01-01")
    date_to = body.get("date_to", "2025-01-31")
    pg_account = body.get("pg_account", "") or ""
    columns = body.get("columns", ["date", "payee", "amount"])
    group_by = body.get("group_by", "date,payee")
    # Tolerance for amount matching (in currency units). Default 0.01 flags
    # anything beyond a single cent as a mismatch between PG and Beancount.
    tolerance = Decimal(str(body.get("tolerance", "0.01")))

    # Guard against frontend race condition sending placeholder values.
    # Empty string is allowed and means 'no account filter' (All accounts).
    if pg_account in ("Loading...", "No accounts found"):
        return {"error": "Invalid account selection. Please wait for accounts to load and select a valid PG account."}

    account_type = body.get("account_type") or None  # optional — empty/missing = no filter
    if account_type and account_type not in VALID_ACCOUNT_TYPES:
        return {"error": f"Invalid account_type '{account_type}'. Must be one of {VALID_ACCOUNT_TYPES}"}

    # Build beancount regex from (account_name, account_type), treating empty as wildcard.
    # ALWAYS keep the (Assets|Liabilities) prefix — prevents double-counting against
    # Expenses/Income legs.
    if pg_account and account_type:
        bean_account_regex = f"(Assets|Liabilities):{re.escape(account_type)}:{re.escape(pg_account)}"
        sign_mult = _sign_for_type(account_type)
        map_warning = None
    elif pg_account and not account_type:
        bean_account_regex = f"(Assets|Liabilities):.*:{re.escape(pg_account)}"
        sign_mult = 1
        map_warning = "wildcard type — sign multiplier forced to +1 (no flip for CreditCard)"
    elif not pg_account and account_type:
        bean_account_regex = f"(Assets|Liabilities):{re.escape(account_type)}:.*"
        sign_mult = _sign_for_type(account_type)
        map_warning = None
    else:
        bean_account_regex = r"(Assets|Liabilities):.*"
        sign_mult = 1
        map_warning = "wildcard account+type — reconciling ALL balance-sheet accounts; sign multiplier forced to +1"

    # Column mappings
    col_map = {"date": "transaction_date", "account": "account_name", "amount": "amount",
               "payee": "payee", "description": "description", "category": "category"}
    bean_col_map = {"date": "date", "transaction_date": "date", "payee": "payee", "description": "narration", "account": "account", "account_name": "account"}

    # Always group by external_id as primary key for stable 1:1 matching.
    # Display columns are independent from grouping — they're shown but don't affect aggregation.
    # Fallback to date+payee composite key only for rows missing external_id.

    # Phase 1: Query PG — each row is one transaction (external_id is unique)
    pg_by_extid = {}   # external_id -> row data
    pg_by_key = {}     # composite key (date,payee) -> row data (fallback for null external_id)
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        # Build WHERE clause with optional account_name and account_type filters
        conditions = ["transaction_date >= %s", "transaction_date < %s"]
        params = [date_from, date_to]
        if pg_account:
            conditions.append("account_name = %s")
            params.append(pg_account)
        if account_type:
            conditions.append("type = %s")
            params.append(account_type)
        where_clause = " AND ".join(conditions)
        query = f"""
            SELECT external_id, transaction_date, payee, description, category,
                   amount, account_name, type, 1 as tx_count
            FROM transactions
            WHERE {where_clause}
            ORDER BY transaction_date, payee
        """
        cur.execute(query, tuple(params))
        for row in cur.fetchall():
            amount = Decimal(str(row["amount"]))
            key_dict = {
                "date": str(row["transaction_date"]),
                "payee": row["payee"] or "",
                "description": row["description"] or "",
                "category": row["category"] or "",
                "external_id": row["external_id"] or "",
                "type": row["type"] or "",
                "account_name": row["account_name"] or "",
            }
            entry = {
                "key_dict": key_dict,
                "amount": amount,
                "count": 1,
            }
            ext_id = row["external_id"]
            if ext_id:
                pg_by_extid[str(ext_id)] = entry
            else:
                # Fallback composite key for rows without external_id
                fallback_key = (str(row["transaction_date"]), row["payee"] or "")
                if fallback_key in pg_by_key:
                    pg_by_key[fallback_key]["amount"] += amount
                    pg_by_key[fallback_key]["count"] += 1
                else:
                    pg_by_key[fallback_key] = entry
        cur.close()
        conn.close()
    except Exception as e:
        return {"error": f"PG query failed: {e}", "results": []}

    # Phase 2: Query Bean — fetch individual postings with external_id from metadata
    bean_by_extid = {}  # external_id -> row data
    bean_by_key = {}    # composite key (date,payee) -> aggregated row data (fallback)
    try:
        bconn = get_bean_conn()
        cur = bconn.cursor()
        # Fetch raw postings; aggregate in Python since BQL can't GROUP BY metadata
        bean_query = f"""
            SELECT date, narration, units(position).number as amount,
                   getitem(entry.meta, 'external_id') as external_id,
                   account
            FROM postings
            WHERE account ~ '{bean_account_regex}'
              AND date >= {date_from} AND date < {date_to}
            ORDER BY date
        """
        cur.execute(bean_query)
        for row in cur.fetchall():
            raw_amount = Decimal(str(row[2]))
            ext_id = row[3]  # May be None
            bean_account = row[4] or ""
            signed_amount = raw_amount * sign_mult
            date_str = str(row[0])
            narration = row[1] or ""
            bean_type = extract_bean_type(bean_account) or ""
            bean_account_name = bean_account.split(":")[-1] if bean_account else ""

            key_dict = {
                "date": date_str,
                "payee": narration,
                "description": narration,
                "category": "",
                "external_id": str(ext_id) if ext_id else "",
                "type": bean_type,
                "account_name": bean_account_name,
                "bean_account": bean_account,
            }

            if ext_id:
                eid_str = str(ext_id)
                if eid_str in bean_by_extid:
                    bean_by_extid[eid_str]["amount"] += signed_amount
                    bean_by_extid[eid_str]["count"] += 1
                else:
                    bean_by_extid[eid_str] = {
                        "key_dict": key_dict,
                        "amount": signed_amount,
                        "count": 1,
                    }
            else:
                # Fallback composite key for postings without external_id
                fallback_key = (date_str, narration)
                if fallback_key in bean_by_key:
                    bean_by_key[fallback_key]["amount"] += signed_amount
                    bean_by_key[fallback_key]["count"] += 1
                else:
                    bean_by_key[fallback_key] = {
                        "key_dict": key_dict,
                        "amount": signed_amount,
                        "count": 1,
                    }
        cur.close()
        bconn.close()
    except Exception as e:
        return {"error": f"Bean query failed: {e}", "results": []}

    # Phase 3: Two-pass matching
    matched_bean_extids = set()  # Track external_ids consumed in Pass A
    matched_pg_fallback_keys = set()  # Track fallback keys consumed in Pass B
    matched_bean_fallback_keys = set()
    results = []

    # Pass A: Match by external_id (primary — most reliable)
    # Use absolute value comparison since PG and Bean may have different sign conventions
    for eid, pg_entry in pg_by_extid.items():
        bean_entry = bean_by_extid.get(eid)
        if bean_entry:
            diff = abs(pg_entry["amount"]) - abs(bean_entry["amount"])
            status = "MATCH" if abs(diff) <= tolerance else "MISMATCH"
            results.append({
                "key": pg_entry["key_dict"],
                "pg_amount": str(pg_entry["amount"]),
                "bean_amount": str(bean_entry["amount"]),
                "diff": str(diff),
                "pg_count": pg_entry["count"],
                "bean_count": bean_entry["count"],
                "status": status,
                "match_type": "external_id",
            })
            matched_bean_extids.add(eid)

    # Pass B: Match by composite key (fallback for rows without external_id)
    all_fallback_keys = set(pg_by_key.keys()) | set(bean_by_key.keys())
    for key in sorted(all_fallback_keys):
        pg = pg_by_key.get(key)
        bean = bean_by_key.get(key)
        key_dict = (pg or bean)["key_dict"]
        if pg and bean:
            diff = pg["amount"] - bean["amount"]
            status = "MATCH" if abs(diff) <= tolerance else "MISMATCH"
            results.append({
                "key": key_dict,
                "pg_amount": str(pg["amount"]),
                "bean_amount": str(bean["amount"]),
                "diff": str(diff),
                "pg_count": pg["count"],
                "bean_count": bean["count"],
                "status": status,
                "match_type": "composite_key",
            })
            matched_pg_fallback_keys.add(key)
            matched_bean_fallback_keys.add(key)
        elif pg:
            results.append({
                "key": key_dict,
                "pg_amount": str(pg["amount"]),
                "bean_amount": None,
                "diff": str(pg["amount"]),
                "pg_count": pg["count"],
                "bean_count": 0,
                "status": "PG_ONLY",
            })
        else:
            results.append({
                "key": key_dict,
                "pg_amount": None,
                "bean_amount": str(bean["amount"]),
                "diff": str(-bean["amount"]),
                "pg_count": 0,
                "bean_count": bean["count"],
                "status": "BEAN_ONLY",
            })

    # Unmatched external_id entries (PG has external_id but Bean doesn't, or vice versa)
    for eid, pg_entry in pg_by_extid.items():
        if eid not in matched_bean_extids:
            results.append({
                "key": pg_entry["key_dict"],
                "pg_amount": str(pg_entry["amount"]),
                "bean_amount": None,
                "diff": str(pg_entry["amount"]),
                "pg_count": pg_entry["count"],
                "bean_count": 0,
                "status": "PG_ONLY",
                "match_type": "external_id",
            })
    for eid, bean_entry in bean_by_extid.items():
        if eid not in matched_bean_extids:
            results.append({
                "key": bean_entry["key_dict"],
                "pg_amount": None,
                "bean_amount": str(bean_entry["amount"]),
                "diff": str(-bean_entry["amount"]),
                "pg_count": 0,
                "bean_count": bean_entry["count"],
                "status": "BEAN_ONLY",
                "match_type": "external_id",
            })

    summary = {
        "total": len(results),
        "match": sum(1 for r in results if r["status"] == "MATCH"),
        "mismatch": sum(1 for r in results if r["status"] == "MISMATCH"),
        "pg_only": sum(1 for r in results if r["status"] == "PG_ONLY"),
        "bean_only": sum(1 for r in results if r["status"] == "BEAN_ONLY"),
    }

    resp = {"summary": summary, "results": results}
    if map_warning:
        resp["warning"] = map_warning
    return resp


@app.get("/api/export")
async def export_csv(
    date_from: str = Query("2025-01-01"),
    date_to: str = Query("2025-01-31"),
    pg_account: str = Query("BarclaysPersonal"),
    account_type: str | None = Query(None),
    tolerance: float = Query(0.01),
):
    account_type = account_type or None
    if account_type and account_type not in VALID_ACCOUNT_TYPES:
        return {"error": f"Invalid account_type '{account_type}'. Must be one of {VALID_ACCOUNT_TYPES}"}

    # Re-run reconciliation to get fresh data
    body = {
        "date_from": date_from,
        "date_to": date_to,
        "pg_account": pg_account,
        "tolerance": tolerance,
        "columns": ["date", "payee", "amount"],
        "group_by": "date,payee",
    }
    if account_type:
        body["account_type"] = account_type
    result = await reconcile(body)
    if "error" in result:
        return {"error": result["error"]}

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Type", "Account", "Date", "Payee", "Description", "Category", "External ID", "PG Amount", "Bean Amount", "Diff", "Status"])
    for r in result["results"]:
        key = r["key"]
        writer.writerow([
            key.get("type", ""),
            key.get("account_name", ""),
            key.get("date", ""),
            key.get("payee", ""),
            key.get("description", ""),
            key.get("category", ""),
            key.get("external_id", ""),
            r["pg_amount"] or "",
            r["bean_amount"] or "",
            r["diff"],
            r["status"],
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=reconcile_{pg_account}_{date_from}.csv"},
    )


@app.post("/api/import")
async def import_bean_only(body: dict[str, Any]):
    """Import BEAN_ONLY reconciliation records into PG."""
    pg_account = body.get("pg_account", "")
    records = body.get("records", [])
    if not pg_account or not records:
        return {"error": "pg_account and records required"}

    account_type = body.get("account_type") or None  # optional — sent by frontend when known
    if account_type and account_type not in VALID_ACCOUNT_TYPES:
        return {"error": f"Invalid account_type '{account_type}'. Must be one of {VALID_ACCOUNT_TYPES}"}
    bean_account, sign_mult, _warn = resolve_mapping(pg_account, account_type)
    if not bean_account:
        return {"error": _warn or f"Unknown account: {pg_account}"}

    imported = 0
    skipped = 0
    conn = get_pg_conn()
    try:
        cur = conn.cursor()
        for rec in records:
            key = rec.get("key", {})
            bean_amount = rec.get("bean_amount")
            if bean_amount is None:
                continue
            # Reverse the sign multiplier to get the original Bean amount for storage
            raw_amount = Decimal(str(bean_amount)) / Decimal(str(sign_mult))
            date_val = key.get("transaction_date") or key.get("date", "")
            payee = key.get("payee", "")
            description = key.get("description", "")
            category = key.get("category", "")
            external_id = key.get("external_id", "")
            # Generate external_id if missing
            if not external_id:
                import hashlib
                h = hashlib.sha256(f"{date_val}|{payee}|{raw_amount}|{bean_account}".encode()).hexdigest()[:16]
                external_id = f"bean-import-{h}"
            try:
                # Check for existing duplicate scoped to this account
                cur.execute(
                    "SELECT 1 FROM transactions WHERE external_id = %s AND account_name = %s",
                    (external_id, pg_account),
                )
                if cur.fetchone():
                    skipped += 1
                    continue

                cur.execute(
                    """INSERT INTO transactions
                       (external_id, transaction_date, account_name, type, amount,
                        payee, description, category, meta)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (external_id) DO NOTHING""",
                    (
                        external_id,
                        date_val,
                        pg_account,
                        "debit" if raw_amount > 0 else "credit",
                        float(raw_amount),
                        payee,
                        description,
                        category,
                        psycopg2.extras.Json({"source": "bean_import", "bean_account": bean_account}),
                    ),
                )
                if cur.rowcount > 0:
                    imported += 1
                else:
                    skipped += 1
            except Exception as e:
                skipped += 1
        conn.commit()
    finally:
        conn.close()
    return {"imported": imported, "skipped": skipped}


@app.get("/api/debug")
async def debug_endpoint(
    account_name: str = Query(""),
    account_type: str | None = Query(None),
    date_from: str = Query("2025-01-01"),
    date_to: str = Query("2025-01-31"),
):
    account_type = account_type or None  # empty/missing = no filter

    if account_name in ("Loading...", "No accounts found"):
        return {"error": "Invalid account selection. Please wait for accounts to load and select a valid PG account."}

    if account_type and account_type not in VALID_ACCOUNT_TYPES:
        return {"error": f"Invalid account_type '{account_type}'. Must be one of {VALID_ACCOUNT_TYPES}"}

    # Build beancount regex from (account_name, account_type), treating empty as wildcard.
    # ALWAYS keep the (Assets|Liabilities) prefix — prevents double-counting.
    if account_name and account_type:
        bean_account_regex = f"(Assets|Liabilities):{re.escape(account_type)}:{re.escape(account_name)}"
        sign_mult = _sign_for_type(account_type)
        map_warning = None
    elif account_name and not account_type:
        bean_account_regex = f"(Assets|Liabilities):.*:{re.escape(account_name)}"
        sign_mult = 1
        map_warning = "wildcard type — sign multiplier forced to +1 (no flip for CreditCard)"
    elif not account_name and account_type:
        bean_account_regex = f"(Assets|Liabilities):{re.escape(account_type)}:.*"
        sign_mult = _sign_for_type(account_type)
        map_warning = None
    else:
        bean_account_regex = r"(Assets|Liabilities):.*"
        sign_mult = 1
        map_warning = "wildcard account+type — reconciling ALL balance-sheet accounts; sign multiplier forced to +1"

    pg_rows = []
    pg_error = None
    # Build WHERE clause with optional account_name and account_type filters
    conditions = ["transaction_date >= %s", "transaction_date < %s"]
    params = [date_from, date_to]
    if account_name:
        conditions.append("account_name = %s")
        params.append(account_name)
    if account_type:
        conditions.append("type = %s")
        params.append(account_type)
    where_clause = " AND ".join(conditions)
    query = f"""
            SELECT external_id, transaction_date, payee, description, category,
                   amount, 1 as tx_count
            FROM transactions
            WHERE {where_clause}
            ORDER BY transaction_date, payee
        """
    try:
        conn = get_pg_conn()
        try:
            cur = conn.cursor()
            cur.execute(query, tuple(params))
            for row in cur.fetchall():
                clean_row = {}
                for k, v in row.items():
                    if isinstance(v, Decimal):
                        clean_row[k] = str(v)
                    elif hasattr(v, 'isoformat'):
                        clean_row[k] = v.isoformat()
                    else:
                        clean_row[k] = v
                pg_rows.append(clean_row)
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        pg_error = f"PG query failed: {e}"

    bean_rows = []
    bean_error = None
    bean_query = f"""
            SELECT date, narration, units(position).number as amount,
                   getitem(entry.meta, 'external_id') as external_id
            FROM postings
            WHERE account ~ '{bean_account_regex}'
              AND date >= {date_from} AND date < {date_to}
            ORDER BY date
        """
    try:
        bconn = get_bean_conn()
        try:
            cur = bconn.cursor()
            cur.execute(bean_query)
            for row in cur.fetchall():
                raw_amount = Decimal(str(row[2])) if row[2] is not None else Decimal("0")
                signed_amount = raw_amount * Decimal(str(sign_mult))
                ext_id = row[3]
                bean_rows.append({
                    "date": str(row[0]) if row[0] is not None else "",
                    "narration": row[1] or "",
                    "amount": str(raw_amount),
                    "signed_amount": str(signed_amount),
                    "external_id": str(ext_id) if ext_id else "",
                })
            cur.close()
        finally:
            bconn.close()
    except Exception as e:
        bean_error = f"Bean query failed: {e}"

    result = {
        "account_type": account_type,
        "pg_query": query.strip(),
        "pg_params": params,
        "pg_rows": pg_rows,
        "pg_count": len(pg_rows),
        "bean_query": bean_query.strip(),
        "bean_rows": bean_rows,
        "bean_count": len(bean_rows),
    }
    if map_warning:
        result["warning"] = map_warning
    if pg_error:
        result["pg_error"] = pg_error
    if bean_error:
        result["bean_error"] = bean_error
    return result


def _db_param(val):
    if isinstance(val, (dict, list)):
        return psycopg2.extras.Json(val)
    return val


@app.post("/api/admin/updates/preview")
async def preview_update(body: dict[str, Any]):
    set_clause = body.get("set")
    where_clause = body.get("where")

    if not isinstance(set_clause, dict) or not set_clause:
        raise HTTPException(status_code=400, detail="'set' must be a non-empty object")
    if not isinstance(where_clause, dict) or not where_clause:
        raise HTTPException(status_code=400, detail="'where' must be a non-empty object")

    invalid_set = set(set_clause.keys()) - SET_COLUMNS
    if invalid_set:
        raise HTTPException(status_code=400, detail=f"Invalid SET columns: {sorted(invalid_set)}")

    try:
        where_sql, where_params = _build_where(where_clause)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    conn = None
    try:
        conn = get_pg_conn()
        cur = conn.cursor()

        count_query = f"SELECT COUNT(*) AS total FROM transactions WHERE {where_sql}"
        cur.execute(count_query, tuple(where_params))
        total_count = cur.fetchone()["total"]

        sample_query = f"SELECT * FROM transactions WHERE {where_sql} LIMIT 100"
        cur.execute(sample_query, tuple(where_params))
        sample_rows = [_json_safe(dict(r)) for r in cur.fetchall()]

        cur.close()
        return {"count": total_count, "sample": sample_rows}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@app.post("/api/admin/updates/execute")
async def execute_update(body: dict[str, Any]):
    set_clause = body.get("set")
    where_clause = body.get("where")
    description = body.get("description", "")
    confirm_count = body.get("confirm_count")

    if not isinstance(set_clause, dict) or not set_clause:
        raise HTTPException(status_code=400, detail="'set' must be a non-empty object")
    if not isinstance(where_clause, dict) or not where_clause:
        raise HTTPException(status_code=400, detail="'where' must be a non-empty object")

    invalid_set = set(set_clause.keys()) - SET_COLUMNS
    if invalid_set:
        raise HTTPException(status_code=400, detail=f"Invalid SET columns: {sorted(invalid_set)}")

    try:
        where_sql, where_params = _build_where(where_clause)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    conn = None
    try:
        conn = get_pg_conn()
        cur = conn.cursor()

        # 1. SELECT * FROM transactions WHERE <clause> FOR UPDATE -> old_rows
        lock_query = f"SELECT * FROM transactions WHERE {where_sql} FOR UPDATE"
        cur.execute(lock_query, tuple(where_params))
        old_rows = cur.fetchall()

        # 2. If zero rows: rollback, return 400 "no rows match"
        if not old_rows:
            conn.rollback()
            raise HTTPException(status_code=400, detail="no rows match")

        # 3. If confirm_count provided and != len(old_rows): rollback, return 400 "count mismatch, expected X got Y"
        if confirm_count is not None and int(confirm_count) != len(old_rows):
            conn.rollback()
            raise HTTPException(
                status_code=400,
                detail=f"count mismatch, expected {confirm_count} got {len(old_rows)}",
            )

        # 4. Serialize old_rows with _json_safe, INSERT INTO transaction_update_history
        safe_old_rows = [_json_safe(dict(r)) for r in old_rows]
        insert_history_query = """
            INSERT INTO transaction_update_history
                (source, description, set_clause, where_clause, rows_affected, old_records)
            VALUES
                ('api', %s, %s, %s, %s, %s)
            RETURNING id
        """
        cur.execute(
            insert_history_query,
            (
                description,
                psycopg2.extras.Json(set_clause),
                psycopg2.extras.Json(where_clause),
                len(old_rows),
                psycopg2.extras.Json(safe_old_rows),
            ),
        )
        history_id = cur.fetchone()["id"]

        # 5. UPDATE transactions SET <set cols> = %s, updated_at = NOW() WHERE <clause>
        set_cols = list(set_clause.keys())
        set_parts = [f"{col} = %s" for col in set_cols]
        set_values = [_db_param(set_clause[col]) for col in set_cols]
        update_query = (
            f"UPDATE transactions SET {', '.join(set_parts)}, updated_at = NOW() "
            f"WHERE {where_sql}"
        )
        cur.execute(update_query, tuple(set_values + where_params))

        # 6. If cur.rowcount != len(old_rows): raise RuntimeError to force rollback
        if cur.rowcount != len(old_rows):
            raise RuntimeError(
                f"Rowcount mismatch: locked {len(old_rows)} rows but updated {cur.rowcount}"
            )

        # 7. COMMIT
        conn.commit()
        rows_affected = cur.rowcount
        cur.close()
        return {"history_id": history_id, "rows_affected": rows_affected}
    except HTTPException:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@app.get("/api/admin/updates/history")
async def get_update_history():
    conn = None
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, executed_at, source, description, set_clause, where_clause, rows_affected, rolled_back_at
            FROM transaction_update_history
            ORDER BY id DESC
            LIMIT 100
            """
        )
        rows = [_json_safe(dict(r)) for r in cur.fetchall()]
        cur.close()
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@app.get("/api/admin/updates/history/{id}")
async def get_update_history_entry(id: int):
    conn = None
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, executed_at, source, description, set_clause, where_clause, rows_affected, old_records, rolled_back_at
            FROM transaction_update_history
            WHERE id = %s
            """,
            (id,),
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            raise HTTPException(status_code=404, detail=f"History entry {id} not found")
        return _json_safe(dict(row))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@app.post("/api/admin/updates/rollback/{id}")
async def rollback_update(id: int):
    conn = None
    try:
        conn = get_pg_conn()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT id, old_records, rolled_back_at
            FROM transaction_update_history
            WHERE id = %s
            FOR UPDATE
            """,
            (id,),
        )
        entry = cur.fetchone()
        if not entry:
            conn.rollback()
            raise HTTPException(status_code=404, detail=f"History entry {id} not found")

        if entry["rolled_back_at"] is not None:
            conn.rollback()
            raise HTTPException(status_code=400, detail="already rolled back")

        old_records = entry["old_records"]
        if isinstance(old_records, str):
            import json
            old_records = json.loads(old_records)

        rolled_back_count = 0
        if isinstance(old_records, list):
            for rec in old_records:
                if not isinstance(rec, dict):
                    continue
                ext_id = rec.get("external_id")
                if not ext_id:
                    continue

                # All cols from record except external_id and updated_at
                restore_cols = [
                    k for k in rec.keys()
                    if k not in ("external_id", "updated_at")
                    and re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", k)
                ]
                if not restore_cols:
                    continue

                set_parts = [f"{col} = %s" for col in restore_cols]
                params = [_db_param(rec[col]) for col in restore_cols]
                params.append(ext_id)

                restore_query = (
                    f"UPDATE transactions SET {', '.join(set_parts)}, updated_at = NOW() "
                    f"WHERE external_id = %s"
                )
                cur.execute(restore_query, tuple(params))
                rolled_back_count += cur.rowcount

        cur.execute(
            "UPDATE transaction_update_history SET rolled_back_at = NOW() WHERE id = %s",
            (id,),
        )
        conn.commit()
        cur.close()
        return {"rolled_back": rolled_back_count, "history_id": id}
    except HTTPException:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


