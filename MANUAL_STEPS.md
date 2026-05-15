# Manual setup steps for ai_telco_release

The generator created the code. These are the **one-time external setup steps**
that can't be automated — accounts to create, keys to copy, services to provision.

> Skip ahead to "After every deploy" at the bottom for things you'll re-do
> regularly. Items above are one-time per environment.

---

## Secrets

```bash
cp backend/.env.example backend/.env
```

Then in `backend/.env`:

- [ ] **`SECRET_KEY`** — replace with a fresh value: `openssl rand -hex 32`
- [ ] **`API_KEY`** — replace with a fresh value: `openssl rand -hex 32`

These are used to sign JWTs and authenticate service-to-service calls. Rotate at every environment promotion (dev → staging → prod each get their own).


## PostgreSQL

- [ ] Provision a PostgreSQL ≥ 14 instance (local: `docker compose up -d db`; managed: Neon / Supabase / RDS / Cloud SQL).
- [ ] Set `DATABASE_URL` in `.env` to the **async** connection string: `postgresql+asyncpg://user:pass@host:5432/dbname`.
- [ ] Run migrations: `cd backend && uv run alembic upgrade head`.

## Anthropic

- [ ] Create API key at https://console.anthropic.com/.
- [ ] Set `ANTHROPIC_API_KEY` in `.env`.

## RAG (milvus)

- [ ] Local: `docker compose up -d milvus etcd minio` (already in `docker-compose.yml`).
- [ ] Cloud: provision via Zilliz Cloud, set `MILVUS_URI` + `MILVUS_TOKEN`.

- [ ] (Optional) Ingest seed documents: `uv run ai_telco_release rag-ingest /path/to/file.pdf --collection docs`.

### Google Drive sync source

- [ ] Create a service account at https://console.cloud.google.com/iam-admin/serviceaccounts.
- [ ] Download the JSON credentials → save to `secrets/gdrive-service-account.json`.
- [ ] Share the target Drive folder with the service-account email.
- [ ] Set `GOOGLE_DRIVE_CREDENTIALS_FILE` in `.env`.

## Redis

- [ ] Local: `docker compose up -d redis` (already in compose file).
- [ ] Managed: Upstash / Redis Cloud / ElastiCache. Set `REDIS_URL` in `.env`.

## Sentry

- [ ] Create project at https://sentry.io/.
- [ ] Copy DSN → set `SENTRY_DSN` in `.env`.
- [ ] (Optional) Configure release tracking in CI by setting `SENTRY_RELEASE` to git SHA before deploy.

## Logfire (Pydantic observability)

- [ ] Create account at https://logfire.pydantic.dev.
- [ ] Run `uv run logfire auth` once locally to bootstrap.
- [ ] Get write token → set `LOGFIRE_TOKEN` in `.env` for non-local environments.

## LangSmith

- [ ] Create account at https://smith.langchain.com.
- [ ] Get API key → set `LANGSMITH_API_KEY` + `LANGSMITH_PROJECT=ai_telco_release` in `.env`.

## Kubernetes deploy

- [ ] Build + push images: see `docs/deploy.md` → Kubernetes section.
- [ ] Create cluster secret from `.env`: `kubectl create secret generic app-secrets --from-env-file=backend/.env`.
- [ ] Update image tags in `k8s/deployment.yaml`.
- [ ] Apply: `kubectl apply -f k8s/`.

---

## After every deploy

- [ ] Run database migrations: `alembic upgrade head` (CI step or post-deploy job).
- [ ] Smoke test `/api/v1/health` returns `{"status": "ok"}`.
- [ ] Frontend loads, login → dashboard flow works.
- [ ] Logs flowing to your aggregator.

---

## Where to find more

- `ENV_VARS.md` — exhaustive env var reference
- `docs/deploy.md` — platform-specific deployment recipes
- `SECURITY.md` — security model + production hardening checklist
- `CONTRIBUTING.md` — dev environment setup
- `docs/architecture.md` — codebase layered architecture rules
