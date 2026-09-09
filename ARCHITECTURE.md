# Xerox Upload System - Technical Architecture

## Overview

This document describes the complete file transfer flow from upload to download, including all technical components and optimizations.

## System Components

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Frontend      │     │   Backend       │     │   Supabase      │
│   (HTML/JS)     │────▶│   (FastAPI)     │────▶│   Storage +     │
│                 │     │   on Render     │     │   PostgreSQL    │
└─────────────────┘     └─────────────────┘     └─────────────────┘
```

### Technology Stack

| Component | Technology |
|-----------|------------|
| Frontend | Vanilla HTML/CSS/JS |
| PDF Preview | PDF.js |
| PDF Processing (Client) | pdf-lib |
| Backend | Python FastAPI |
| Database | PostgreSQL (Supabase) |
| File Storage | Supabase Storage |
| Hosting | Render.com |
| Payments | Razorpay |

---

## File Upload Flow

### Step 1: File Selection & Preview

```
User selects PDF file
        │
        ▼
┌─────────────────────────────────────┐
│  PDF.js renders page thumbnails     │
│  in the browser                     │
└─────────────────────────────────────┘
        │
        ▼
User can:
  • Select specific pages (1, 3, 5-10)
  • Rotate individual pages (90°, 180°, 270°)
  • Choose print options (color, duplex, binding)
```

### Step 2: Client-Side PDF Processing (NEW - Faster!)

If user selects specific pages or rotates any pages:

```javascript
// Using pdf-lib in the browser
const { PDFDocument, degrees } = PDFLib;

// 1. Load original PDF
const srcDoc = await PDFDocument.load(arrayBuffer);

// 2. Create new PDF with only selected pages
const pdfDoc = await PDFDocument.create();
const copiedPages = await pdfDoc.copyPages(srcDoc, selectedPageIndices);

// 3. Apply rotations
for (let page of copiedPages) {
    if (rotation) {
        page.setRotation(degrees(currentRotation + rotation));
    }
    pdfDoc.addPage(page);
}

// 4. Export processed PDF (SMALLER file!)
const processedBytes = await pdfDoc.save();
```

**Benefits:**
- 12-page PDF (3.86 MB) → Select 2 pages → Upload only ~0.6 MB
- Rotations applied instantly in browser
- No server processing needed
- Upload time reduced by 50-80%

### Step 3: Get Signed Upload URL

```
Frontend                          Backend                          Supabase
   │                                 │                                │
   │  POST /api/upload/get-signed-url│                                │
   │  {filename, sender, metadata}   │                                │
   │────────────────────────────────▶│                                │
   │                                 │                                │
   │                                 │  Generate unique filename      │
   │                                 │  Generate ticket number        │
   │                                 │                                │
   │                                 │  create_signed_upload_url()    │
   │                                 │────────────────────────────────▶│
   │                                 │                                │
   │                                 │◀────────────────────────────────│
   │                                 │  {signed_url, token}           │
   │                                 │                                │
   │◀────────────────────────────────│                                │
   │  {ticket, signed_url, token}    │                                │
```

**Backend Code (app.py):**
```python
@app.post("/api/upload/get-signed-url")
async def get_signed_upload_url(data: SignedUrlRequest):
    # Generate unique ticket (YYYYMMDD-NNN)
    if data.parent_ticket:
        # Bulk order: 20260205-004-1, 20260205-004-2
        existing = db.get_jobs_by_group_ticket(data.parent_ticket)
        ticket = f"{data.parent_ticket}-{len(existing) + 1}"
    else:
        ticket = db.generate_ticket()  # 20260205-007
    
    # Create unique storage filename
    storage_filename = f"{ticket}_{uuid4().hex[:8]}{extension}"
    
    # Get signed URL from Supabase (valid for 1 hour)
    signed_url, token = storage.create_signed_upload_url(storage_filename)
    
    return {
        "ticket": ticket,
        "storage_filename": storage_filename,
        "signed_url": signed_url,
        "token": token
    }
```

### Step 4: Direct Upload to Supabase

```
Frontend                                      Supabase Storage
   │                                                │
   │  PUT {signed_url}                              │
   │  Header: Content-Type: application/pdf         │
   │  Body: [PDF binary data]                       │
   │───────────────────────────────────────────────▶│
   │                                                │
   │           (File uploaded DIRECTLY)             │
   │                                                │
   │◀───────────────────────────────────────────────│
   │  200 OK                                        │
```

**Key Point:** File goes **directly** to Supabase, NOT through the Render server!

**Frontend Code (index.html):**
```javascript
const xhr = new XMLHttpRequest();

xhr.upload.addEventListener('progress', (e) => {
    const percent = 15 + Math.round((e.loaded / e.total) * 75);
    progressFill.style.width = percent + '%';
});

