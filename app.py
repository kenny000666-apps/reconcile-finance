"""PG vs Beancount Reconciliation Web App."""
import csv
import io
import os
from decimal import Decimal
from typing import Any

import beanquery
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, StreamingResponse
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
ACCOUNT_MAP = {
    "AmericanExpress": ("Liabilities:CreditCard:AmericanExpress", -1),
    "BarclaysPersonal": ("Liabilities:CreditCard:BarclaysPersonal", -1),
    "HalifaxPersonal": ("Liabilities:CreditCard:HalifaxPersonal", -1),
    "Revolut": ("Assets:Current:Revolut", 1),
    "ZopaBank": ("Assets:Current:ZopaBank", 1),
    "Discover": ("Liabilities:CreditCard:Discover", -1),
}


def get_pg_conn():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASSWORD,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def get_bean_conn():
    return beanquery.connect(f"beancount:///{BEAN_PATH}")


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html") as f:
        return HTMLResponse(content=f.read())


@app.get("/api/accounts")
async def get_accounts():
    pg_accounts = []
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT account_name FROM transactions ORDER BY account_name")
        pg_accounts = [row["account_name"] for row in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception as e:
        return {"error": f"PG error: {e}", "pg_accounts": [], "bean_accounts": [], "mappings": {}}

    bean_accounts = []
    try:
        bconn = get_bean_conn()
        cur = bconn.cursor()
        cur.execute("SELECT DISTINCT account FROM postings WHERE account ~ 'Liabilities:CreditCard:' ORDER BY account")
        bean_accounts = [row[0] for row in cur.fetchall()]
        cur.close()
        bconn.close()
    except Exception as e:
        return {"error": f"Bean error: {e}", "pg_accounts": pg_accounts, "bean_accounts": [], "mappings": {}}

    return {
        "pg_accounts": pg_accounts,
        "bean_accounts": bean_accounts,
        "mappings": ACCOUNT_MAP,
    }


@app.post("/api/reconcile")
async def reconcile(body: dict[str, Any]):
    date_from = body.get("date_from", "2025-01-01")
    date_to = body.get("date_to", "2025-01-31")
    pg_account = body.get("pg_account", "BarclaysPersonal")
    columns = body.get("columns", ["date", "payee", "amount"])
    group_by = body.get("group_by", "date,payee")

    mapping = ACCOUNT_MAP.get(pg_account)
    if not mapping:
        return {"error": f"No mapping for PG account: {pg_account}"}
    bean_account_regex, sign_mult = mapping

    # Column mappings
    col_map = {"date": "transaction_date", "account": "account_name", "amount": "amount",
               "payee": "payee", "description": "description", "category": "category"}
    bean_col_map = {"date": "date", "payee": "payee", "description": "narration", "account": "account"}

    # Grouping columns exclude 'amount' — amount is a comparison value, not a key dimension
    group_cols = [c for c in columns if c != "amount"]
    if not group_cols:
        group_cols = ["date"]

    # Phase 1: Query PG with external_id for primary matching
    pg_by_extid = {}   # external_id -> row data
    pg_by_key = {}     # composite key -> row data (fallback)
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        mapped_group_cols = [col_map.get(c, c) for c in group_cols]
        select_parts = list(mapped_group_cols) + ["SUM(amount) as total_amount", "COUNT(*) as tx_count", "array_agg(external_id) as ext_ids"]
        select_clause = ", ".join(select_parts)
        group_clause = ", ".join(mapped_group_cols)
        query = f"""
            SELECT {select_clause}
            FROM transactions
            WHERE account_name = %s AND transaction_date >= %s AND transaction_date < %s
            GROUP BY {group_clause}
            ORDER BY {group_clause}
        """
        cur.execute(query, (pg_account, date_from, date_to))
        for row in cur.fetchall():
            key = tuple(str(row[col_map.get(c, c)]) for c in group_cols)
            amount = Decimal(str(row["total_amount"]))
            entry = {
                "key_dict": dict(zip(group_cols, key)),
                "amount": amount,
                "count": row["tx_count"],
                "ext_ids": row["ext_ids"] or [],
            }
            # Index by first non-null external_id for primary matching
            for eid in (row["ext_ids"] or []):
                if eid:
                    pg_by_extid[str(eid)] = entry
                    break
            pg_by_key[key] = entry
        cur.close()
        conn.close()
    except Exception as e:
        return {"error": f"PG query failed: {e}", "results": []}

    # Phase 2: Query Bean
    bean_by_key = {}
    try:
        bconn = get_bean_conn()
        cur = bconn.cursor()
        bean_group_cols = [bean_col_map.get(c, c) for c in group_cols]
        bean_select = ', '.join(bean_group_cols) if bean_group_cols else 'date'
        bean_query = f"""
            SELECT {bean_select}, sum(units(position).number) as total_amount, count(*) as tx_count
            FROM postings
            WHERE account ~ '{bean_account_regex}'
              AND date >= {date_from} AND date < {date_to}
            GROUP BY {bean_select}
            ORDER BY {bean_select}
        """
        cur.execute(bean_query)
        for row in cur.fetchall():
            key = tuple(str(row[i]) for i in range(len(bean_group_cols)))
            raw_amount = Decimal(str(row[-2]))
            bean_by_key[key] = {
                "key_dict": dict(zip(group_cols, key)),
                "amount": raw_amount * sign_mult,
                "count": row[-1],
            }
        cur.close()
        bconn.close()
    except Exception as e:
        return {"error": f"Bean query failed: {e}", "results": []}

    # Phase 3: Two-pass matching
    matched_pg_keys = set()
    matched_bean_keys = set()
    results = []

    # Pass A: Match by external_id (if PG has ext_ids that map to bean keys)
    # For now, external_id matching is reserved for when bean side also carries IDs.
    # Since beanquery doesn't easily expose metadata in GROUP BY, we skip to Pass B.

    # Pass B: Match by composite key (group columns only, no amount in key)
    all_keys = set(pg_by_key.keys()) | set(bean_by_key.keys())
    for key in sorted(all_keys):
        pg = pg_by_key.get(key)
        bean = bean_by_key.get(key)
        key_dict = (pg or bean)["key_dict"]
        if pg and bean:
            diff = pg["amount"] - bean["amount"]
            status = "MATCH" if diff == 0 else "MISMATCH"
            results.append({
                "key": key_dict,
                "pg_amount": str(pg["amount"]),
                "bean_amount": str(bean["amount"]),
                "diff": str(diff),
                "pg_count": pg["count"],
                "bean_count": bean["count"],
                "status": status,
            })
            matched_pg_keys.add(key)
            matched_bean_keys.add(key)
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

    summary = {
        "total": len(results),
        "match": sum(1 for r in results if r["status"] == "MATCH"),
        "mismatch": sum(1 for r in results if r["status"] == "MISMATCH"),
        "pg_only": sum(1 for r in results if r["status"] == "PG_ONLY"),
        "bean_only": sum(1 for r in results if r["status"] == "BEAN_ONLY"),
    }

    return {"summary": summary, "results": results}


@app.get("/api/export")
async def export_csv(
    date_from: str = Query("2025-01-01"),
    date_to: str = Query("2025-01-31"),
    pg_account: str = Query("BarclaysPersonal"),
):
    # Re-run reconciliation to get fresh data
    body = {
        "date_from": date_from,
        "date_to": date_to,
        "pg_account": pg_account,
        "columns": ["date", "payee", "amount"],
        "group_by": "date,payee",
    }
    result = await reconcile(body)
    if "error" in result:
        return {"error": result["error"]}

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Payee", "PG Amount", "Bean Amount", "Diff", "Status"])
    for r in result["results"]:
        key = r["key"]
        writer.writerow([
            key.get("date", ""),
            key.get("payee", ""),
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
