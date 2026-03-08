# Generate Cloud

> An intelligent collaborative cloud image platform with public galleries, personal spaces, team workspaces, and admin moderation.

Generate Cloud is a runnable MVP of the **Intelligent Collaborative Cloud Image Platform** described in `PRD.docx` and `TECH.docx`.

It is designed to demonstrate a full-stack image platform with:

- public image discovery
- private user uploads
- collaborative team libraries
- realtime activity updates
- admin moderation workflows
- persistent storage with PostgreSQL and S3-compatible object storage

---

## Overview

Generate Cloud combines the experience of a modern cloud gallery with collaboration and platform governance features.

### Core capabilities

- **Public Gallery**  
  Browse public images with keyword and tag search.

- **Personal Space**  
  Upload and manage private or public images in your own library.

- **Team Space**  
  Collaborate in shared image libraries, invite members, and view realtime team activity.

- **Admin Console**  
  Review pending uploads, moderate platform content, and inspect platform metrics.

- **Persistent Media Storage**  
  Store original files in S3-compatible object storage and generate thumbnails automatically.

---

## Tech Stack

### Frontend
- Vue 3
- TypeScript
- Vite

### Backend
- Spring Boot 3
- Spring Security
- Spring Data JPA
- WebSocket

### Infrastructure
- PostgreSQL
- S3-compatible object storage
- Docker Compose

### Supported storage providers
- AWS S3
- Cloudflare R2
- MinIO
- Google Cloud Storage with S3 interoperability

---

## Architecture

```text
Frontend (Vue 3 + Vite)
        |
        |  HTTP / WebSocket
        v
Backend API (Spring Boot)
        |
        +--> PostgreSQL
        |
        +--> S3-compatible Object Storage
                |
                +--> Original files
                +--> Generated thumbnails
