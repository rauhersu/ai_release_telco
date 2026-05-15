# Deployment

This project was generated with the following deployment-related flags:

- ✅ Docker / `docker-compose.yml`
- ✅ Kubernetes manifests in `k8s/`
- CI: `github`
- Reverse proxy: Nginx


---


## Docker Compose (single host)

For staging or small production:

```bash
# 1. Configure
cp backend/.env.example backend/.env
# Edit backend/.env with production values (see ENV_VARS.md)

# 2. Build + start
docker compose up -d --build

# 3. Apply migrations
docker compose exec app uv run alembic upgrade head


# 4. Verify
curl http://localhost:8000/api/v1/health
# Frontend: http://localhost:3000
```

### Reverse proxy
Nginx config in `nginx/` proxies `/` → frontend, `/api` → backend, `/ws` → backend WebSocket. Update `server_name` and TLS cert paths in `nginx/conf.d/app.conf`.




## Kubernetes

Manifests in `k8s/` cover: Deployment, Service, ConfigMap, Secret stub, Ingress, optional HPA.

```bash
# Build + push images
docker build -t your-registry/ai_telco_release-backend:latest backend/
docker build -t your-registry/ai_telco_release-frontend:latest frontend/
docker push your-registry/ai_telco_release-backend:latest

# Update image tags in k8s/deployment.yaml, then:
kubectl create namespace ai_telco_release
kubectl -n ai_telco_release create secret generic app-secrets --from-env-file=backend/.env
kubectl apply -n ai_telco_release -f k8s/

# Migrations as a Job
kubectl -n ai_telco_release apply -f k8s/migration-job.yaml
```

### Tuning

- **Replicas:** edit `k8s/deployment.yaml`. Backend is async — start with 2 replicas.
- **HPA:** if `k8s/hpa.yaml` is present, scales on CPU. Adjust thresholds.
- **Resources:** request/limit set conservatively. Scale up RAM if you process large files or have many concurrent RAG queries.


## Platform-specific quickstarts

### Fly.io

```bash
fly launch --name ai_telco_release-backend --region waw
fly postgres create --name ai_telco_release-db
fly postgres attach ai_telco_release-db
# Redis: use Upstash (`fly redis create`) or Fly's Tigris
fly secrets set $(cat backend/.env | grep -v '^#' | xargs)
fly deploy
```

### Railway

1. Connect repo, pick Dockerfile-based deploy.
2. Add env vars from `backend/.env` to Railway service.
3. Provision PostgreSQL plugin → `DATABASE_URL` auto-injected.
4. Provision Redis plugin → `REDIS_URL` auto-injected.
5. Deploy.

### Render

1. Create Web Service → docker, point at `backend/Dockerfile`.
2. Create Static Site for frontend (build cmd: `bun install && bun run build`, output dir: `.next`).
3. Create PostgreSQL → copy DATABASE_URL.
4. Add env vars; deploy.

### Vercel (frontend only)

The frontend is a Next.js app — works on Vercel out of the box.

```bash
cd frontend
vercel
```

Set `BACKEND_URL` and `NEXT_PUBLIC_API_URL` env vars in Vercel dashboard pointing to your backend host.


---

## Environment validation in production

Before promoting to prod, run:

```bash
docker compose exec app uv run python -c "from app.core.config import settings; print('OK')"
```

Catches missing required env vars early. See `ENV_VARS.md` for the full list.

## Post-deploy checks

- [ ] `/api/v1/health` returns `{"status": "ok"}`
- [ ] `alembic current` matches expected revision
- [ ] Frontend renders, login flow works end-to-end
- [ ] Logs flowing to your aggregator + Sentry capturing errors + Logfire receiving traces
- [ ] Reverse proxy enforces HTTPS

## Rollback

- **Schema:** `alembic downgrade -1` rolls back one migration. Test on staging first.
- **Code:** redeploy previous image tag. Pin tags (`v1.2.3`), never deploy `latest` to prod.
- **Data:** restore from your most recent backup; verify `alembic current` matches the data version.
