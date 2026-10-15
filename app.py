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

    # Query PG
    pg_rows = {}
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        # Map frontend column names to actual PG column names
        col_map = {"date": "transaction_date", "account": "account_name", "amount": "amount", "payee": "payee", "description": "description", "category": "category"}
        mapped_cols = [col_map.get(c, c) for c in columns]
        select_cols = ", ".join(mapped_cols)
        query = f"""
            SELECT {select_cols}, SUM(amount) as total_amount, COUNT(*) as tx_count
            FROM transactions
            WHERE account_name = %s AND transaction_date >= %s AND transaction_date < %s
            GROUP BY {select_cols}
            ORDER BY {select_cols}
        """
        cur.execute(query, (pg_account, date_from, date_to))
        for row in cur.fetchall():
            key = tuple(str(row[col_map.get(c, c)]) for c in columns)
            pg_rows[key] = {
                "amount": Decimal(str(row["total_amount"])),
                "count": row["tx_count"],
                "source": "pg",
            }
        cur.close()
        conn.close()
    except Exception as e:
        return {"error": f"PG query failed: {e}", "results": []}

    # Query Bean
    bean_rows = {}
    try:
        bconn = get_bean_conn()
        cur = bconn.cursor()
        # Map frontend column names to BQL equivalents; 'amount' is computed, not a column
        bean_col_map = {"date": "date", "payee": "payee", "description": "narration", "account": "account"}
        bean_group_cols = [bean_col_map.get(c, c) for c in columns if c != "amount"]
        bean_select = ', '.join(bean_group_cols) if bean_group_cols else 'date'
        # Build equivalent BQL
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
            # Key uses only the group columns (excludes amount/count)
            key = tuple(str(row[i]) for i in range(len(bean_group_cols)))
            raw_amount = Decimal(str(row[-2]))
            bean_rows[key] = {
                "amount": raw_amount * sign_mult,
                "count": row[-1],
                "source": "bean",
            }
        cur.close()
        bconn.close()
    except Exception as e:
        return {"error": f"Bean query failed: {e}", "results": []}

    # Compare
    all_keys = set(pg_rows.keys()) | set(bean_rows.keys())
    results = []
    for key in sorted(all_keys):
        pg = pg_rows.get(key)
        bean = bean_rows.get(key)
        if pg and bean:
            diff = pg["amount"] - bean["amount"]
            status = "MATCH" if diff == 0 else "MISMATCH"
            results.append({
                "key": dict(zip(columns, key)),
                "pg_amount": str(pg["amount"]),
                "bean_amount": str(bean["amount"]),
                "diff": str(diff),
                "pg_count": pg["count"],
                "bean_count": bean["count"],
                "status": status,
            })
        elif pg:
            results.append({
                "key": dict(zip(columns, key)),
                "pg_amount": str(pg["amount"]),
                "bean_amount": None,
                "diff": str(pg["amount"]),
                "pg_count": pg["count"],
                "bean_count": 0,
                "status": "PG_ONLY",
            })
        else:
            results.append({
                "key": dict(zip(columns, key)),
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
