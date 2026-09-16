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
