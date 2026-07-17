# tag-governance ✨

> A modern full-stack application built with [`apx`](https://github.com/databricks-solutions/apx) 🚀

## 🛠️ Tech Stack

This application leverages a powerful, modern tech stack:

- **Backend** 🐍 Python + [FastAPI](https://fastapi.tiangolo.com/)
- **Frontend** ⚛️ React + [shadcn/ui](https://ui.shadcn.com/)
- **API Client** 🔄 Auto-generated TypeScript client from OpenAPI schema

## 🚀 Quick Start

### Development Mode

Start all development servers (backend, frontend, and OpenAPI watcher) in detached mode:

```bash
apx dev start
```

This will start an apx development server, which in it's turn runs backend, frontend and OpenAPI watcher.
All servers run in the background, with logs kept in-memory of the apx dev server.

### 📊 Monitoring & Logs

```bash
# View all logs
apx dev logs

# Stream logs in real-time
apx dev logs -f

# Check server status
apx dev status

# Stop all servers
apx dev stop
```

## ✅ Code Quality

Run type checking and linting for both TypeScript and Python:

```bash
apx dev check
```

## 📦 Build

Create a production-ready build:

```bash
apx build
```

## 🚢 Deployment

Deploy to Databricks:

```bash
databricks bundle deploy -p <your-profile>
databricks bundle run tag-governance-app -p <your-profile>   # push source to the running app
```

### ⚠️ Required one-time grants (or the dashboard shows blank $)

The app runs as its **own service principal**, not as you. That SP needs UC
read/write on the `tag_governance` schema and `CAN_USE` on the SQL warehouse,
or every query fails with a 500 and the KPIs come up empty. These objects are
pre-existing (not bundle-managed), so run the grant script once after deploy:

```bash
./grant_app_sp.sh <profile> tag-governance main.tag_governance <warehouse_id>
```

It's idempotent — safe to re-run. Requires an admin/owner identity on the profile.

> **Also**: `app.yml` must contain the `env:` block (including
> `DATABRICKS_WAREHOUSE_ID: valueFrom: sql-warehouse`). Databricks Apps reads
> env from `app.yml`, **not** from `databricks.yml`'s `config.env` — if
> `apx build` ever regenerates `app.yml` without it, the app loses its
> warehouse and the dashboard goes blank again.

---

<p align="center">Built with ❤️ using <a href="https://github.com/databricks-solutions/apx">apx</a></p>
