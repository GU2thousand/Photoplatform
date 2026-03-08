# Generate Cloud

> A prototype cloud image platform for public browsing, personal asset management, team collaboration, and admin moderation.

Generate Cloud is a runnable MVP of the **Intelligent Collaborative Cloud Image Platform** described in `PRD.docx` and `TECH.docx`.

It focuses on validating the core workflow of a modern image platform:

**authentication → upload → storage → gallery display → collaboration → moderation**

---

## Project Purpose

Generate Cloud is built to explore how a cloud image platform can support both **individual users** and **collaborative teams** in a single product.

The current prototype includes:

- **Public Gallery** for browsing public images with keyword and tag search
- **Personal Space** for managing private and public uploads
- **Team Space** for shared image libraries, member invites, and realtime activity
- **Admin Console** for content review and platform statistics
- **Persistent Media Storage** using PostgreSQL and S3-compatible object storage

The current focus is on proving the **main product flow** and **system integration**, rather than shipping a production-ready platform.

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

### Supported Storage Providers
- AWS S3
- Cloudflare R2
- MinIO
- Google Cloud Storage with S3 interoperability

---

## System Architecture

```text
Frontend (Vue 3 + Vite)
        |
        | HTTP / WebSocket
        v
Backend API (Spring Boot)
        |
        +--> PostgreSQL
        |
        +--> S3-compatible Object Storage
                |
                +--> Original image files
                +--> Generated thumbnails
