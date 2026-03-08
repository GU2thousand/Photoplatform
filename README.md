# Generate Cloud

Generate Cloud is a runnable MVP of the "Intelligent Collaborative Cloud Image Platform" described in `PRD.docx` and `TECH.docx`.

## What is included

- Public image gallery with keyword and tag search
- User registration, login, token authentication, and role-based access
- Personal image space with private and public uploads
- Team collaboration space with shared image libraries, member invites, and realtime activity
- Admin moderation console with pending review queue and platform metrics
- PostgreSQL persistence and S3-compatible object storage with generated thumbnails and seeded demo data

## Stack

- Frontend: Vue 3 + TypeScript + Vite
- Backend: Spring Boot 3 + Spring Security + Spring Data JPA + WebSocket
- Database: PostgreSQL
- Storage: S3-compatible object storage such as AWS S3, Cloudflare R2, MinIO, or Google Cloud Storage S3 interoperability

## Local run

### Infrastructure

```bash
cd /Users/guerqian77/Desktop/generateCloud
cp .env.example .env
docker compose up -d
```

This now starts the full local stack:

- PostgreSQL on `localhost:5432`
- MinIO object storage on `localhost:9000`
- MinIO console on [http://localhost:9001](http://localhost:9001)
- Backend API on [http://localhost:8081](http://localhost:8081)
- Frontend on [http://localhost:5173](http://localhost:5173)

The frontend container proxies `/api` and `/ws` to the backend container, so browser interaction works from a single local URL.

### Stop local stack

```bash
cd /Users/guerqian77/Desktop/generateCloud
docker compose down
```

### Rebuild after code changes

```bash
cd /Users/guerqian77/Desktop/generateCloud
docker compose up -d --build
```

## Render deployment

This repository now includes Render deployment files:

- [render.yaml](/Users/guerqian77/Desktop/generateCloud/render.yaml) for the Blueprint
- [backend/Dockerfile](/Users/guerqian77/Desktop/generateCloud/backend/Dockerfile) for the Spring Boot API container

Recommended production wiring on Render:

- `photoplatform-api`: Render Web Service
- `photoplatform-web`: Render Static Site
- `photoplatform-db`: Render Postgres
- Object storage: external S3-compatible bucket such as AWS S3, Cloudflare R2, or Google Cloud Storage S3 interoperability

Important deployment notes:

- `VITE_API_BASE_URL` must be set to the public HTTPS URL of `photoplatform-api`.
- `APP_CORS_ALLOWED_ORIGINS` must include the public URL of `photoplatform-web`.
- `DATABASE_URL` from Render Postgres is supported directly, including non-JDBC Postgres URLs.
- Storage secrets in `render.yaml` use `sync: false`, so Render will ask for them only during the initial Blueprint creation flow.

## Configuration

Backend reads these environment variables:

- `SPRING_DATASOURCE_URL`
- `SPRING_DATASOURCE_USERNAME`
- `SPRING_DATASOURCE_PASSWORD`
- `STORAGE_PROVIDER`
- `STORAGE_BUCKET`
- `STORAGE_REGION`
- `STORAGE_ENDPOINT`
- `STORAGE_ACCESS_KEY`
- `STORAGE_SECRET_KEY`
- `STORAGE_PATH_STYLE_ACCESS`
- `STORAGE_AUTO_CREATE_BUCKET`
- `STORAGE_PREFIX`
- `APP_SEED_ENABLED`

Provider notes:

- AWS S3: leave `STORAGE_ENDPOINT` empty and set your S3 bucket, region, access key, and secret key.
- Cloudflare R2: use your R2 S3 endpoint and credentials.
- Google Cloud Storage: use S3 interoperability keys and `https://storage.googleapis.com` as the endpoint.

## Demo accounts

- Admin: `admin@generatecloud.local` / `admin123`
- Creator: `avery@generatecloud.local` / `creator123`
- Team member: `sam@generatecloud.local` / `team123`

## Useful endpoints

- `GET /api/public/images`
- `POST /api/auth/login`
- `GET /api/images/me`
- `GET /api/teams`
- `GET /api/admin/stats`

## How to verify locally

After `docker compose up -d`, verify in this order:

1. Open [http://localhost:5173](http://localhost:5173). The landing page should load instead of a blank page or proxy error.
2. Sign in with `avery@generatecloud.local / creator123` and confirm "My Space" and "Team Space" appear.
3. Open "Public Gallery" and confirm at least one seeded public image is visible.
4. Open "Team Space", select `Atlas Studio`, and confirm the realtime feed shows as connected.
5. Sign in as `admin@generatecloud.local / admin123`, open "Admin", and confirm there is a pending image waiting for moderation.
6. Optionally check raw health endpoints:

```bash
curl http://localhost:8081/actuator/health
curl http://localhost:8081/api/public/summary
curl -I http://localhost:8081/api/files/1/thumbnail
docker compose ps
```

Expected results:

- `/actuator/health` returns `UP`
- `/api/public/summary` returns JSON counts
- `/api/files/1/thumbnail` returns `HTTP 200`
- `docker compose ps` shows all four services as running

## Notes

- Public uploads created by regular users enter `PENDING` moderation until approved by an admin.
- Team websocket endpoint is `ws://localhost:8080/ws/teams/{teamId}?token=...`.
- Seeded demo images are created automatically on first backend boot.
- For local deployment the full stack is defined in [compose.yaml](/Users/guerqian77/Desktop/generateCloud/compose.yaml).
