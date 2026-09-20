[English](#english) | [简体中文](#简体中文)

<a id="english"></a>

# English

# Generate Cloud
<img width="1625" height="970" alt="52101772998852_ pic" src="https://github.com/user-attachments/assets/07e34505-dbe4-4a7c-9bbb-a300c0a678e3" />
<img width="1483" height="787" alt="52111772998874_ pic" src="https://github.com/user-attachments/assets/47cd7ce4-16b3-4cde-a52e-eacec9b0ffdc" />

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
```

---

## Run and verify

For a local demo with PostgreSQL and MinIO, run `docker compose up -d --build`, then open [http://localhost:5173](http://localhost:5173). The Compose ports bind only to localhost. It explicitly enables seeded demo users; the backend otherwise disables seeding by default. Demo sign-in: `avery@generatecloud.local / creator123`; admin: `admin@generatecloud.local / admin123`. Do not publish these demo credentials or this Compose configuration as a production service.

```bash
# Backend behavior and security regressions (Java 17+)
cd backend
./gradlew test

# Frontend type checking, production build, and dependency audit (Node 22+)
cd ../frontend
npm ci
npm run build
npm test
npm audit --audit-level=high
```

CI runs these checks for pushes and pull requests. `docker compose stop` preserves database and image volumes; `docker compose down -v` deletes them.

## API and reliability notes

- API authentication uses `Authorization: Bearer <access-token>`. Login tokens are not accepted from image URLs or cookies. The frontend loads protected media using authenticated requests and releases its temporary object URLs on logout or component cleanup.
- `GET /api/public/images?page=0&size=24&query=&tag=` returns `{items,page,size,totalElements,totalPages}`. Search and pagination run in the database. Image author objects contain only `id` and `name`; account email and role are not published in gallery responses.
- Only approved public media receives public caching. Private, team, and pending media uses a private, non-storable response.
- Team clients first request `POST /api/teams/{id}/socket-ticket` with their access token, then connect to `/ws/teams/{id}?ticket=...`. Tickets last 60 seconds and only authorize that team connection; they cannot authenticate ordinary API requests. Heartbeats and frontend reconnection maintain idle connections.
- Image deletion schedules durable object cleanup so original and thumbnail deletion can retry after a storage outage. Database backups must include cleanup jobs, and media backups must include both object prefixes.

## Deployment

`render.yaml` defines the API, static frontend, and PostgreSQL services. The API enables the `prod` profile, generates its JWT secret, and disables demo seeding. Configure the S3-compatible bucket, endpoint, region, access credentials, frontend `VITE_API_BASE_URL`, and exact backend `APP_CORS_ALLOWED_ORIGINS` for your deployment. Never put storage secrets in frontend environment variables.

Before public use, configure HTTPS, private bucket access, database and object-store backups with a tested restore procedure, external health monitoring at `/actuator/health`, and log retention. Provision an administrator deliberately after registering an account; do not re-enable demo seed accounts to obtain admin access. Cross-origin hosting and provider-specific backup/restore must be verified in the actual deployment environment.

---

<a id="简体中文"></a>

# 简体中文

# Generate Cloud

<img width="1625" height="970" alt="52101772998852_ pic" src="https://github.com/user-attachments/assets/07e34505-dbe4-4a7c-9bbb-a300c0a678e3" />
<img width="1483" height="787" alt="52111772998874_ pic" src="https://github.com/user-attachments/assets/47cd7ce4-16b3-4cde-a52e-eacec9b0ffdc" />

> 支持公开浏览、个人素材管理、团队协作和管理员审核的云图片平台原型。

Generate Cloud 是 `PRD.docx` 和 `TECH.docx` 所描述的**智能协作云图片平台**的可运行 MVP。

项目重点验证现代图片平台的核心工作流：

**身份验证 → 上传 → 存储 → 图库展示 → 协作 → 审核**

---

## 项目目标

Generate Cloud 探索如何在同一产品中支持**个人用户**和**协作团队**。

当前原型包括：

- **公共图库**：浏览公开图片，支持关键词和标签搜索
- **个人空间**：管理私密和公开上传的图片
- **团队空间**：共享图片库、成员邀请和实时动态
- **管理控制台**：内容审核和平台统计
- **持久化媒体存储**：使用 PostgreSQL 和兼容 S3 的对象存储

当前重点是验证**主要产品流程**与**系统集成**，尚非可直接投入生产的平台。

---

## 技术栈

### 前端

- Vue 3
- TypeScript
- Vite

### 后端

- Spring Boot 3
- Spring Security
- Spring Data JPA
- WebSocket

### 基础设施

- PostgreSQL
- 兼容 S3 的对象存储
- Docker Compose

### 支持的存储服务

- AWS S3
- Cloudflare R2
- MinIO
- 使用 S3 互操作功能的 Google Cloud Storage

---

## 系统架构

```text
前端 (Vue 3 + Vite)
        |
        | HTTP / WebSocket
        v
后端 API (Spring Boot)
        |
        +--> PostgreSQL
        |
        +--> 兼容 S3 的对象存储
                |
                +--> 原始图片文件
                +--> 生成的缩略图
```


## 运行与验证

执行 `docker compose up -d --build`，然后访问 [http://localhost:5173](http://localhost:5173)，即可在本机运行带 PostgreSQL 和 MinIO 的完整演示。Compose 端口仅绑定 localhost，并显式启用演示账号；后端默认关闭种子数据。普通账号为 `avery@generatecloud.local / creator123`，管理员为 `admin@generatecloud.local / admin123`。不要将演示账号和本机 Compose 配置直接用于公网服务。

Java 17+ 下在 `backend/` 执行 `./gradlew test`；Node 22+ 下在 `frontend/` 依次执行 `npm ci`、`npm run build`、`npm test`、`npm audit --audit-level=high`。CI 在推送和 PR 上运行这些检查。`docker compose stop` 保留数据库和图片卷，`docker compose down -v` 会删除数据卷。

## API 与可靠性

- API 使用 `Authorization: Bearer <access-token>`。图片 URL 与 cookie 不再接受完整登录 token；前端通过认证请求加载私密媒体，并在退出或组件销毁时释放临时 object URL。
- `GET /api/public/images?page=0&size=24&query=&tag=` 返回 `{items,page,size,totalElements,totalPages}`，搜索和分页在数据库中执行。图片作者仅包含 `id` 和 `name`，不公开邮箱及角色。
- 仅已审核的公开图片允许公开缓存；私密、团队和待审核图片使用禁止存储的私密缓存策略。
- 团队客户端先携带登录 token 请求 `POST /api/teams/{id}/socket-ticket`，再通过 `/ws/teams/{id}?ticket=...` 建立连接。票据有效期为 60 秒，仅允许对应团队连接，无法认证普通 API。心跳和前端重连维护空闲连接。
- 删除图片使用持久化清理任务，在存储故障后重试删除原图与缩略图。数据库备份须包含清理任务；媒体备份须覆盖两个对象前缀。

## 部署

`render.yaml` 定义后端、静态前端与 PostgreSQL，启用后端 `prod` 配置、生成 JWT 密钥并关闭演示种子。根据部署填写 S3 兼容存储的 bucket、endpoint、region 和访问凭据，设置前端 `VITE_API_BASE_URL` 及后端精确的 `APP_CORS_ALLOWED_ORIGINS`。存储凭据不能放入前端环境变量。

公网使用前配置 HTTPS、私有 bucket、经过恢复演练的数据库与对象存储备份、`/actuator/health` 外部监控及日志保留。注册账号后通过受控流程设置管理员，不要通过重新启用演示种子取得管理员权限。跨域部署和服务商备份恢复需要在实际环境中单独验证。