xhr.open('PUT', signed_url);
xhr.setRequestHeader('Content-Type', 'application/pdf');
xhr.send(file);  // Processed file (smaller!)
```

### Step 5: Confirm Upload & Create Database Entry

```
Frontend                          Backend                          Supabase DB
   │                                 │                                │
   │  POST /api/upload/confirm       │                                │
   │  {ticket, filename, size}       │                                │
   │────────────────────────────────▶│                                │
   │                                 │                                │
   │                                 │  Get public URL for file       │
   │                                 │  Create job entry              │
   │                                 │────────────────────────────────▶│
   │                                 │  INSERT INTO jobs (...)        │
   │                                 │                                │
   │◀────────────────────────────────│                                │
   │  {status: "ok", ticket: "007"}  │                                │
```

**Database Schema (jobs table):**
```sql
CREATE TABLE jobs (
    id SERIAL PRIMARY KEY,
    ticket VARCHAR(50) UNIQUE,
    sender VARCHAR(255),
    phone VARCHAR(50),
    filename VARCHAR(255),
    filepath TEXT,  -- Supabase Storage URL
    size_bytes INTEGER,
    status VARCHAR(50) DEFAULT 'pending',
    created_at TIMESTAMP,
    color_mode VARCHAR(20),
    duplex BOOLEAN,
    paper_size VARCHAR(20),
    binding VARCHAR(50),
    copies INTEGER,
    selected_pages TEXT,  -- JSON array
    rotations TEXT,       -- JSON object
    client_estimate_cents INTEGER,
    user_id INTEGER,
    paid_at TIMESTAMP,
    razorpay_order_id VARCHAR(100)
);
```

---

## File Download Flow

### Step 1: Staff Clicks Download

```
Staff Dashboard                   Backend                          Supabase
   │                                 │                                │
   │  GET /api/download/{ticket}     │                                │
   │────────────────────────────────▶│                                │
   │                                 │                                │
   │                                 │  db.get_job_by_ticket(ticket)  │
   │                                 │────────────────────────────────▶│
   │                                 │◀────────────────────────────────│
   │                                 │  job.filepath (Supabase URL)   │
   │                                 │                                │
   │                                 │  Update status to "downloaded" │
   │                                 │                                │
   │◀────────────────────────────────│                                │
   │  302 Redirect to Supabase URL   │                                │
   │  + ?download=filename parameter │                                │
```

### Step 2: Direct Download from Supabase

```
Staff Browser                                 Supabase Storage
   │                                                │
   │  GET https://xxx.supabase.co/storage/v1/...    │
   │      ?download=document.pdf                    │
   │───────────────────────────────────────────────▶│
   │                                                │
   │◀───────────────────────────────────────────────│
   │  Content-Disposition: attachment               │
   │  [PDF binary data]                             │
   │                                                │
   │  (File downloads directly from Supabase CDN)   │
```

**Backend Code (app.py):**
```python
@app.get("/api/download/{ticket}")
async def download_file(ticket: str):
    job = db.get_job_by_ticket(ticket)
    
    # Use processed file if available
    file_url = job.get("processed_filepath") or job["filepath"]
    download_filename = job["filename"]
    
    # Update status to downloaded
    if job["status"] == "pending":
        db.update_job_status(ticket, "downloaded")
    
    # Add download parameter for forced download (not preview)
    parsed = urlparse(file_url)
    query_params = parse_qs(parsed.query)
    query_params['download'] = [download_filename]
    download_url = urlunparse(parsed._replace(query=urlencode(query_params)))
    
    return RedirectResponse(url=download_url)
```

---

## Ticket Numbering System

### Format
- **Single uploads:** `YYYYMMDD-NNN` (e.g., `20260205-001`)
- **Bulk orders:** `YYYYMMDD-NNN-M` (e.g., `20260205-004-1`, `20260205-004-2`)

### Generation Logic

```python
def generate_ticket() -> str:
    today = get_ist_now().date()
    date_prefix = today.strftime("%Y%m%d")  # "20260205"
    
    # Count only MAIN tickets, exclude sub-items
    cursor.execute("""
        SELECT MAX(CAST(SUBSTRING(ticket FROM 10) AS INTEGER))
        FROM jobs 
        WHERE ticket LIKE %s
        AND ticket ~ %s
    """, (f"{date_prefix}-%", f"^{date_prefix}-[0-9]+$"))
    
    last_ticket = result[0] or 0
    next_ticket = last_ticket + 1
    
    return f"{date_prefix}-{next_ticket:03d}"  # "20260205-007"
