# Xerox Upload System - Technical Specifications

## Overview
A complete print job management system that allows users to upload documents for printing and staff to manage, download, and process those jobs.

---

## File Upload Limits

| Parameter | Value | Notes |
|-----------|-------|-------|
| **Files per upload** | 1 | Single file per upload session |
| **Max file size** | 5 GB (5120 MB) | Supports video files |
| **Copies per job** | 1 - 100 | Bulk discount at 10+ copies |

---

## Supported File Formats

### Documents
`pdf`, `doc`, `docx`, `xls`, `xlsx`, `ppt`, `pptx`, `odt`, `ods`, `odp`, `txt`, `rtf`

### Images
`jpg`, `jpeg`, `png`, `gif`, `bmp`, `webp`, `tiff`

---

## Print Options

| Option | Choices |
|--------|---------|
| **Color Mode** | B&W, Color |
| **Sides** | Single, Double (20% discount) |
| **Paper Size** | A4 (1x), A3 (2x), Letter (1x), Legal (1.2x) |
| **Binding** | None (₹0), Staple (₹1), Spiral (₹5), Hard Cover (₹20) |

---

## Pricing (INR)

| Type | Price per Page |
|------|---------------|
| B&W | ₹2.00 |
| Color | ₹5.00 |
| **Minimum Job** | ₹5.00 |

**Discounts:**
- Double-sided: 20% off
- Bulk (10+ copies): 10% off

---

## Rate Limiting

| Limit | Value |
|-------|-------|
| Uploads per IP per hour | 10 |
| Concurrent uploads | 2 |

---

## File Retention

| Stage | Duration |
|-------|----------|
| Active jobs | Until completed |
| Archived jobs | 14 days |
| Auto-cleanup | Runs on server startup |

---

## Storage Locations

| Type | Path |
|------|------|
| Incoming files | `./incoming_files/` |
| Archived files | `./archived_files/` |
| Database | `./jobs.db` |
| Logs | `./logs/` |

---

## Staff Dashboard Features

### Job Management
- View all pending, paid, downloaded, printed, and archived jobs
- Filter jobs by status
- Real-time job count statistics
- Download files for printing
- Mark jobs as printed/completed
- Archive completed jobs

### Job Information Displayed
- Ticket number (e.g., XRX240130001)
- User name and email
- Filename and file size
- Upload timestamp
- Print options (color, sides, paper, binding, copies)
- Estimated price
- Payment status

### Actions Available
| Action | Description |
|--------|-------------|
| **Download** | Downloads the file for printing |
| **Mark Printed** | Updates status to printed |
| **Mark Downloaded** | Updates status to downloaded |
| **Archive** | Moves job to archived status |

### Status Flow
```
pending → paid → downloaded → printed → archived
```

### Staff Authentication
- Password-protected access
- Staff passphrase required (configurable via `STAFF_PASSPHRASE` env variable)

---

## Authentication Methods

| Method | Details |
|--------|---------|
| Email/Password | Standard registration and login |
| Google OAuth | Sign in with Google account |
| Session duration | 60 minutes (JWT token) |

---

## Payment Integration

- **Gateway:** Razorpay
- **Webhook support:** For payment confirmation
- **Status tracking:** pending_payment → paid

---

## API Endpoints Summary

### User Endpoints
- `POST /api/upload` - Upload file
- `GET /api/user/jobs` - Get user's job history
- `POST /api/auth/login` - Login
- `POST /api/auth/register` - Register
- `POST /api/auth/google` - Google OAuth

### Staff Endpoints
- `GET /api/staff/jobs` - List all jobs
- `PUT /api/staff/jobs/{ticket}/status` - Update job status
- `GET /api/download/{ticket}` - Download file

### Payment Endpoints
- `POST /api/payment/initiate` - Start payment
- `POST /api/payment/webhook` - Razorpay webhook
