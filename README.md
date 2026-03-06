# Generate Cloud

Generate Cloud is a runnable MVP of the "Intelligent Collaborative Cloud Image Platform" described in `PRD.docx` and `TECH.docx`.

## What is included

- Public image gallery with keyword and tag search
- User registration, login, token authentication, and role-based access
- Personal image space with private and public uploads
- Team collaboration space with shared image libraries, member invites, and realtime activity
- Admin moderation console with pending review queue and platform metrics
- Local image storage with generated thumbnails and seeded demo data

## Stack

- Frontend: Vue 3 + TypeScript + Vite
- Backend: Spring Boot 3 + Spring Security + Spring Data JPA + WebSocket
- Database: H2 file database for local development
- Storage: local filesystem storage under `backend/data/storage`

## Local run

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
- `GET /h2-console`

## Notes

- Public uploads created by regular users enter `PENDING` moderation until approved by an admin.
- Team websocket endpoint is `ws://localhost:8080/ws/teams/{teamId}?token=...`.
- Seeded demo images are created automatically on first backend boot.
