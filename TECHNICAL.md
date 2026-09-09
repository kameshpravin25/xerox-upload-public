# Xerox Hotspot Upload System — Technical Overview

## Stack

| Layer | Tech |
|-------|------|
| **Backend** | Python FastAPI, Gunicorn |
| **Database** | Supabase PostgreSQL |
| **Storage** | Supabase Storage (bucket: `uploads`) |
| **Payments** | Razorpay |
| **Auth** | Custom JWT + Google OAuth |
| **Hosting** | Render (free tier) |
| **Frontend** | Vanilla HTML/CSS/JS (PWA) |

---

## File Structure

```
backend/
├── app.py           # FastAPI routes (upload, download, jobs, payments, auth)
├── auth.py          # JWT tokens, password hashing, Google OAuth verification
├── config.py        # Settings from .env (JWT secret, Supabase, Razorpay keys)
├── db.py            # PostgreSQL helpers (jobs, users, tickets, cleanup)
├── storage.py       # Supabase Storage (upload, download, delete, signed URLs)
├── pdf_processor.py # Page extraction & rotation (PyPDF2)
├── processing.py    # File optimization (images, PDFs)
├── razorpay.py      # Razorpay order creation & verification
├── notifications.py # Email notifications
├── sms.py           # GSM modem SMS (hardware-dependent)
└── requirements.txt

frontend/
├── index.html       # Upload page (customer-facing)
├── staff.html       # Staff dashboard (queue management)
├── login.html       # Login/signup page
├── history.html     # User order history
├── pricing.json     # Print pricing config
├── manifest.json    # PWA manifest
└── service-worker.js
```

---

## Upload Flow

```
Customer → index.html
    │
    ├─ Select file + print options (copies, color, pages, rotation)
    │
    ├─ Client-side PDF processing (pdf-lib)
    │   └─ Extract pages + apply rotations in browser BEFORE upload
    │
    ├─ POST /api/upload/get-signed-url
    │   └─ Backend generates ticket (e.g. 001) + Supabase signed URL
    │
    ├─ XHR PUT → Supabase Storage (direct upload, bypasses server)
    │   └─ File stored as: YYYYMMDD-TICKET.ext (e.g. 20260211-001.pdf)
    │
    └─ POST /api/upload/confirm
        └─ Backend creates DB entry with file URL + metadata
```

**Key**: Files upload directly to Supabase from the browser (not through the server), making uploads fast.

---

## Download Flow

```
Staff Dashboard → Click "Download"
    │
    ├─ GET /api/download/{ticket}
    │   └─ Backend fetches job, marks as "downloaded"
    │   └─ Redirects to Supabase URL with ?download= param (forces download, not preview)
    │
    └─ Browser downloads file directly from Supabase CDN
```

---

## Authentication

| Method | Flow |
|--------|------|
| **Email/Password** | Register → bcrypt hash → stored in `users` table. Login → verify hash → JWT cookie |
| **Google OAuth** | Frontend gets Google token → POST `/api/auth/google` → verify with Google API → create/get user → JWT cookie |

- JWT stored as `HttpOnly` cookie (`access_token`)
- Token expiry: configurable via `JWT_ACCESS_TOKEN_EXPIRE_MINUTES`
- Auth is optional for uploads (passphrase-based access also supported)

---

## Ticket Numbering

- Sequential per day: `001`, `002`, `003`...
- Resets at **midnight IST**
- Bulk orders: `009`, `009-1`, `009-2` (sub-tickets under group)

---

## Daily Cleanup (Cron)

| What | When | How |
|------|------|-----|
| DB jobs older than 1 day | 12:00 AM IST | `/api/cron/cleanup` via cron-job.org |
| Payment records (FK) | Same | Deleted before jobs to avoid FK violation |
| Supabase Storage files | Same | Scans bucket, deletes files with old date prefix |
| Archived jobs | Same | Separate cleanup call |

**External cron** (cron-job.org) triggers cleanup because Render's free tier sleeps the server.

---

## Database Tables

| Table | Purpose |
|-------|---------|
| `jobs` | Upload queue (ticket, sender, filepath, status, metadata, timestamps) |
| `users` | User accounts (email, password hash, name, Google ID) |
| `payments` | Razorpay payment audit trail (FK → jobs.id) |
| `archived_jobs` | Completed/archived jobs |

---

## Payment Flow (Razorpay)

```
Upload with payment → POST /api/upload-with-payment
    → Creates job + Razorpay order
    → Frontend opens Razorpay checkout
    → On success: POST /api/payment/verify
    → Webhook: /api/payment/webhook (backup verification)
    → Job status: pending_payment → paid → printed → delivered
```

Bulk orders: Multiple cart items → single Razorpay order via `POST /api/payment/bulk-order`.

---

## Key Environment Variables

```
DATABASE_URL          # Supabase PostgreSQL connection string
SUPABASE_URL          # Supabase project URL
SUPABASE_ANON_KEY     # Supabase anonymous key
JWT_SECRET_KEY        # JWT signing secret
GOOGLE_CLIENT_ID      # Google OAuth client ID
RAZORPAY_KEY_ID       # Razorpay API key
RAZORPAY_KEY_SECRET   # Razorpay secret
UPLOAD_PASSPHRASE     # Optional passphrase for uploads
```

---

## Staff Dashboard Features

- Real-time job queue with auto-refresh (3s)
- Sort by token or time
- Filter by status: All, Paid, Printed, Delivered, Pending, Refunded
- Actions: Download, Archive, Cancel, Mark Printed/Delivered, Refund
- Stats: Pending count, today's total, free disk space
