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

This starts:

- PostgreSQL on `localhost:5432`
- MinIO object storage on `localhost:9000`
- MinIO console on `http://localhost:9001`

### Backend

```bash
cd /Users/guerqian77/Desktop/generateCloud/backend
./gradlew bootRun
```

Backend runs on [http://localhost:8080](http://localhost:8080).

### Frontend

```bash
cd /Users/guerqian77/Desktop/generateCloud/frontend
npm install --cache .npm-cache
npm run dev
```

Frontend runs on [http://localhost:5173](http://localhost:5173) and proxies API and WebSocket traffic to the backend.

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

## Notes

- Public uploads created by regular users enter `PENDING` moderation until approved by an admin.
- Team websocket endpoint is `ws://localhost:8080/ws/teams/{teamId}?token=...`.
- Seeded demo images are created automatically on first backend boot.
- For local development the backend expects PostgreSQL and MinIO from `compose.yaml`.