```

### Bulk Order Flow
1. First file → `20260205-004` (new main ticket)
2. Add second file → parent_ticket=`20260205-004` → `20260205-004-1`
3. Add third file → parent_ticket=`20260205-004` → `20260205-004-2`

---

## Performance Optimizations

### 1. Client-Side PDF Processing
- **Before:** Upload 12-page PDF → Server downloads → Processes → Re-uploads
- **After:** Browser extracts 2 pages → Upload only processed PDF
- **Improvement:** 50-80% faster for PDFs with page selection

### 2. Direct Uploads (Signed URLs)
- **Before:** User → Render → Supabase (file passes through server)
- **After:** User → Supabase (direct, bypasses Render)
- **Improvement:** Reduced latency, no server bottleneck

### 3. Direct Downloads
- **Before:** Supabase → Render → Staff
- **After:** Supabase → Staff (redirect)
- **Improvement:** Faster downloads, reduced server load

### 4. Forced Download Headers
- Added `?download=filename.pdf` parameter to Supabase URLs
- Files download directly instead of opening in browser preview

---

## File Storage Structure

### Supabase Storage Bucket: `uploads`

```
uploads/
├── 20260205-001_abc12345.pdf        # Single upload
├── 20260205-002_def67890.jpg        # Image upload
├── 20260205-004_ghi11111.pdf        # Bulk order main
├── 20260205-004-1_jkl22222.pdf      # Bulk order item 2
├── 20260205-004-2_mnl33333.pdf      # Bulk order item 3
├── 20260205-004_processed.pdf       # Server-processed version (if any)
└── ...
```

### URL Format
```
Public URL:
https://{project}.supabase.co/storage/v1/object/public/uploads/{filename}

Signed Upload URL:
https://{project}.supabase.co/storage/v1/object/upload/sign/uploads/{filename}?token=xxx
```

---

## Database Tables

### jobs
Main table for print jobs.

### users
User accounts (email, password hash, admin flag).

### bulk_payments
Payment records linking multiple jobs to a single Razorpay order.

### webhook_events
Idempotency tracking for Razorpay webhooks.

---

## Security

### Authentication
- JWT tokens stored in localStorage
- Tokens expire after 7 days
- Staff endpoints require admin role

### Upload Security
- Passphrase validation (optional)
- File type validation
- Size limits (50 MB)
- Rate limiting (10 uploads/hour per IP)

### Storage Security
- Signed upload URLs expire after 1 hour
- Public read access for downloads
- Supabase RLS policies for bucket access

---

## Environment Variables

```env
# Supabase
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=eyJ...
SUPABASE_SERVICE_KEY=eyJ...

# Razorpay
RAZORPAY_KEY_ID=rzp_test_xxx
RAZORPAY_KEY_SECRET=xxx
RAZORPAY_WEBHOOK_SECRET=xxx

# App
SECRET_KEY=your-jwt-secret
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=admin123
PASSPHRASE=your-passphrase
```

---

## Flow Summary Diagram

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              UPLOAD FLOW                                      │
│                                                                              │
│  User selects PDF                                                            │
│       │                                                                      │
│       ▼                                                                      │
│  ┌─────────────────────┐                                                     │
│  │ Client-side PDF     │  ─── Uses pdf-lib to extract pages                 │
│  │ Processing          │  ─── Applies rotations                              │
│  └─────────────────────┘  ─── Creates smaller PDF                           │
│       │                                                                      │
│       ▼                                                                      │
│  ┌─────────────────────┐                                                     │
│  │ Get Signed URL      │  ─── Backend generates ticket                       │
│  │ from Backend        │  ─── Gets Supabase signed URL                       │
│  └─────────────────────┘                                                     │
│       │                                                                      │
│       ▼                                                                      │
│  ┌─────────────────────┐                                                     │
│  │ Direct Upload to    │  ─── Bypasses server                                │
│  │ Supabase Storage    │  ─── Uses signed URL                                │
│  └─────────────────────┘                                                     │
│       │                                                                      │
│       ▼                                                                      │
│  ┌─────────────────────┐                                                     │
│  │ Confirm Upload      │  ─── Creates database entry                         │
│  │ (Backend)           │  ─── Returns ticket number                          │
│  └─────────────────────┘                                                     │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────┐
│                             DOWNLOAD FLOW                                     │
│                                                                              │
│  Staff clicks "Download"                                                     │
│       │                                                                      │
│       ▼                                                                      │
│  ┌─────────────────────┐                                                     │
│  │ GET /api/download/  │  ─── Fetches job from database                     │
│  │ {ticket}            │  ─── Updates status to "downloaded"                 │
│  └─────────────────────┘                                                     │
│       │                                                                      │
│       ▼                                                                      │
│  ┌─────────────────────┐                                                     │
│  │ 302 Redirect to     │  ─── Supabase URL + ?download=filename             │
│  │ Supabase URL        │  ─── Forces download (not preview)                  │
│  └─────────────────────┘                                                     │
│       │                                                                      │
│       ▼                                                                      │
│  ┌─────────────────────┐                                                     │
│  │ Direct download     │  ─── From Supabase CDN                              │
│  │ from Supabase       │  ─── Fast, no server involvement                    │
│  └─────────────────────┘                                                     │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```
