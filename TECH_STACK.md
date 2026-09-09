# Xerox Upload System - Complete Technical Stack

> End-to-end technical documentation of every technology, library, service, and architecture choice used in the project.

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Backend Stack](#backend-stack)
3. [Frontend Stack](#frontend-stack)
4. [Database](#database)
5. [File Storage](#file-storage)
6. [Authentication and Security](#authentication-and-security)
7. [Payment Integration](#payment-integration)
8. [Email Notifications](#email-notifications)
9. [PDF and File Processing](#pdf-and-file-processing)
10. [Background Jobs and Scheduling](#background-jobs-and-scheduling)
11. [Deployment and Infrastructure](#deployment-and-infrastructure)
12. [Testing](#testing)
13. [Utility Scripts](#utility-scripts)
14. [Project File Map](#project-file-map)

---

## System Overview

**Xerox Upload** is a full-stack web application that enables customers to upload documents, pay for printing online, and receive a token number for pickup at a physical xerox shop. The staff manages print jobs via a real-time dashboard.

**Architecture**: Monolithic Python backend with static HTML/CSS/JS frontend, PostgreSQL database, and cloud file storage.

**Core Flow**:
```
Customer uploads file --> Stored in Supabase/Google Drive --> Payment via Cashfree --> 
Token assigned --> Staff downloads and prints --> Customer collects with token
```

---

## Backend Stack

### Web Framework

| Technology | Version | Purpose |
|---|---|---|
| **FastAPI** | >= 0.100.0 | Async Python web framework for REST APIs and page serving |
| **Uvicorn** | >= 0.22.0 | ASGI server (runs FastAPI in production) |
| **Gunicorn** | >= 21.0.0 | Process manager for Uvicorn workers in production |

- FastAPI serves both the REST API and the frontend HTML pages
- Routes are defined in `backend/app.py` (2978 lines, the main application file)
- ASGI-based with async support for non-blocking I/O

### Core Python Libraries

| Library | Version | Purpose |
|---|---|---|
| **Pydantic Settings** | >= 2.0.0 | Configuration management via environment variables |
| **python-dotenv** | >= 1.0.0 | `.env` file loading for local development |
| **python-multipart** | >= 0.0.6 | Multipart form data parsing for file uploads |
| **aiofiles** | >= 23.0.0 | Async file I/O operations |
| **SlowAPI** | >= 0.1.8 | Rate limiting middleware (per-IP throttling) |

### Backend Module Map

| Module | File | Lines | Purpose |
|---|---|---|---|
| Main Application | `backend/app.py` | 2978 | API endpoints, routing, page serving, upload/download logic |
| Configuration | `backend/config.py` | 107 | Environment-based settings via Pydantic BaseSettings |
| Database | `backend/db.py` | 1334 | PostgreSQL connection pool, CRUD, migrations, ticket generation |
| Authentication | `backend/auth.py` | 267 | JWT tokens, password hashing, Google OAuth, user management |
| File Storage | `backend/storage.py` | 378 | Supabase Storage upload/download/delete operations |
| Google Drive | `backend/google_drive.py` | 548 | OAuth2 Drive API, file upload/download/streaming |
| Payments | `backend/cashfree.py` | 345 | Cashfree payment gateway integration |
| Notifications | `backend/notifications.py` | 378 | Email sending via Brevo/Resend/SMTP |
| PDF Processor | `backend/pdf_processor.py` | 82 | Page extraction and rotation using pikepdf |
| File Processing | `backend/processing.py` | ~300 | File conversion and processing pipeline |
| SMS | `backend/sms.py` | ~170 | Optional SMS notifications via USB GSM modem |

---

## Frontend Stack

### Technology

| Technology | Purpose |
|---|---|
| **Vanilla HTML5** | Page structure (no framework like React/Vue) |
| **Vanilla CSS** | Inline styles within each HTML file, dark glassmorphism theme |
| **Vanilla JavaScript** | Inline scripts, `fetch()` API calls, DOM manipulation |
| **Inter** (Google Fonts) | Primary UI typeface (`400, 500, 600, 700` weights) |

### Design System

- **Theme**: Dark mode with glassmorphism (backdrop-blur, translucent surfaces)
- **Color Palette**: Monochrome base with indigo accent (`#6366f1`), status colors (green/amber/red/purple)
- **Layout**: Responsive CSS with media queries at 1200px, 992px, 768px, 480px breakpoints
- **Animations**: CSS transitions, keyframe animations for pending job pulse, toast slide-up

### Frontend Pages

| Page | File | Size | Purpose |
|---|---|---|---|
| Upload Page | `frontend/index.html` | 147 KB | Customer file upload with drag-drop, PDF preview, cost estimator, print options |
| Login Page | `frontend/login.html` | 38 KB | Email/password signup with OTP verification, Google OAuth login |
| Staff Dashboard | `frontend/staff.html` | 78 KB | Real-time job queue management, download tracking, action buttons |
| Admin Dashboard | `frontend/admin.html` | 52 KB | Analytics charts, system overview, user stats |
| Order History | `frontend/history.html` | 20 KB | Past uploads and status tracking for logged-in users |
| Privacy Policy | `frontend/privacy.html` | 7 KB | Privacy policy page |
| Terms of Service | `frontend/terms.html` | 6 KB | Terms of service page |

### JavaScript Modules

| File | Purpose |
|---|---|
| `frontend/js/estimator.js` | Real-time cost calculation from `pricing.json` |
| `frontend/js/pdf-preview.js` | PDF.js integration for page preview, selection, and rotation |

### PWA (Progressive Web App)

| File | Purpose |
|---|---|
| `frontend/manifest.json` | App metadata, icons, display mode for installable PWA |
| `frontend/service-worker.js` | Offline caching of static assets |

- App name: "Upload and Print"
- Display: Standalone (no browser chrome)
- Theme color: `#6366f1` (Indigo)
- Installable on mobile devices from the staff dashboard

### Key Frontend Libraries (CDN)

| Library | Purpose |
|---|---|
| **PDF.js** (Mozilla) | Client-side PDF rendering in the upload page preview |

### Pricing Configuration

Defined in `frontend/pricing.json`:
- Base prices: B/W at 1.80 INR/page, Color at 5.00 INR/page
- Paper sizes: A4 (1x), A3 (2x), Letter (1x), Legal (1.2x)
- Binding: None (free), Staple (1.00), Spiral (5.00), Hardcover (20.00)
- Bulk discount: 10% off for 10+ copies
- Minimum job: 1.80 INR

---

## Database

### Engine

| Technology | Purpose |
|---|---|
| **PostgreSQL** | Primary database (hosted on Supabase) |
| **psycopg2-binary** | Python PostgreSQL adapter |

### Connection Management

- **ThreadedConnectionPool**: Min 2, Max 20 connections
- **Health checks**: `SELECT 1` before each connection use; stale connections are discarded and replaced
- **Retry logic**: Exponential backoff with jitter for connection failures (up to 5 retries)
- **Connection string**: Loaded from `DATABASE_URL` environment variable

### Schema (5 Tables)

#### `jobs` Table
Main table tracking print jobs through their lifecycle.

| Column | Type | Purpose |
|---|---|---|
| id | SERIAL PK | Auto-increment ID |
| ticket | TEXT UNIQUE | Daily ticket number (format: YYYYMMDD-NNN) |
| token | TEXT UNIQUE | 6-char alphanumeric payment token |
| sender | TEXT | Customer name |
| phone | TEXT | Customer phone |
| customer_email | TEXT | Customer email |
| filename | TEXT | Original uploaded filename |
| filepath | TEXT | Storage URL (Supabase public URL) |
| size_bytes | INTEGER | File size |
| status | TEXT | Job lifecycle status |
| created_at | TEXT | IST timestamp |
| color_mode | TEXT | "bw" or "color" |
| duplex | INTEGER | 0 = single, 1 = double-sided |
| paper_size | TEXT | A4, A3, Letter, Legal |
| binding | TEXT | none, staple, spiral, hardcover |
| copies | INTEGER | Number of copies |
| selected_pages | TEXT | Page selection string |
| rotations | TEXT | Page rotation JSON |
| estimate_cents | INTEGER | Server-calculated price |
| client_estimate_cents | INTEGER | Client-calculated price |
| order_id | TEXT | Cashfree order ID (format: ORDYYYYMMDDNNN) |
| payment_id | TEXT | Cashfree payment ID |
| payment_gateway | TEXT | "cashfree" |
| storage_type | TEXT | "supabase" or "drive" |
| drive_file_id | TEXT | Google Drive file ID |
| user_id | INTEGER | FK to users table |

**Job Status Lifecycle**:
```
created --> pending_payment --> paid --> downloaded --> printed --> delivered
                                   \--> cancelled
                                   \--> refunded
                                   \--> expired (auto, after retention period)
```

#### `users` Table
| Column | Type | Purpose |
|---|---|---|
| id | SERIAL PK | User ID |
| email | TEXT UNIQUE | Email address |
| password_hash | TEXT | bcrypt hash (null for Google-only users) |
| name | TEXT | Display name |
| google_id | TEXT UNIQUE | Google OAuth user ID |
| google_refresh_token | TEXT | Stored for Google Drive access |
| google_drive_folder_id | TEXT | Cached Drive folder ID |
| created_at | TEXT | Registration timestamp |
| last_login | TEXT | Last login timestamp |

#### `payments` Table
Audit trail for all payment transactions.
- Fields: order_id, payment_id, payment_gateway, payment_method, amount_cents, currency, status, refund tracking, customer details, IP address, timestamps

#### `webhook_events` Table
Idempotent webhook processing log (event_id, event_type, payload, status, timestamps).

#### `sms_log` Table
SMS notification tracking (phone, message, status, job_ticket, timestamps).

### Database Indexes
- Jobs: ticket, token, status, user_id, created_at, order_id, payment_id
- Payments: order_id, payment_id, status, job_id
- Webhooks: event_id

### Migrations
Column migrations run automatically on startup using a separate autocommit connection. Each `ALTER TABLE` runs independently so failures do not affect other migrations.

---

## File Storage

### Dual Storage Strategy

| Storage | Use Case | Library |
|---|---|---|
| **Supabase Storage** | Default file storage for all uploads | `supabase` Python SDK >= 2.0.0 |
| **Google Drive** | Optional per-user storage (files in user's own Drive) | `google-api-python-client` >= 2.0.0 |

### Supabase Storage
- Bucket: `uploads`
- Upload methods: Direct bytes upload, signed URL upload (frontend uploads directly to Supabase)
- Operations: upload, download, delete, list, orphan cleanup
- Public URLs generated for file access

### Google Drive
- Uses OAuth2 refresh tokens stored per-user
- Files stored in an app-created folder (`XeroxUploads`) in the user's Drive
- Access token caching (50-minute TTL) to avoid repeated OAuth refreshes
- Supports chunked streaming downloads (2MB chunks) for large files
- Direct CDN download URL generation for staff downloads

---

## Authentication and Security

### Authentication Methods

| Method | Library | Purpose |
|---|---|---|
| **Email/Password** | bcrypt (via passlib) | Traditional registration with OTP email verification |
| **Google OAuth 2.0** | google-auth, google-auth-oauthlib | One-click login with Google, also grants Drive access |
| **JWT Tokens** | python-jose (JOSE/JWS) | Session management via HttpOnly cookies |
| **Staff HTTP Basic Auth** | FastAPI built-in | Staff dashboard access control |

### Security Details

- **Password hashing**: bcrypt with auto salt generation
- **JWT**: HS256 algorithm, 30-day expiration, stored as HttpOnly + Secure + SameSite=Lax cookies
- **OTP verification**: 6-digit code, 10-minute expiry, rate-limited (60s cooldown per email)
- **Rate limiting**: 500 requests/IP/hour for uploads, 10 concurrent connections
- **File validation**: Whitelist of allowed extensions (PDF, DOC, DOCX, XLS, XLSX, PPT, PPTX, ODT, ODS, ODP, TXT, RTF, JPG, PNG, TIFF)
- **Max file size**: 5 GB (configurable)
- **CSRF protection**: State parameter in Google OAuth flow
- **Staff auth**: HTTP Basic credentials (username/password) on `/staff` route
- **Admin auth**: Separate credentials for admin dashboard

### Google OAuth Flow
1. Frontend redirects to Google authorization URL (with `drive.file` scope)
2. User authorizes and is redirected back with authorization code
3. Backend exchanges code for access token + refresh token
4. Refresh token stored in DB for future Google Drive API calls
5. JWT cookie set for session management

---

## Payment Integration

### Payment Gateway

| Technology | Purpose |
|---|---|
| **Cashfree** | Payment processing (UPI, cards, netbanking) |

### Integration Details

- **Module**: `backend/cashfree.py`
- **API Version**: 2023-08-01
- **Environments**: Sandbox (TEST) and Production
- **Order format**: `ORDYYYYMMDDNNN` (e.g., ORD20260321001)
- **Currency**: INR (amounts in paise internally, rupees for API)

### Payment Flow
```
1. Customer selects files and print options
2. Frontend calculates cost from pricing.json
3. Backend creates Cashfree order (POST /pg/orders)
4. Frontend shows Cashfree payment widget
5. Customer completes payment (UPI/Card/Netbanking)
6. Backend verifies payment (GET /pg/orders/{id} with retry)
7. Token assigned, email confirmation sent
8. Job visible on staff dashboard
```

### Features
- **Order creation**: With customer details and order tags
- **Payment verification**: Server-side status check with 3 retries (Cashfree recommends this over signature verification)
- **Webhook processing**: HMAC-SHA256 signature verification for async payment notifications
- **Refunds**: Full refund API integration

---

## Email Notifications

### Email Backends (Prioritized)

| Priority | Backend | Library | Notes |
|---|---|---|---|
| 1 | **Brevo** (Sendinblue) | HTTP REST API | Free tier: 300 emails/day |
| 2 | **Resend** | HTTP REST API | Recommended for cloud (Render) |
| 3 | **SMTP** | Python smtplib | For local dev or hosts with SMTP access |
| 4 | **Mock** | Logging only | Fallback when no backend configured |

### Email Types
- **OTP Verification**: 6-digit code for registration
- **Payment Confirmation**: Token number, amount, order details, pickup instructions
- **Job Ready Notification**: Token number and pickup info
- **Password Reset**: OTP for password recovery

---

## PDF and File Processing

| Technology | Version | Purpose |
|---|---|---|
| **pikepdf** | >= 8.0.0 | PDF page extraction and rotation |
| **Pillow** | >= 10.0.0 | Image processing |
| **qrcode** | >= 7.4.0 | QR code generation for upload/WiFi QR sheets |
| **LibreOffice** (Docker only) | System package | Office document to PDF conversion |
| **Ghostscript** (Docker only) | System package | PDF optimization and processing |

### PDF Processing Pipeline
1. Customer uploads PDF and selects specific pages/rotations in the browser
2. Backend extracts selected pages using pikepdf
3. Rotations applied per-page
4. Processed file stored alongside original

---

## Background Jobs and Scheduling

| Technology | Version | Purpose |
|---|---|---|
| **APScheduler** | >= 3.10.0 | In-process async task scheduler |

### Scheduled Tasks
- **Daily cleanup at midnight IST** (18:30 UTC): Delete jobs and files older than 5 days
- **Periodic cleanup every 4 hours**: Safety net for missed cleanup runs
- **External cron endpoint** (`GET /api/cron/cleanup`): For cron-job.org or similar external triggers

### Cleanup Logic
- Supabase files: Always cleaned up after retention period
- Google Drive files (paid jobs): Preserved permanently in user's Drive
- Google Drive files (unpaid jobs): Cleaned up after retention period
- Guest job records: Fully deleted (no history needed)
- Archived jobs: Deleted after 1 day
- Orphaned storage files: Detected and deleted by comparing storage listing against DB references

---

## Deployment and Infrastructure

### Hosting

| Service | Purpose |
|---|---|
| **Render** | Primary deployment platform (web service) |
| **Supabase** | Managed PostgreSQL + file storage |
| **Google Cloud** | OAuth2 credentials and Google Drive API |
| **Cashfree** | Payment processing |
| **Brevo / Resend** | Transactional email delivery |

### Render Configuration (`render.yaml`)
- Runtime: Python
- Build: `pip install -r backend/requirements.txt`
- Start: `uvicorn backend.app:app --host 0.0.0.0 --port $PORT --workers 2`
- Health check: `GET /health`

### Docker (`docker/Dockerfile`)
- Base: `python:3.11-slim`
- System deps: LibreOffice (writer/calc/impress), Ghostscript, qpdf, libmagic
- Port: 5000
- Health check: Every 30s, 10s timeout

### Docker Compose (`docker/docker-compose.yml`)
- Single service setup with environment variable configuration

### Environment Variables

| Category | Variables |
|---|---|
| **Server** | APP_HOST, APP_PORT |
| **Storage** | STORAGE_DIR, ARCHIVE_DIR, DB_PATH, LOG_DIR |
| **Supabase** | DATABASE_URL, SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_STORAGE_BUCKET |
| **Auth** | JWT_SECRET_KEY, JWT_ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES |
| **Google** | GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_DRIVE_FOLDER_NAME |
| **Payments** | CASHFREE_APP_ID, CASHFREE_SECRET_KEY, CASHFREE_WEBHOOK_SECRET, CASHFREE_ENVIRONMENT |
| **Email** | BREVO_API_KEY, RESEND_API_KEY, SENDER_EMAIL, SMTP_HOST/PORT/USER/PASSWORD/FROM |
| **Staff** | STAFF_USERNAME, STAFF_PASSWORD, ADMIN_USERNAME, ADMIN_PASSWORD |
| **Limits** | MAX_FILE_MB, DISK_LOW_THRESHOLD_MB, RATE_LIMIT_PER_IP_PER_HOUR |

---

## Testing

| Technology | Version | Purpose |
|---|---|---|
| **pytest** | >= 8.0.0 | Test framework |
| **pytest-playwright** | >= 0.4.0 | Browser-based end-to-end testing |

### Test Files
| File | Purpose |
|---|---|
| `tests/conftest.py` | Test fixtures and configuration |
| `tests/test_upload_flow.py` | End-to-end upload flow tests |
| `tests/test_staff_dashboard.py` | Staff dashboard UI tests |

### Other Testing Tools
| File | Purpose |
|---|---|
| `scripts/stress_upload.py` | Load testing with concurrent uploads (uses `aiohttp`) |

---

## Utility Scripts

| Script | Purpose |
|---|---|
| `scripts/gen_upload_qr.py` | Generate QR code poster for the upload URL |
| `scripts/gen_wifi_qr.py` | Generate QR code for WiFi hotspot connection |
| `scripts/stress_upload.py` | Stress test with concurrent file uploads |
| `start-dev.sh` | Local development server startup |
| `start-production.sh` | Production server startup with gunicorn |
| `keep-alive.sh` | Keep-alive pinger for Render free tier |

---

## Project File Map

```
xerox-upload/
|-- backend/
|   |-- __init__.py              # Package init
|   |-- app.py                   # Main FastAPI application (2978 lines)
|   |-- auth.py                  # Authentication (JWT, OAuth, bcrypt)
|   |-- cashfree.py              # Cashfree payment integration
|   |-- config.py                # Pydantic settings from env vars
|   |-- db.py                    # PostgreSQL database operations
|   |-- google_drive.py          # Google Drive API integration
|   |-- notifications.py         # Email via Brevo/Resend/SMTP
|   |-- pdf_processor.py         # PDF page extraction and rotation
|   |-- processing.py            # File processing pipeline
|   |-- sms.py                   # SMS via USB GSM modem (optional)
|   |-- storage.py               # Supabase Storage operations
|   |-- requirements.txt         # Python dependencies
|
|-- frontend/
|   |-- index.html               # Customer upload page (147 KB)
|   |-- login.html               # Login/signup page (38 KB)
|   |-- staff.html               # Staff dashboard (78 KB)
|   |-- admin.html               # Admin analytics dashboard (52 KB)
|   |-- history.html             # Order history page (20 KB)
|   |-- privacy.html             # Privacy policy
|   |-- terms.html               # Terms of service
|   |-- pricing.json             # Pricing configuration
|   |-- manifest.json            # PWA manifest
|   |-- service-worker.js        # PWA service worker
|   |-- js/
|   |   |-- estimator.js         # Cost calculation module
|   |   |-- pdf-preview.js       # PDF.js preview module
|   |-- icons/
|       |-- icon-192.png         # PWA icon (192x192)
|       |-- icon-512.png         # PWA icon (512x512)
|
|-- docker/
|   |-- Dockerfile               # Docker build (Python 3.11 + LibreOffice)
|   |-- docker-compose.yml       # Docker Compose config
|
|-- scripts/
|   |-- gen_upload_qr.py         # Upload QR code generator
|   |-- gen_wifi_qr.py           # WiFi QR code generator
|   |-- stress_upload.py         # Load testing script
|
|-- tests/
|   |-- conftest.py              # Test fixtures
|   |-- test_upload_flow.py      # Upload flow tests
|   |-- test_staff_dashboard.py  # Dashboard tests
|
|-- .env.example                 # Environment template
|-- render.yaml                  # Render deployment config
|-- main.py                      # Entry point (imports backend.app)
|-- start-dev.sh                 # Dev server script
|-- start-production.sh          # Production server script
|-- keep-alive.sh                # Keep-alive pinger
```

---

## Technology Summary

| Layer | Technologies |
|---|---|
| **Language** | Python 3.11, JavaScript (ES6+), HTML5, CSS3 |
| **Backend Framework** | FastAPI + Uvicorn + Gunicorn |
| **Database** | PostgreSQL (Supabase-hosted), psycopg2 connection pool |
| **File Storage** | Supabase Storage, Google Drive API v3 |
| **Authentication** | JWT (HS256), bcrypt, Google OAuth 2.0, HTTP Basic |
| **Payments** | Cashfree (API v2023-08-01) |
| **Email** | Brevo API, Resend API, SMTP |
| **PDF Processing** | pikepdf, Pillow |
| **Scheduling** | APScheduler (AsyncIOScheduler) |
| **Frontend** | Vanilla HTML/CSS/JS, Inter font, PDF.js |
| **PWA** | Service Worker, Web App Manifest |
| **Deployment** | Render, Docker (Python 3.11-slim + LibreOffice) |
| **Testing** | pytest, pytest-playwright, aiohttp stress tests |
| **Version Control** | Git |
