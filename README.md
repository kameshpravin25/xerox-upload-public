# PrintPress

A self-hosted print job management system built for local xerox and print shops. Customers upload documents, choose print options, pay online, and receive a ticket number. Staff manage the queue from a dashboard.

## Problem

Small print shops still rely on USB drives, WhatsApp file sharing, and manual payment tracking. Customers wait in line to hand over files, explain print settings verbally, and pay in cash. This is slow for both sides. Files get lost, orders get mixed up, and there is no record of what was requested.

## How It Works

PrintPress replaces the entire manual process with a single web application.

A customer visits the site, uploads a document, selects print options (color mode, paper size, number of copies, binding, specific pages, page rotation), sees the cost estimate, and pays through Razorpay. The system generates a sequential ticket number for the day. The customer receives a confirmation email and can track their order.

On the shop side, staff log into a dashboard that shows the live queue of paid jobs. They download files, print them, and mark orders as completed. The queue updates in real time.

The entire file transfer bypasses the application server. Uploads go directly from the browser to Supabase Storage using signed URLs. Downloads redirect staff straight to the storage CDN. The server only handles metadata, authentication, and payment verification.

## Time Saved

- Customers submit orders remotely instead of visiting the shop and waiting in line in person.
- Print options are captured digitally, eliminating miscommunication about copies, color, and page ranges.
- Payment is handled before printing, removing the need for cash transactions and manual bookkeeping.
- Staff see a filtered, sorted queue instead of managing a pile of USB drives and verbal instructions.

## Technical Details

### Architecture

```
Browser (HTML/JS)  -->  FastAPI Backend (Render)  -->  Supabase (PostgreSQL + Storage)
                                |
                          Razorpay (Payments)
```

### Stack

| Layer       | Technology                         |
|-------------|-------------------------------------|
| Frontend    | Vanilla HTML, CSS, JavaScript (PWA) |
| Backend     | Python, FastAPI                     |
| Database    | PostgreSQL via Supabase             |
| Storage     | Supabase Storage                    |
| Payments    | Razorpay                            |
| Auth        | Custom JWT, Google OAuth            |
| PDF Preview | PDF.js (client-side rendering)      |
| PDF Processing | pdf-lib (client-side page extraction and rotation) |
| Hosting     | Render                              |

### Upload Flow

1. Customer selects a file and configures print options.
2. For PDFs, the browser extracts only the selected pages and applies rotations using pdf-lib before uploading. A 12-page PDF where the customer selects 2 pages results in uploading roughly 0.6 MB instead of 3.8 MB.
3. The backend generates a ticket number and a signed upload URL from Supabase.
4. The browser uploads the file directly to Supabase Storage using the signed URL. The file never passes through the application server.
5. The backend creates a database record with file metadata, print options, and pricing.

### Download Flow

1. Staff clicks download on the dashboard.
2. The backend looks up the file URL, updates the job status, and returns a 302 redirect to the Supabase storage URL with a forced download header.
3. The file downloads directly from Supabase CDN to the staff browser.

### Payment Flow

1. Upload creates a job record and a Razorpay order.
2. The frontend opens the Razorpay checkout modal.
3. On successful payment, the frontend sends the payment signature to the backend for HMAC verification.
4. A Razorpay webhook provides backup verification in case the frontend call fails.
5. Bulk orders with multiple files in a cart are handled as a single Razorpay order.

### Ticket System

Tickets are sequential per day in the format NNN (001, 002, 003). They reset at midnight IST. Bulk orders use sub-tickets (009, 009-1, 009-2) grouped under a single parent.

### Authentication

Two methods are supported: email and password registration with bcrypt hashing, and Google OAuth. Sessions use JWT tokens stored as HttpOnly cookies. Authentication is optional for uploads; a passphrase-based access mode is also available.

### Staff Dashboard

- Real-time job queue with 3-second auto-refresh
- Status filters: All, Paid, Printed, Delivered, Pending, Refunded
- Sort by ticket number or submission time
- Actions: Download, Archive, Cancel, Mark Printed, Mark Delivered, Refund
- Job count statistics and disk space monitoring

### Automated Cleanup

A daily cron job (triggered externally via cron-job.org to work around Render free tier sleep) deletes jobs older than one day, removes associated payment records, and purges corresponding files from Supabase Storage.

## Project Structure

```
backend/
    app.py              Main FastAPI application and routes
    auth.py             JWT and Google OAuth authentication
    config.py           Environment-based settings
    db.py               PostgreSQL database operations
    storage.py          Supabase Storage integration
    razorpay.py         Payment order creation and verification
    notifications.py    Email notifications
    pdf_processor.py    Server-side PDF processing
    processing.py       File optimization pipeline
    requirements.txt    Python dependencies

frontend/
    index.html          Customer upload page
    staff.html          Staff dashboard
    login.html          Login and registration
    history.html        Customer order history
    pricing.json        Print pricing configuration
    developer.html      Developer credits
    manifest.json       PWA manifest
    service-worker.js   Service worker for offline support
```

## Setup

1. Clone the repository.
2. Copy `.env.example` to `.env` and fill in the required values. At minimum, you need a Supabase project (for PostgreSQL and file storage) and Razorpay API keys (for payment processing).
3. Install dependencies.

```
pip install -r backend/requirements.txt
```

4. Run the development server.

```
uvicorn backend.app:app --host 0.0.0.0 --port 5000
```

The application will be available at `http://localhost:5000`.

## Environment Variables

Refer to `.env.example` for the full list. The critical ones are:

| Variable             | Purpose                              |
|----------------------|--------------------------------------|
| DATABASE_URL         | Supabase PostgreSQL connection string |
| SUPABASE_URL         | Supabase project URL                  |
| SUPABASE_ANON_KEY    | Supabase anonymous API key            |
| RAZORPAY_KEY_ID      | Razorpay API key                      |
| RAZORPAY_KEY_SECRET  | Razorpay secret key                   |
| JWT_SECRET_KEY       | Secret for signing JWT tokens         |
| GOOGLE_CLIENT_ID     | Google OAuth client ID                |

## Developers

**Kamesh Pravin** -- Electronics and Computer Engineering

**Praneel N Chetty** -- Neural networks, transformers, and business studies
