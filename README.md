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
- **Image**: `ghcr.io/kenny000666-apps/reconcile-finance:latest`

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
| `BEAN_PATH` | `/data/beancount/main.bean` | Path to Beancount ledger |
