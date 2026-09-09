"""
Xerox Hotspot Upload System - FastAPI Application

Main application providing:
- Upload page for customers
- Staff UI for queue management
- REST API for file operations
"""

import os
import shutil
import logging
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List
from pydantic import BaseModel
from io import BytesIO

from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Request, Depends, Header, Response
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# Background scheduler for periodic cleanup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import settings
from . import db
from . import storage

# ============== Sentry Error Monitoring ==============
if settings.sentry_dsn:
    import sentry_sdk
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        traces_sample_rate=0.3,  # 30% of requests get performance tracing
        profiles_sample_rate=0.1,  # 10% profiling
        environment="production",
        release="printpress@2.0.0",
        send_default_pii=False,  # Don't send emails/IPs to Sentry
    )

# Configure logging
settings.ensure_directories()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(settings.log_dir / "app.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Initialize rate limiter
limiter = Limiter(key_func=get_remote_address)

# Initialize background scheduler
scheduler = AsyncIOScheduler()

# Create FastAPI app (docs disabled in production for security)
app = FastAPI(
    title="Xerox Hotspot Upload",
    description="Offline xerox upload system with queue management",
    version="2.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# ============== Brute-Force Protection ==============
_login_attempts: dict = {}  # { ip: { "count": N, "lockout_until": timestamp } }
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 300  # 5 minute lockout after max attempts

def _check_login_lockout(ip: str):
    """Check if an IP is locked out from login attempts. Raises 429 if locked."""
    import time
    entry = _login_attempts.get(ip)
    if not entry:
        return
    # Clean up expired lockouts
    if entry.get("lockout_until") and time.time() > entry["lockout_until"]:
        del _login_attempts[ip]
        return
    if entry.get("lockout_until") and time.time() < entry["lockout_until"]:
        remaining = int(entry["lockout_until"] - time.time())
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {remaining} seconds."
        )

def _record_failed_login(ip: str):
    """Record a failed login attempt. Triggers lockout after MAX_LOGIN_ATTEMPTS."""
    import time
    entry = _login_attempts.get(ip, {"count": 0})
    entry["count"] = entry.get("count", 0) + 1
    if entry["count"] >= MAX_LOGIN_ATTEMPTS:
        entry["lockout_until"] = time.time() + LOCKOUT_SECONDS
        logger.warning(f"Login lockout triggered for IP: {ip}")
    _login_attempts[ip] = entry

def _clear_login_attempts(ip: str):
    """Clear failed login attempts after successful login."""
    _login_attempts.pop(ip, None)

# ============== Cloudflare Turnstile Verification ==============
async def verify_turnstile(token: str, remote_ip: str = None) -> bool:
    """Verify a Cloudflare Turnstile token. Returns True if valid or if Turnstile is not configured."""
    secret = settings.turnstile_secret_key
    if not secret:
        return True  # Turnstile not configured, skip verification
    if not token:
        return True  # No token provided (JS didn't load / adblocker) — fail open to avoid locking out users
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": secret, "response": token, "remoteip": remote_ip or ""},
            )
            result = resp.json()
            if not result.get("success"):
                logger.warning(f"Turnstile verification failed: {result.get('error-codes', [])}")
            return result.get("success", False)
    except Exception as e:
        logger.error(f"Turnstile verification error: {e}")
        return True  # Fail open — don't block users if Cloudflare is down

# Add rate limiter to app
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ============== CORS — Strict Origin Whitelisting ==============
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://amritablr.printpress.in",
        "https://xerox-upload.onrender.com",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# ============== Trusted Host — Block Host Header Injection ==============
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[
        "amritablr.printpress.in",
        "xerox-upload.onrender.com",
        "localhost",
        "127.0.0.1",
    ],
)


# ============== Security Headers Middleware ==============
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Add security headers to all responses (CSP, HSTS, X-Frame-Options, etc.)."""
    response = await call_next(request)

    # 1. Content-Security-Policy — restrict resource loading to trusted sources
    # Note: 'unsafe-inline'/'unsafe-eval' required by Razorpay SDK and inline app scripts
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
        "https://cdnjs.cloudflare.com https://unpkg.com https://checkout.razorpay.com "
        "https://challenges.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob: https:; "
        "connect-src 'self' https://*.supabase.co https://lumberjack.razorpay.com https://api.razorpay.com "
        "https://accounts.google.com https://oauth2.googleapis.com "
        "https://challenges.cloudflare.com https://*.ingest.sentry.io; "
        "frame-src 'self' https://api.razorpay.com https://challenges.cloudflare.com; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "manifest-src 'self'; "
        "worker-src 'self'"
    )

    # 2. HSTS — force HTTPS for 1 year
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    # 3. X-Frame-Options — prevent clickjacking
    response.headers["X-Frame-Options"] = "DENY"

    # 4. X-Content-Type-Options — prevent MIME sniffing
    response.headers["X-Content-Type-Options"] = "nosniff"

    # 5. Referrer-Policy — limit referrer info leakage
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    # 6. Permissions-Policy — disable unused browser features
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

    # 7. Cross-Origin headers — block Spectre-class side-channel attacks
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"

    # 8. Cache-Control — prevent caching of HTML pages with sensitive content
    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"

    return response


# ============== Suspicious Request Blocker ==============
_BLOCKED_PATHS = {
    "/.env", "/.git", "/.git/config", "/.git/HEAD",
    "/wp-admin", "/wp-login.php", "/wp-content",
    "/phpmyadmin", "/phpMyAdmin", "/pma",
    "/admin.php", "/xmlrpc.php", "/wp-cron.php",
    "/.aws/credentials", "/.docker/config.json",
    "/api/v1", "/actuator", "/actuator/health",
    "/server-status", "/.htaccess", "/web.config",
    "/.DS_Store", "/config.json", "/.vscode",
}

@app.middleware("http")
async def block_suspicious_requests(request: Request, call_next):
    """Block common recon/scanner probes targeting known vulnerable paths."""
    path = request.url.path.rstrip("/")
    if path in _BLOCKED_PATHS or path.startswith("/.env") or path.startswith("/.git"):
        logger.warning(f"Blocked suspicious request: {request.client.host} -> {path}")
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    return await call_next(request)


# ============== Request Size Limiter ==============
MAX_REQUEST_BODY_MB = 100  # 100MB max for uploads

@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    """Reject excessively large request bodies to prevent DoS."""
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_REQUEST_BODY_MB * 1024 * 1024:
        return JSONResponse(status_code=413, content={"detail": "Request body too large"})
    return await call_next(request)


@app.on_event("shutdown")
def shutdown_event():
    """Clean up database connection pool on shutdown."""
    db.close_pool()
    logger.info("Application shutdown complete")

# Mount static files for JS modules and CSS
frontend_path = Path(__file__).parent.parent / "frontend"
if frontend_path.exists():
    app.mount("/js", StaticFiles(directory=frontend_path / "js"), name="js")
    app.mount("/css", StaticFiles(directory=frontend_path / "css"), name="css")
    app.mount("/icons", StaticFiles(directory=frontend_path / "icons"), name="icons")


def run_daily_cleanup():
    """
    Run cleanup of old jobs and files.
    Called on startup and scheduled to run at midnight IST.
    """
    try:
        cleanup_result = db.cleanup_old_jobs(retention_days=5)
        if cleanup_result["deleted_count"] > 0:
            logger.info(f"Daily cleanup: Deleted {cleanup_result['deleted_count']} jobs older than 5 days")
            
            # Delete files from Supabase Storage
            files_deleted = 0
            for file_url in cleanup_result.get("files_to_delete", []):
                if not file_url:
                    continue
                # Extract filename: could be a full URL or just a filename
                if file_url.startswith("http"):
                    # Strip query params first, then get the last path segment
                    from urllib.parse import urlparse
                    parsed = urlparse(file_url)
                    filename = parsed.path.split("/")[-1]
                else:
                    filename = file_url.split("/")[-1]
                
                if filename and storage.delete_file(filename):
                    files_deleted += 1
                else:
                    logger.warning(f"Failed to delete storage file: {filename} (from {file_url})")
            
            logger.info(f"  - Files deleted from Supabase: {files_deleted}")
            
            # Delete files from Google Drive
            drive_files = cleanup_result.get("drive_files_to_delete", [])
            if drive_files:
                from . import google_drive
                drive_deleted = 0
                # Group by user_id to batch lookups
                user_tokens = {}
                for user_id, drive_file_id in drive_files:
                    if user_id not in user_tokens:
                        user = db.get_user_by_id(user_id)
                        user_tokens[user_id] = user.get("google_refresh_token") if user else None
                    
                    token = user_tokens.get(user_id)
                    if token and google_drive.delete_file(token, drive_file_id):
                        drive_deleted += 1
                    else:
                        logger.warning(f"Failed to delete Drive file: {drive_file_id} (user={user_id})")
                
                logger.info(f"  - Files deleted from Google Drive: {drive_deleted}")
        
        # Also cleanup old archived jobs (1 day retention)
        archived_deleted = db.cleanup_old_archived_jobs(1)
        if archived_deleted > 0:
            logger.info(f"Cleaned up {archived_deleted} archived jobs older than 1 day")
            
        return cleanup_result
    except Exception as e:
        logger.error(f"Error during daily cleanup: {e}")
        return None


# Insecure default values that MUST be changed in production
_INSECURE_DEFAULTS = {
    "staff_password": ["xerox123", ""],
    "admin_password": ["admin123", ""],
    "jwt_secret_key": ["change-this-secret-key-in-production", ""],
}

# Initialize database on startup
@app.on_event("startup")
async def startup_event():
    """Initialize database, directories, and background scheduler on startup."""
    settings.ensure_directories()
    db.init_db()
    
    # ===== Security: Validate critical credentials are set =====
    if settings.staff_password in _INSECURE_DEFAULTS["staff_password"]:
        logger.error("SECURITY: STAFF_PASSWORD is not set or uses an insecure default! "
                     "Set a strong STAFF_PASSWORD in .env before deploying to production.")
    if settings.admin_password in _INSECURE_DEFAULTS["admin_password"]:
        logger.error("SECURITY: ADMIN_PASSWORD is not set or uses an insecure default! "
                     "Set a strong ADMIN_PASSWORD in .env before deploying to production.")
    if settings.jwt_secret_key in _INSECURE_DEFAULTS["jwt_secret_key"]:
        logger.error("SECURITY: JWT_SECRET_KEY is not set or uses an insecure default! "
                     "Generate a random secret: python -c 'import secrets; print(secrets.token_hex(32))'")
    
    # Run cleanup immediately on startup
    run_daily_cleanup()
    
    # Schedule daily cleanup at midnight IST (18:30 UTC previous day)
    # IST is UTC+5:30, so midnight IST = 18:30 UTC previous day
    scheduler.add_job(
        run_daily_cleanup,
        CronTrigger(hour=18, minute=30),  # 18:30 UTC = 00:00 IST
        id="daily_cleanup_midnight",
        replace_existing=True,
        name="Daily cleanup at midnight IST"
    )
    
    # Also run cleanup every 4 hours as a safety net
    scheduler.add_job(
        run_daily_cleanup,
        CronTrigger(hour="*/4"),  # Every 4 hours
        id="periodic_cleanup",
        replace_existing=True,
        name="Periodic cleanup every 4 hours"
    )
    
    # Start the scheduler
    scheduler.start()
    logger.info("Background scheduler started (cleanup at midnight IST + every 4 hours)")
    
    logger.info("Application started successfully")


# Health check endpoint for Render
@app.get("/health")
async def health_check():
    """Simple health check endpoint for deployment platforms."""
    return {"status": "healthy"}


# ============== Shop Status API ==============

def _check_shop_open():
    """Helper to check if shop is open. Raises 403 if closed."""
    shop = db.get_shop_status()
    if shop["status"] == "closed":
        msg = shop.get("message") or "Shop is currently closed. Please try again later."
        raise HTTPException(status_code=403, detail=msg)


@app.get("/api/shop/status")
async def get_shop_status():
    """Get current shop open/closed status. Public endpoint."""
    return db.get_shop_status()


@app.get("/api/cron/cleanup")
async def cron_cleanup(request: Request):
    """
    External cron endpoint to trigger cleanup.
    Protected by X-Cron-Secret header.
    """
    # Verify cron secret — require authentication if no secret is configured
    cron_secret = settings.cron_secret
    if cron_secret:
        provided = request.headers.get("x-cron-secret", "")
        if not secrets.compare_digest(provided, cron_secret):
            raise HTTPException(status_code=403, detail="Forbidden")
    else:
        # No cron secret configured — require staff auth as fallback (SEC-09)
        logger.warning("CRON_SECRET not set — cron endpoint requires staff Basic auth as fallback")
        from fastapi.security import HTTPBasic, HTTPBasicCredentials
        creds_header = request.headers.get("authorization", "")
        if not creds_header:
            raise HTTPException(status_code=403, detail="Cron secret or staff credentials required")
    from datetime import timedelta
    
    now_ist = db.get_ist_now()
    cutoff = now_ist - timedelta(days=1)
    
    result = {
        "status": "ok",
        "current_time_ist": now_ist.isoformat(),
        "cutoff_time": cutoff.isoformat(),
        "jobs_found": 0,
        "jobs_deleted": 0,
        "files_deleted": 0,
        "errors": []
    }
    
    try:
        # Step 1: Find jobs with files older than cutoff
        with db.get_connection() as conn:
            cursor = conn.cursor(cursor_factory=db.psycopg2.extras.RealDictCursor)
            cursor.execute(
                "SELECT id, ticket, filepath, processed_filepath FROM jobs WHERE created_at < %s AND (COALESCE(filepath, '') != '' OR processed_filepath IS NOT NULL)",
                (cutoff.isoformat(),)
            )
            jobs = cursor.fetchall()
            result["jobs_found"] = len(jobs)
            
            # Step 2: Collect file paths to delete
            files_to_delete = []
            job_ids = []
            for job in jobs:
                job_ids.append(job['id'])
                if job.get('filepath'):
                    files_to_delete.append(job['filepath'])
                if job.get('processed_filepath'):
                    files_to_delete.append(job['processed_filepath'])
            
            # Step 3: Preserve payment records (audit trail), clear file refs only
            if job_ids:
                # Detach payments from guest jobs before deleting guest records
                cursor.execute(
                    """UPDATE payments SET job_id = NULL 
                       WHERE job_id IN (SELECT id FROM jobs WHERE user_id IS NULL AND created_at < %s)""",
                    (cutoff.isoformat(),)
                )
                result["payments_detached"] = cursor.rowcount
                
                cursor.execute(
                    "UPDATE jobs SET filepath = '', processed_filepath = NULL, status = 'expired' WHERE id = ANY(%s)",
                    (job_ids,)
                )
                result["jobs_cleaned"] = cursor.rowcount
                conn.commit()
        
        # Step 4: Delete files from Supabase
        from urllib.parse import urlparse
        for file_url in files_to_delete:
            try:
                if not file_url:
                    continue
                if file_url.startswith("http"):
                    parsed = urlparse(file_url)
                    filename = parsed.path.split("/")[-1]
                else:
                    filename = file_url.split("/")[-1]
                if filename and storage.delete_file(filename):
                    result["files_deleted"] += 1
            except Exception as e:
                result["errors"].append(f"File delete error: {str(e)}")
        
        # Step 5: Clean up archived jobs
        try:
            archived = db.cleanup_old_archived_jobs(1)
            result["archived_deleted"] = archived
        except Exception as e:
            result["errors"].append(f"Archive cleanup error: {str(e)}")
        
        # Step 6: Clean up orphaned files in Supabase Storage
        # List all storage files and delete any not referenced by current DB jobs
        try:
            storage_cleanup = storage.delete_orphaned_files(db)
            result["storage_cleanup"] = storage_cleanup
        except Exception as e:
            result["errors"].append(f"Storage cleanup error: {str(e)}")
            
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        import traceback
        result["traceback"] = traceback.format_exc()
    
    return result


@app.on_event("shutdown")
async def shutdown_event():
    """Shutdown the background scheduler gracefully."""
    scheduler.shutdown(wait=False)
    logger.info("Background scheduler stopped")


def get_disk_free_space() -> int:
    """Get free disk space in bytes for the storage directory."""
    stat = os.statvfs(settings.storage_dir)
    return stat.f_bavail * stat.f_frsize


def check_disk_space() -> bool:
    """Check if there's enough disk space for uploads."""
    return get_disk_free_space() > settings.disk_low_threshold_bytes


def validate_passphrase(passphrase: Optional[str]) -> bool:
    """Validate the upload passphrase if one is configured."""
    if not settings.upload_passphrase:
        return True  # No passphrase required
    return passphrase == settings.upload_passphrase


def get_file_extension(filename: str) -> str:
    """Get lowercase file extension."""
    return Path(filename).suffix.lower()


def is_allowed_file(filename: str) -> bool:
    """Check if file type is allowed (PDF, images, office docs)."""
    allowed_extensions = {
        # Documents
        ".pdf", ".doc", ".docx", ".ppt", ".pptx",
        ".odt", ".odp", ".txt", ".rtf",
        # Images
        ".jpg", ".jpeg", ".png", ".tiff",
    }
    return get_file_extension(filename) in allowed_extensions


# ============== Frontend Pages ==============

@app.get("/", response_class=HTMLResponse)
async def upload_page(request: Request):
    """Serve the customer upload page."""
    from fastapi.responses import RedirectResponse
    from . import auth
    
    # Check for authentication (allow guest mode)
    guest = request.query_params.get("guest")
    token = request.cookies.get("access_token")
    if not guest and (not token or not auth.decode_access_token(token)):
        return RedirectResponse(url="/login")
        
    frontend_path = Path(__file__).parent.parent / "frontend" / "index.html"
    if frontend_path.exists():
        return HTMLResponse(content=frontend_path.read_text(), status_code=200)
    return HTMLResponse(content="<h1>Upload page not found</h1>", status_code=404)


from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets

security = HTTPBasic()

def verify_staff_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, settings.staff_username)
    correct_password = secrets.compare_digest(credentials.password, settings.staff_password)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

@app.get("/staff", response_class=HTMLResponse)
async def staff_page(request: Request, key: Optional[str] = None):
    """Serve the staff management UI — hidden behind access key."""
    # Check access: query param ?key=xxx or cookie
    access_key = settings.staff_access_key
    if access_key:
        cookie_key = request.cookies.get("staff_access")
        if key != access_key and cookie_key != access_key:
            return HTMLResponse(content="<h1>404 — Page not found</h1>", status_code=404)
    
    frontend_path = Path(__file__).parent.parent / "frontend" / "staff.html"
    if frontend_path.exists():
        response = HTMLResponse(content=frontend_path.read_text(), status_code=200)
        # Set cookie so they don't need the key every time (30 days)
        if key == access_key and access_key:
            response.set_cookie("staff_access", access_key, max_age=30*24*3600, httponly=True, samesite="lax")
        return response
    return HTMLResponse(content="<h1>404 — Page not found</h1>", status_code=404)


@app.post("/api/staff/login")
async def staff_login(request: Request):
    """Validate staff credentials. Returns success if valid, 401 if not."""
    try:
        body = await request.json()
        username = body.get("username", "")
        password = body.get("password", "")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request")

    correct_username = secrets.compare_digest(username, settings.staff_username)
    correct_password = secrets.compare_digest(password, settings.staff_password)

    if not (correct_username and correct_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return {"status": "ok", "message": "Login successful"}


class ShopStatusRequest(BaseModel):
    status: str  # 'open' or 'closed'
    message: Optional[str] = ""


@app.post("/api/shop/status")
async def set_shop_status(
    data: ShopStatusRequest,
    username: str = Depends(verify_staff_credentials)
):
    """Toggle shop open/closed status. Staff-only (Basic Auth)."""
    if data.status not in ("open", "closed"):
        raise HTTPException(status_code=400, detail="Status must be 'open' or 'closed'")
    
    success = db.set_shop_status(data.status, data.message or "")
    if not success:
        raise HTTPException(status_code=500, detail="Failed to update shop status")
    
    logger.info(f"Shop status changed to '{data.status}' by {username}")
    return {"status": data.status, "message": data.message}


@app.get("/pricing.json")
async def get_pricing():
    """Serve the pricing configuration."""
    pricing_path = Path(__file__).parent.parent / "frontend" / "pricing.json"
    if pricing_path.exists():
        return JSONResponse(content=json.loads(pricing_path.read_text()))
    return JSONResponse(content={}, status_code=404)


@app.get("/manifest.json")
async def get_manifest():
    """Serve the PWA manifest."""
    manifest_path = Path(__file__).parent.parent / "frontend" / "manifest.json"
    if manifest_path.exists():
        return JSONResponse(content=json.loads(manifest_path.read_text()))
    return JSONResponse(content={}, status_code=404)


@app.get("/staff-manifest.json")
async def get_staff_manifest():
    """Serve the staff-specific PWA manifest (start_url = /staff)."""
    manifest_path = Path(__file__).parent.parent / "frontend" / "staff-manifest.json"
    if manifest_path.exists():
        return JSONResponse(content=json.loads(manifest_path.read_text()))
    return JSONResponse(content={}, status_code=404)


@app.get("/service-worker.js")
async def get_service_worker():
    """Serve the service worker."""
    sw_path = Path(__file__).parent.parent / "frontend" / "service-worker.js"
    if sw_path.exists():
        return HTMLResponse(
            content=sw_path.read_text(),
            media_type="application/javascript"
        )
    return HTMLResponse(content="", status_code=404)


@app.get("/.well-known/assetlinks.json")
async def get_asset_links():
    """Serve Digital Asset Links for TWA (Trusted Web Activity) verification.
    
    This file proves to Chrome that the Android app (published on Play Store)
    owns this website, enabling the app to open without the browser URL bar.
    """
    asset_links_path = Path(__file__).parent.parent / "frontend" / ".well-known" / "assetlinks.json"
    if asset_links_path.exists():
        return JSONResponse(
            content=json.loads(asset_links_path.read_text()),
            headers={"Access-Control-Allow-Origin": "*"}
        )
    return JSONResponse(content=[], status_code=404)


@app.get("/favicon.ico")
async def get_favicon():
    """Serve the favicon from PWA icons."""
    icon_path = Path(__file__).parent.parent / "frontend" / "icons" / "icon-192.png"
    if icon_path.exists():
        return FileResponse(icon_path, media_type="image/png")
    return HTMLResponse(content="", status_code=404)


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """Serve the login/signup page."""
    frontend_path = Path(__file__).parent.parent / "frontend" / "login.html"
    if frontend_path.exists():
        return HTMLResponse(content=frontend_path.read_text(), status_code=200)
    return HTMLResponse(content="<h1>Login page not found</h1>", status_code=404)


@app.get("/history", response_class=HTMLResponse)
@app.get("/profile", response_class=HTMLResponse)
async def history_page():
    """Serve the user history/profile page."""
    frontend_path = Path(__file__).parent.parent / "frontend" / "history.html"
    if frontend_path.exists():
        return HTMLResponse(content=frontend_path.read_text(), status_code=200)
    return HTMLResponse(content="<h1>History page not found</h1>", status_code=404)


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page():
    """Serve the privacy policy page."""
    frontend_path = Path(__file__).parent.parent / "frontend" / "privacy.html"
    if frontend_path.exists():
        return HTMLResponse(content=frontend_path.read_text(), status_code=200)
    return HTMLResponse(content="<h1>Privacy policy not found</h1>", status_code=404)


@app.get("/terms", response_class=HTMLResponse)
async def terms_page():
    """Serve the terms of service page."""
    frontend_path = Path(__file__).parent.parent / "frontend" / "terms.html"
    if frontend_path.exists():
        return HTMLResponse(content=frontend_path.read_text(), status_code=200)
    return HTMLResponse(content="<h1>Terms of service not found</h1>", status_code=404)


@app.get("/developer", response_class=HTMLResponse)
async def developer_page():
    """Serve the developer info page."""
    frontend_path = Path(__file__).parent.parent / "frontend" / "developer.html"
    if frontend_path.exists():
        return HTMLResponse(content=frontend_path.read_text(), status_code=200)
    return HTMLResponse(content="<h1>Developer page not found</h1>", status_code=404)


@app.get("/contact", response_class=HTMLResponse)
async def contact_page():
    """Serve the contact us page."""
    frontend_path = Path(__file__).parent.parent / "frontend" / "contact.html"
    if frontend_path.exists():
        return HTMLResponse(content=frontend_path.read_text(), status_code=200)
    return HTMLResponse(content="<h1>Contact page not found</h1>", status_code=404)

# ============== Authentication API ==============

from . import auth
from . import notifications
import random, time

# In-memory OTP store: { email: { "otp": "123456", "expires": timestamp, "name": "...", "password": "..." } }
_otp_store: dict = {}

class OTPRequest(BaseModel):
    email: str
    password: str
    name: Optional[str] = None
    turnstile_token: Optional[str] = None

class OTPVerify(BaseModel):
    email: str
    otp: str

class ForgotPasswordRequest(BaseModel):
    email: str
    turnstile_token: Optional[str] = None

class ResetPasswordRequest(BaseModel):
    email: str
    otp: str
    new_password: str

def _cleanup_expired_otps():
    """Remove expired OTP entries."""
    now = time.time()
    expired = [e for e, d in _otp_store.items() if d["expires"] < now]
    for e in expired:
        del _otp_store[e]

def _send_otp_email(email: str, otp: str) -> bool:
    """Send OTP verification email."""
    subject = "Your Verification Code - Xerox Upload"
    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:400px;margin:0 auto;padding:30px;background:#111;color:#fff;border-radius:16px;text-align:center;">
        <h2 style="margin-bottom:8px;">Verify Your Email</h2>
        <p style="color:#999;font-size:14px;margin-bottom:24px;">Enter this code to complete registration</p>
        <div style="font-size:36px;font-weight:bold;letter-spacing:8px;padding:20px;background:#1a1a1a;border-radius:12px;margin-bottom:20px;">{otp}</div>
        <p style="color:#666;font-size:12px;">This code expires in 10 minutes.<br>If you didn't request this, ignore this email.</p>
    </div>
    """
    return notifications.send_email(email, subject, html_body, f"Your verification code is: {otp}")


@app.post("/api/auth/send-otp")
@limiter.limit("10/minute")
async def send_otp(request: Request, data: OTPRequest):
    """
    Send OTP to email for registration verification.
    Does NOT create the account yet.
    """
    try:
        # Verify Turnstile token
        if not await verify_turnstile(data.turnstile_token, get_remote_address(request)):
            raise HTTPException(status_code=400, detail="Human verification failed. Please try again.")

        _cleanup_expired_otps()

        # Check if email already registered
        existing = db.get_user_by_email(data.email)
        if existing:
            raise HTTPException(status_code=400, detail="Email already registered. Try signing in instead.")

        # Rate-limit: don't let same email spam OTPs (must wait 60s)
        if data.email in _otp_store:
            remaining = _otp_store[data.email]["expires"] - time.time()
            if remaining > 540:  # sent less than 60s ago (600 - 60 = 540)
                raise HTTPException(status_code=429, detail="Please wait before requesting a new code")

        # Validate password upfront
        if len(data.password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        # Password complexity: require at least 1 letter and 1 digit (SEC-06)
        import re
        if not re.search(r'[A-Za-z]', data.password) or not re.search(r'[0-9]', data.password):
            raise HTTPException(status_code=400, detail="Password must contain at least one letter and one number")

        # Validate name — letters and spaces only
        if data.name and not re.match(r'^[A-Za-z\s]+$', data.name.strip()):
            raise HTTPException(status_code=400, detail="Name must contain only letters and spaces")

        # Generate 6-digit OTP
        otp = f"{random.randint(100000, 999999)}"

        # Store OTP with registration data (expires in 10 min)
        # Hash password immediately to avoid storing plaintext in memory (SEC-05)
        _otp_store[data.email] = {
            "otp": otp,
            "expires": time.time() + 600,
            "password_hash": auth.hash_password(data.password),
            "name": data.name
        }

        # Send email in a thread to avoid blocking the async event loop
        import asyncio
        sent = await asyncio.to_thread(_send_otp_email, data.email, otp)
        if not sent:
            del _otp_store[data.email]
            raise HTTPException(status_code=500, detail="Failed to send verification email. Please check your email address.")

        logger.info(f"OTP sent to {data.email}")
        return {"status": "ok", "message": "Verification code sent to your email"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Send OTP error: {e}")
        raise HTTPException(status_code=500, detail="Failed to send verification code")

@app.post("/api/auth/verify-otp")
async def verify_otp_and_register(data: OTPVerify):
    """
    Verify OTP and create user account.
    This is the ONLY way to create an email/password account (no direct registration).
    After account creation, frontend should redirect to Google OAuth for Drive permission.
    """
    try:
        _cleanup_expired_otps()

        # Check if OTP exists for this email
        stored = _otp_store.get(data.email)
        if not stored:
            raise HTTPException(status_code=400, detail="No verification code found. Please request a new one.")

        # Verify OTP — use constant-time comparison to prevent timing attacks (SEC-03)
        if not secrets.compare_digest(stored["otp"], data.otp):
            raise HTTPException(status_code=400, detail="Invalid verification code")

        if time.time() > stored["expires"]:
            del _otp_store[data.email]
            raise HTTPException(status_code=400, detail="Verification code expired. Please request a new one.")

        # OTP valid — create the account using the pre-hashed password (SEC-05)
        password_hash = stored["password_hash"]
        name = stored.get("name")

        # Check if email already registered (race condition guard)
        existing = db.get_user_by_email(data.email)
        if existing:
            del _otp_store[data.email]
            raise HTTPException(status_code=400, detail="Email already registered. Try signing in instead.")

        # Create user directly with pre-hashed password (skip double-hashing)
        user = db.create_user(
            email=data.email,
            password_hash=password_hash,
            name=name
        )

        if not user:
            raise HTTPException(status_code=500, detail="Failed to create account")

        # Clean up OTP store
        del _otp_store[data.email]

        # Generate token
        access_token = auth.create_access_token(data={"sub": str(user["id"])})

        logger.info(f"User registered via OTP: {data.email}")

        # Set cookie for seamless Google OAuth redirect
        from fastapi.responses import JSONResponse
        response = JSONResponse(content={
            "access_token": access_token,
            "token_type": "bearer",
            "user": {
                "id": user["id"],
                "email": user["email"],
                "name": user.get("name")
            },
            "has_drive": False,  # New account — no Drive yet, frontend should redirect to Google OAuth
            "needs_drive_permission": True
        })
        response.set_cookie(
            key="access_token",
            value=access_token,
            httponly=True,
            samesite="lax",
            secure=True,
            max_age=settings.access_token_expire_minutes * 60
        )
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"OTP verification error: {type(e).__name__}: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Verification failed. Please try again.")


@app.post("/api/auth/forgot-password")
@limiter.limit("5/minute")
async def forgot_password(request: Request, data: ForgotPasswordRequest):
    """
    Send OTP for password reset. User must already have an account.
    """
    try:
        # Verify Turnstile token
        if not await verify_turnstile(data.turnstile_token, get_remote_address(request)):
            raise HTTPException(status_code=400, detail="Human verification failed. Please try again.")

        _cleanup_expired_otps()

        # Check if user exists — use generic message to prevent email enumeration (SEC-13)
        user = db.get_user_by_email(data.email)
        if not user or not user.get("password_hash"):
            # Don't reveal whether account exists or not
            logger.info(f"Password reset requested for unknown/OAuth email: {data.email}")
            return {"status": "ok", "message": "If an account exists with this email, a reset code has been sent."}

        # Rate-limit
        reset_key = f"reset_{data.email}"
        if reset_key in _otp_store:
            remaining = _otp_store[reset_key]["expires"] - time.time()
            if remaining > 540:
                raise HTTPException(status_code=429, detail="Please wait before requesting a new code")

        # Generate OTP
        otp = f"{random.randint(100000, 999999)}"
        _otp_store[reset_key] = {
            "otp": otp,
            "expires": time.time() + 600,
            "user_id": user["id"]
        }

        # Send email
        import asyncio
        sent = await asyncio.to_thread(_send_otp_email, data.email, otp)
        if not sent:
            del _otp_store[reset_key]
            raise HTTPException(status_code=500, detail="Failed to send reset code")

        logger.info(f"Password reset OTP sent to {data.email}")
        return {"status": "ok", "message": "Reset code sent to your email"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Forgot password error: {e}")
        raise HTTPException(status_code=500, detail="Failed to send reset code")


@app.post("/api/auth/reset-password")
async def reset_password(data: ResetPasswordRequest):
    """
    Verify OTP and reset user password.
    """
    try:
        _cleanup_expired_otps()

        reset_key = f"reset_{data.email}"
        stored = _otp_store.get(reset_key)
        if not stored:
            raise HTTPException(status_code=400, detail="No reset code found. Please request a new one.")

        # Constant-time comparison to prevent timing attacks (SEC-03)
        if not secrets.compare_digest(stored["otp"], data.otp):
            raise HTTPException(status_code=400, detail="Invalid reset code")

        if time.time() > stored["expires"]:
            del _otp_store[reset_key]
            raise HTTPException(status_code=400, detail="Reset code expired. Please request a new one.")

        # Validate new password
        if len(data.new_password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

        # Update password
        password_hash = auth.hash_password(data.new_password)
        db.update_user_password(stored["user_id"], password_hash)

        # Clean up
        del _otp_store[reset_key]

        logger.info(f"Password reset for {data.email}")
        return {"status": "ok", "message": "Password reset successfully. You can now sign in."}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Reset password error: {e}")
        raise HTTPException(status_code=500, detail="Failed to reset password")


@app.post("/api/auth/login")
@limiter.limit("10/minute")
async def login(request: Request, user_data: auth.UserLogin, response: Response):
    """
    Login with email and password.
    Protected by rate limiting and brute-force lockout.
    """
    client_ip = get_remote_address(request)
    _check_login_lockout(client_ip)
    
    # Verify Turnstile token
    if not await verify_turnstile(user_data.turnstile_token, client_ip):
        raise HTTPException(status_code=400, detail="Human verification failed. Please try again.")
    
    user = auth.authenticate_user(user_data.email, user_data.password)
    
    if not user:
        _record_failed_login(client_ip)
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password"
        )
    
    _clear_login_attempts(client_ip)
    
    # Update last login
    db.update_user_last_login(user["id"])
    
    # Generate token
    access_token = auth.create_access_token(data={"sub": str(user["id"])})
    
    # Set cookie
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        samesite="lax",
        secure=True,
        max_age=settings.access_token_expire_minutes * 60
    )
    
    logger.info(f"User logged in: {user_data.email}")
    
    has_drive = bool(user.get("google_refresh_token"))
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user.get("name")
        },
        "has_drive": has_drive,
        "needs_drive_permission": not has_drive
    }


@app.post("/api/auth/google")
async def google_login_legacy(request: auth.GoogleAuthRequest, response: Response):
    """
    Login or register with Google OAuth (legacy ID token method).
    Kept for backwards compatibility but new flow uses redirect-based auth.
    """
    # Verify Google token
    google_user = auth.verify_google_token(request.token)
    
    if not google_user:
        raise HTTPException(status_code=401, detail="Invalid Google token")
    
    # Get or create user
    user = auth.get_or_create_google_user(
        google_id=google_user["sub"],
        email=google_user["email"],
        name=google_user.get("name")
    )
    
    if not user:
        raise HTTPException(status_code=500, detail="Failed to create user")
    
    # Generate token
    access_token = auth.create_access_token(data={"sub": str(user["id"])})
    
    # Set cookie
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        samesite="lax",
        secure=True,
        max_age=settings.access_token_expire_minutes * 60
    )
    
    logger.info(f"User logged in via Google (legacy): {google_user['email']}")
    
    return auth.Token(
        access_token=access_token,
        user={
            "id": user["id"],
            "email": user["email"],
            "name": user.get("name")
        }
    )


@app.get("/api/auth/google/login")
async def google_login_redirect(request: Request):
    """
    Redirect to Google OAuth consent screen.
    Requests drive.file scope for Google Drive storage.
    Smart consent: skips permissions screen for returning users who already granted access.
    """
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=500, detail="Google OAuth not configured")
    
    # Build callback URL based on the current request
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    redirect_uri = f"{scheme}://{host}/api/auth/google/callback"
    
    # Check if this user already has a refresh token (returning user)
    # If they do, no need to force consent — just show account picker
    force_consent = True  # Default: force consent for new users
    try:
        current_user = await auth.get_current_user_optional(request)
        if current_user and current_user.get("google_refresh_token"):
            force_consent = False  # Returning user with existing Drive access
    except Exception:
        pass  # If we can't check, default to force consent
    
    auth_url = auth.build_google_auth_url(redirect_uri, force_consent=force_consent)
    
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=auth_url)


@app.get("/api/auth/google/callback")
async def google_auth_callback(request: Request, code: str = None, error: str = None):
    """
    Handle Google OAuth callback after user consent.
    Exchanges auth code for tokens, stores refresh token, redirects to home.
    """
    from fastapi.responses import RedirectResponse
    
    if error:
        logger.warning(f"Google OAuth error: {error}")
        return RedirectResponse(url="/login?error=google_auth_failed")
    
    if not code:
        return RedirectResponse(url="/login?error=no_auth_code")
    
    # Build the same redirect_uri used in the login request
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    redirect_uri = f"{scheme}://{host}/api/auth/google/callback"
    
    # Exchange auth code for tokens + user info
    google_data = auth.handle_google_callback(code, redirect_uri)
    
    if not google_data:
        logger.error("Failed to exchange Google auth code")
        return RedirectResponse(url="/login?error=google_auth_failed")
    
    # Check if user granted Drive access (granular consent may have it unchecked)
    granted_scope = google_data.get("scope", "")
    has_drive_scope = "drive.file" in granted_scope
    
    if not has_drive_scope:
        logger.warning(f"User {google_data.get('email')} did not grant Drive permission. "
                       f"Granted scopes: {granted_scope}")
        return RedirectResponse(url="/login?error=drive_permission_required")
    
    # Get or create user with refresh token
    # Note: refresh_token may be None for returning users who used select_account prompt
    # In that case, the existing stored refresh token is preserved
    user = auth.get_or_create_google_user(
        google_id=google_data["sub"],
        email=google_data["email"],
        name=google_data.get("name"),
        refresh_token=google_data.get("refresh_token")
    )
    
    if not user:
        return RedirectResponse(url="/login?error=user_creation_failed")
    
    # Generate JWT
    access_token = auth.create_access_token(data={"sub": str(user["id"])})
    
    logger.info(f"User logged in via Google (Drive): {google_data['email']}, has_refresh_token={bool(google_data.get('refresh_token'))}")
    
    # Use JS-based redirect to prevent OAuth History Pollution.
    # window.location.replace() removes the callback URL from browser history,
    # so pressing back won't go to Google's OAuth pages.
    import json
    user_json = json.dumps({
        "id": user["id"],
        "email": user["email"],
        "name": user.get("name", ""),
    })
    html_content = f"""<!DOCTYPE html><html><head><title>Redirecting...</title></head><body>
    <script>
        localStorage.setItem('access_token', '{access_token}');
        localStorage.setItem('user', '{user_json}');
        window.location.replace('/');
    </script>
    <noscript><a href="/">Click here to continue</a></noscript>
    </body></html>"""
    
    from fastapi.responses import HTMLResponse
    response = HTMLResponse(content=html_content, status_code=200)
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        samesite="lax",
        secure=True,
        max_age=settings.access_token_expire_minutes * 60
    )
    
    return response


@app.post("/api/auth/logout")
async def logout(response: Response):
    """Logout user by clearing access token cookie."""
    response.delete_cookie(key="access_token")
    return {"status": "ok", "message": "Logged out successfully"}


@app.get("/api/auth/check-cookie")
async def check_auth_cookie(request: Request):
    """Check if the access_token cookie is present and valid."""
    from . import auth
    token = request.cookies.get("access_token")
    if not token or not auth.decode_access_token(token):
        return JSONResponse(content={"authenticated": False}, status_code=401)
    return JSONResponse(content={"authenticated": True}, status_code=200)


@app.get("/api/auth/me")
async def get_current_user_profile(current_user: dict = Depends(auth.get_current_user)):
    """
    Get the current authenticated user's profile.
    Includes has_drive flag indicating if user has Google Drive access.
    """
    return {
        "id": current_user["id"],
        "email": current_user["email"],
        "name": current_user.get("name"),
        "created_at": current_user.get("created_at"),
        "has_drive": bool(current_user.get("google_refresh_token"))
    }


@app.get("/api/user/jobs")
async def get_user_jobs(
    limit: int = 50,
    offset: int = 0,
    current_user: dict = Depends(auth.get_current_user)
):
    """
    Get the current user's job/transaction history.
    Returns paginated list of jobs with total count.
    """
    jobs = db.get_user_jobs(current_user["id"], limit=limit, offset=offset)
    total = db.get_user_job_count(current_user["id"])
    total_spent = db.get_user_total_spent(current_user["id"])
    return {"jobs": jobs, "count": len(jobs), "total": total, "total_spent_cents": total_spent}


@app.get("/api/config/google-client-id")
async def get_google_client_id():
    """Return Google Client ID for frontend OAuth initialization."""
    return {"client_id": settings.google_client_id or None}


# ============== Direct Upload API (Faster) ==============

class DirectUploadRequest(BaseModel):
    """Request to get a signed URL for direct upload."""
    filename: str
    content_type: str = "application/octet-stream"
    sender: str
    phone: Optional[str] = None
    passphrase: Optional[str] = None
    job_metadata: Optional[str] = None
    parent_ticket: Optional[str] = None  # For bulk orders


class DirectUploadConfirmRequest(BaseModel):
    """Confirm a direct upload after file is uploaded to Supabase."""
    ticket: str
    filename: str
    storage_filename: str
    file_size: int
    sender: str
    phone: Optional[str] = None
    job_metadata: Optional[str] = None


# ── Page Count Endpoint (pre-cart) ──────────────────────────────────
@app.post("/api/count-pages")
@limiter.limit("30/hour")
async def count_pages(request: Request, file: UploadFile = File(...)):
    """
    Count exact pages in an office document before adding to cart.
    Uses Google Drive to convert the file and count pages.
    Returns: { "pages": N } or { "pages": null } if counting fails.
    """
    from . import docx_pages
    
    if not file.filename:
        return {"pages": None}
    
    extension = get_file_extension(file.filename).lower()
    office_extensions = {'.doc', '.docx', '.ppt', '.pptx',
                         '.odt', '.odp', '.txt', '.rtf'}
    
    if extension not in office_extensions:
        return {"pages": None}
    
    try:
        # Read file bytes (small files only — office docs are usually < 10MB)
        file_bytes = await file.read()
        
        if not file_bytes:
            return {"pages": None}
        
        # Count pages via Google Drive conversion
        page_count = docx_pages.count_pages_from_bytes(file_bytes, file.filename)
        
        logger.info(f"Pre-cart page count: {file.filename} = {page_count} pages")
        return {"pages": page_count}
        
    except Exception as e:
        logger.error(f"Pre-cart page count error: {e}")
        return {"pages": None}

@app.post("/api/upload/get-signed-url")
@limiter.limit(f"{settings.rate_limit_per_ip_per_hour}/hour")
async def get_signed_upload_url(request: Request, data: DirectUploadRequest, authorization: Optional[str] = Header(None)):
    """
    Get a signed URL for direct upload to Supabase Storage.
    For Google Drive users, returns upload_mode='drive' so frontend uses /api/upload/drive instead.
    
    Flow (Supabase - guest/non-Drive users):
    1. Frontend calls this endpoint with file info
    2. Backend generates ticket and signed URL
    3. Frontend uploads directly to Supabase
    4. Frontend calls /api/upload/confirm to finalize
    
    Flow (Drive - authenticated Google users):
    1. Frontend calls this endpoint with file info
    2. Backend returns ticket + upload_mode='drive'
    3. Frontend uploads file via /api/upload/drive (multipart)
    """
    # Block uploads when shop is closed
    _check_shop_open()
    
    # Validate passphrase
    if not validate_passphrase(data.passphrase):
        raise HTTPException(status_code=403, detail="Invalid passphrase")
    
    # Get current user if authenticated
    current_user = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        payload = auth.decode_access_token(token)
        if payload and payload.get("sub"):
            current_user = db.get_user_by_id(int(payload["sub"]))
    
    # Also check cookie if no header auth
    if not current_user:
        cookie_token = request.cookies.get("access_token")
        if cookie_token:
            payload = auth.decode_access_token(cookie_token)
            if payload and payload.get("sub"):
                current_user = db.get_user_by_id(int(payload["sub"]))
    
    # Update last activity time for admin dashboard tracking
    if current_user:
        try:
            db.update_user_last_login(current_user["id"])
        except Exception:
            pass  # Non-critical — don't block the upload
    
    # Generate ticket (handle bulk order sub-items)
    if data.parent_ticket:
        try:
            existing_group = db.get_jobs_by_group_ticket(data.parent_ticket)
            if not existing_group:
                ticket = f"{data.parent_ticket}-1"
            else:
                max_suffix = 1
                for t in existing_group:
                    if t == data.parent_ticket:
                        continue
                    parts = t.split('-')
                    if len(parts) > 1 and parts[-1].isdigit():
                        suffix = int(parts[-1])
                        if suffix > max_suffix:
                            max_suffix = suffix
                ticket = f"{data.parent_ticket}-{max_suffix + 1}"
        except Exception as e:
            logger.error(f"Error generating suffix ticket: {e}")
            ticket = db.generate_ticket()
    else:
        ticket = db.generate_ticket()
    
    # Check if user has Google Drive access
    if current_user and current_user.get("google_refresh_token"):
        # Drive user — frontend should upload via /api/upload/drive
        logger.info(f"Drive upload mode: ticket={ticket}, user={current_user['email']}")
        return {
            "status": "ok",
            "ticket": ticket,
            "upload_mode": "drive",
            "message": "Use /api/upload/drive to upload file"
        }
    
    # Standard Supabase flow
    import uuid
    extension = get_file_extension(data.filename)
    storage_filename = f"{ticket}_{uuid.uuid4().hex[:8]}{extension}"
    
    # Get signed upload URL from Supabase
    signed_url_data = storage.create_signed_upload_url(storage_filename)
    
    if not signed_url_data or not signed_url_data.get("signed_url"):
        raise HTTPException(status_code=500, detail="Failed to create upload URL")
    
    logger.info(f"Created signed upload URL: ticket={ticket}, file={data.filename}")
    
    return {
        "status": "ok",
        "ticket": ticket,
        "upload_mode": "supabase",
        "storage_filename": storage_filename,
        "signed_url": signed_url_data["signed_url"],
        "token": signed_url_data.get("token"),
        "path": signed_url_data.get("path"),
        "expires_in": 3600  # 1 hour
    }


@app.post("/api/upload/confirm")
@limiter.limit(f"{settings.rate_limit_per_ip_per_hour}/hour")
async def confirm_direct_upload(request: Request, data: DirectUploadConfirmRequest, authorization: Optional[str] = Header(None)):
    """Confirm a direct upload after file is uploaded to Supabase."""
    # Block uploads when shop is closed
    _check_shop_open()
    
    # Get current user if authenticated
    current_user = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        payload = auth.decode_access_token(token)
        if payload and payload.get("sub"):
            current_user = db.get_user_by_id(int(payload["sub"]))
    
    # Get public URL for the uploaded file
    storage_url = storage.get_public_url(data.storage_filename)
    
    # Parse job metadata
    metadata = {}
    if data.job_metadata:
        try:
            metadata = json.loads(data.job_metadata)
        except json.JSONDecodeError:
            pass
    
    # Create database entry with 'draft' status (not visible on staff dashboard until payment)
    job = db.create_job(
        ticket=data.ticket,
        sender=data.sender,
        phone=data.phone,
        filename=data.filename,
        filepath=storage_url,
        size_bytes=data.file_size,
        color_mode=metadata.get("color_mode", "bw"),
        duplex=metadata.get("duplex", False),
        paper_size=metadata.get("paper_size", "A4"),
        binding=metadata.get("binding", "none"),
        copies=metadata.get("copies", 1),
        selected_pages=json.dumps(metadata.get("page_list")) if metadata.get("page_list") else None,
        rotations=json.dumps(metadata.get("rotations")) if metadata.get("rotations") else None,
        client_estimate_cents=metadata.get("client_estimate_cents"),
        user_id=current_user["id"] if current_user else None,
        status="draft"
    )
    
    logger.info(f"Direct upload confirmed: ticket={data.ticket}, sender={data.sender}, size={data.file_size}")
    
    # Process PDF if pages are selected (download from Supabase, process, re-upload)
    page_list = metadata.get("page_list")
    rotations = metadata.get("rotations")
    file_extension = get_file_extension(data.filename)
    
    if page_list and file_extension == ".pdf":
        from . import pdf_processor
        import tempfile
        import requests
        
        try:
            # Download the original file from Supabase
            logger.info(f"Downloading file for processing: {storage_url}")
            response = requests.get(storage_url, timeout=60)
            if response.status_code == 200:
                file_data = response.content
                
                # Write to temp file for processing
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_in:
                    tmp_in.write(file_data)
                    tmp_in_path = tmp_in.name
                
                processed_filename = f"{data.ticket}_processed.pdf"
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_out:
                    tmp_out_path = tmp_out.name
                
                try:
                    rotation_dict = None
                    if rotations:
                        rotation_dict = {int(k): v for k, v in rotations.items()}
                    
                    if pdf_processor.extract_pages(tmp_in_path, tmp_out_path, page_list, rotation_dict):
                        # Upload processed file to Supabase
                        with open(tmp_out_path, 'rb') as f:
                            processed_data = f.read()
                        
                        processed_url = storage.upload_file(processed_data, processed_filename, "application/pdf")
                        if processed_url:
                            # Delete the original file from Supabase since we have the processed version
                            from urllib.parse import urlparse
                            original_parsed = urlparse(storage_url)
                            original_filename = original_parsed.path.split("/")[-1]
                            if original_filename:
                                storage.delete_file(original_filename)
                                logger.info(f"Deleted original file after processing: {original_filename}")
                            db.update_job_processing_status(data.ticket, "processed", processed_url)
                            logger.info(f"PDF processed and uploaded: ticket={data.ticket}, pages={len(page_list)}")
                        else:
                            logger.warning(f"Failed to upload processed PDF for ticket={data.ticket}")
                    else:
                        logger.warning(f"PDF processing failed for ticket={data.ticket}, using original")
                finally:
                    # Clean up temp files
                    os.unlink(tmp_in_path)
                    if os.path.exists(tmp_out_path):
                        os.unlink(tmp_out_path)
            else:
                logger.warning(f"Failed to download file for processing: {response.status_code}")
        except Exception as e:
            logger.error(f"PDF processing error for ticket={data.ticket}: {e}")
    
    # ── Server-side page count via Google Drive export ───────────────────
    # Downloads the file, uploads to Drive temporarily, exports as PDF, counts pages
    office_extensions = {'.doc', '.docx', '.ppt', '.pptx',
                         '.odt', '.odp', '.txt', '.rtf'}
    server_page_count = None
    server_estimate_cents = None
    
    if file_extension in office_extensions and not metadata.get("pages_pre_counted"):
        from . import pricing, docx_pages
        import requests as req
        
        try:
            # Download file from Supabase
            logger.info(f"Downloading office doc for page counting: ticket={data.ticket}, ext={file_extension}")
            dl_response = req.get(storage_url, timeout=60)
            if dl_response.status_code == 200:
                # Count pages via Google Drive export
                server_page_count = docx_pages.count_pages_from_bytes(
                    dl_response.content, data.filename
                )
                
                if server_page_count and server_page_count > 0:
                    server_estimate_cents = pricing.calculate_price_cents(
                        page_count=server_page_count,
                        color_mode=metadata.get("color_mode", "bw"),
                        duplex=metadata.get("duplex", False),
                        paper_size=metadata.get("paper_size", "A4"),
                        binding=metadata.get("binding", "none"),
                        copies=metadata.get("copies", 1),
                    )
                    
                    db.update_job_estimate(data.ticket, server_estimate_cents, server_page_count)
                    
                    logger.info(
                        f"Server price recalc: ticket={data.ticket}, "
                        f"pages={server_page_count}, "
                        f"client={metadata.get('client_estimate_cents')}¢ → server={server_estimate_cents}¢"
                    )
            else:
                logger.warning(f"Failed to download file: {dl_response.status_code}")
        except Exception as e:
            logger.error(f"Page count error for ticket={data.ticket}: {e}")
    
    # Build response — prefer server values if available
    final_estimate = server_estimate_cents if server_estimate_cents is not None else metadata.get("client_estimate_cents")
    
    return {
        "status": "ok",
        "ticket": data.ticket,
        "message": f"File uploaded successfully. Your ticket is {data.ticket}",
        "estimate_cents": final_estimate,
        "server_page_count": server_page_count,
        "server_recalculated": server_estimate_cents is not None
    }


# ============== Google Drive Upload API ==============

# Semaphore to limit concurrent Drive uploads (prevents memory exhaustion on free tier)
import asyncio
_drive_upload_semaphore = asyncio.Semaphore(5)  # Max 5 concurrent Drive uploads

@app.post("/api/upload/drive")
@limiter.limit(f"{settings.rate_limit_per_ip_per_hour}/hour")
async def upload_to_drive(
    request: Request,
    file: UploadFile = File(...),
    ticket: str = Form(...),
    sender: str = Form(...),
    phone: Optional[str] = Form(None),
    passphrase: Optional[str] = Form(None),
    job_metadata: Optional[str] = Form(None),
    authorization: Optional[str] = Header(None)
):
    """
    Upload a file to the authenticated user's Google Drive.
    Memory-efficient: streams file to temp disk, then uploads to Drive from disk.
    Uses a semaphore to limit concurrent uploads (prevents OOM on free tier).
    """
    # Block uploads when shop is closed
    _check_shop_open()

    from . import google_drive
    import tempfile
    import shutil

    # Validate passphrase
    if not validate_passphrase(passphrase):
        raise HTTPException(status_code=403, detail="Invalid passphrase")
    
    # Get current user (required for Drive uploads)
    current_user = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        payload = auth.decode_access_token(token)
        if payload and payload.get("sub"):
            current_user = db.get_user_by_id(int(payload["sub"]))
    
    if not current_user:
        cookie_token = request.cookies.get("access_token")
        if cookie_token:
            payload = auth.decode_access_token(cookie_token)
            if payload and payload.get("sub"):
                current_user = db.get_user_by_id(int(payload["sub"]))
    
    if not current_user or not current_user.get("google_refresh_token"):
        raise HTTPException(status_code=401, detail="Google Drive access required. Please sign in with Google.")
    
    refresh_token = current_user["google_refresh_token"]
    
    # Validate file type
    if not file.filename or not is_allowed_file(file.filename):
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Allowed: PDF, Office docs, Images"
        )
    
    # Stream file to temp disk (memory-efficient — only 1MB in RAM at a time)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1]) as tmp:
            tmp_path = tmp.name
            file_size = 0
            chunk_size = 1024 * 1024  # 1MB chunks
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                file_size += len(chunk)
                # Check size limit during streaming (fail early)
                if file_size > settings.max_file_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large. Maximum size: {settings.max_file_mb}MB"
                    )
                tmp.write(chunk)
        
        if file_size == 0:
            raise HTTPException(status_code=400, detail="Empty file")
        
        # Acquire semaphore — limits concurrent Drive uploads to prevent OOM
        async with _drive_upload_semaphore:
            # Get or create the app folder in user's Drive
            folder_id = current_user.get("google_drive_folder_id")
            folder_id = google_drive.get_or_create_app_folder(refresh_token, folder_id)
            
            if not folder_id:
                raise HTTPException(status_code=500, detail="Failed to access Google Drive folder")
            
            # Cache folder ID for future uploads
            if folder_id != current_user.get("google_drive_folder_id"):
                db.update_user_drive_folder_id(current_user["id"], folder_id)
            
            # Upload file to Google Drive (streams from disk, not from memory)
            extension = get_file_extension(file.filename)
            drive_filename = f"{ticket}{extension}"
            content_type = file.content_type or "application/octet-stream"
            
            drive_file_id = google_drive.upload_file_from_path(
                refresh_token=refresh_token,
                file_path=tmp_path,
                filename=drive_filename,
                content_type=content_type,
                folder_id=folder_id
            )
            
            if not drive_file_id:
                raise HTTPException(status_code=500, detail="Failed to upload file to Google Drive")
        
        # Parse job metadata
        metadata = {}
        if job_metadata:
            try:
                metadata = json.loads(job_metadata)
            except json.JSONDecodeError:
                pass
        
        # Create database entry — filepath stores a placeholder, storage_type='drive'
        job = db.create_job(
            ticket=ticket,
            sender=sender,
            phone=phone,
            filename=file.filename,
            filepath=f"drive://{drive_file_id}",  # Placeholder URL for Drive files
            size_bytes=file_size,
            color_mode=metadata.get("color_mode", "bw"),
            duplex=metadata.get("duplex", False),
            paper_size=metadata.get("paper_size", "A4"),
            binding=metadata.get("binding", "none"),
            copies=metadata.get("copies", 1),
            selected_pages=json.dumps(metadata.get("page_list")) if metadata.get("page_list") else None,
            rotations=json.dumps(metadata.get("rotations")) if metadata.get("rotations") else None,
            client_estimate_cents=metadata.get("client_estimate_cents"),
            user_id=current_user["id"],
            customer_email=current_user.get("email"),
            status="draft"
        )
        
        # Update job with Drive storage info
        db.update_job_drive_info(ticket, "drive", drive_file_id)
        
        logger.info(f"File uploaded to Drive: ticket={ticket}, sender={sender}, size={file_size}, drive_id={drive_file_id}")
        
        # Process PDF if pages are selected (uses temp file on disk, not memory)
        page_list = metadata.get("page_list")
        rotations = metadata.get("rotations")
        
        if page_list and extension == ".pdf":
            from . import pdf_processor
            
            try:
                processed_filename = f"{ticket}_processed.pdf"
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_out:
                    tmp_out_path = tmp_out.name
                
                try:
                    rotation_dict = None
                    if rotations:
                        rotation_dict = {int(k): v for k, v in rotations.items()}
                    
                    if pdf_processor.extract_pages(tmp_path, tmp_out_path, page_list, rotation_dict):
                        # Upload processed file to Drive (streams from disk)
                        processed_drive_id = google_drive.upload_file_from_path(
                            refresh_token=refresh_token,
                            file_path=tmp_out_path,
                            filename=processed_filename,
                            content_type="application/pdf",
                            folder_id=folder_id
                        )
                        
                        if processed_drive_id:
                            # Delete original from Drive since we have processed version
                            google_drive.delete_file(refresh_token, drive_file_id)
                            db.update_job_drive_processed(ticket, processed_drive_id)
                            logger.info(f"PDF processed and uploaded to Drive: ticket={ticket}, pages={len(page_list)}")
                        else:
                            logger.warning(f"Failed to upload processed PDF to Drive for ticket={ticket}")
                    else:
                        logger.warning(f"PDF processing failed for ticket={ticket}, using original")
                finally:
                    if os.path.exists(tmp_out_path):
                        os.unlink(tmp_out_path)
            except Exception as e:
                logger.error(f"PDF processing error for ticket={ticket}: {e}")
        
        # ── Server-side page count via Google Drive export ──────────
        office_extensions = {'.doc', '.docx', '.ppt', '.pptx',
                             '.odt', '.odp', '.txt', '.rtf'}
        server_page_count = None
        server_estimate_cents = None
        
        if extension in office_extensions and drive_file_id and not metadata.get("pages_pre_counted"):
            from . import pricing, docx_pages
            
            try:
                # Get Drive service using the user's refresh token
                drive_service = google_drive._get_drive_service(refresh_token)
                
                if drive_service:
                    server_page_count = docx_pages.count_pages_from_drive(
                        drive_service, drive_file_id, file.filename
                    )
                    
                    if server_page_count and server_page_count > 0:
                        server_estimate_cents = pricing.calculate_price_cents(
                            page_count=server_page_count,
                            color_mode=metadata.get("color_mode", "bw"),
                            duplex=metadata.get("duplex", False),
                            paper_size=metadata.get("paper_size", "A4"),
                            binding=metadata.get("binding", "none"),
                            copies=metadata.get("copies", 1),
                        )
                        
                        db.update_job_estimate(ticket, server_estimate_cents, server_page_count)
                        
                        logger.info(
                            f"Server price recalc (Drive): ticket={ticket}, "
                            f"pages={server_page_count}, "
                            f"client={metadata.get('client_estimate_cents')}¢ → server={server_estimate_cents}¢"
                        )
            except Exception as e:
                logger.error(f"Page count error for ticket={ticket}: {e}")
        elif metadata.get("pages_pre_counted"):
            logger.info(f"Skipping page count for ticket={ticket} — already counted pre-cart")
        
        final_estimate = server_estimate_cents if server_estimate_cents is not None else metadata.get("client_estimate_cents")
        
        return {
            "status": "ok",
            "ticket": ticket,
            "message": f"File uploaded to Google Drive. Your ticket is {ticket}",
            "estimate_cents": final_estimate,
            "server_page_count": server_page_count,
            "server_recalculated": server_estimate_cents is not None,
            "storage_type": "drive"
        }
    
    finally:
        # Always clean up temp file
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


# ============== Cancel Draft Job ==============

@app.delete("/api/jobs/{ticket}/cancel-draft")
async def cancel_draft_job(
    ticket: str,
    current_user: dict = Depends(auth.get_current_user)
):
    """Cancel a draft job — deletes file from storage (Supabase or Drive) and removes DB record."""
    job = db.get_job_by_ticket(ticket)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Only allow cancelling draft jobs
    if job.get("status") != "draft":
        raise HTTPException(status_code=400, detail="Can only cancel draft jobs")
    
    # Verify ownership
    if job.get("user_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Delete file from storage
    filepath = job.get("filepath", "")
    storage_type = job.get("storage_type", "supabase")
    
    if storage_type == "drive" or filepath.startswith("drive://"):
        # Delete from Google Drive
        from . import google_drive
        drive_file_id = job.get("drive_file_id") or filepath.replace("drive://", "")
        refresh_token = current_user.get("google_refresh_token")
        if drive_file_id and refresh_token:
            try:
                google_drive.delete_file(refresh_token, drive_file_id)
                logger.info(f"Deleted Drive file for draft job {ticket}: {drive_file_id}")
            except Exception as e:
                logger.warning(f"Failed to delete Drive file for {ticket}: {e}")
        
        # Also delete processed version if exists
        processed_id = job.get("drive_processed_file_id")
        if processed_id and refresh_token:
            try:
                google_drive.delete_file(refresh_token, processed_id)
            except Exception:
                pass
    elif filepath:
        # Delete from Supabase storage
        storage_filename = filepath.split("/")[-1] if "/" in filepath else filepath
        try:
            storage.delete_file(storage_filename)
            logger.info(f"Deleted Supabase file for draft job {ticket}: {storage_filename}")
        except Exception as e:
            logger.warning(f"Failed to delete Supabase file for {ticket}: {e}")
    
    # Delete DB record
    db.delete_job(ticket)
    logger.info(f"Cancelled draft job: {ticket}")
    
    return {"status": "ok", "message": f"Draft job {ticket} cancelled"}


# ============== Upload API (Legacy/Fallback) ==============

@app.post("/api/upload")
@limiter.limit(f"{settings.rate_limit_per_ip_per_hour}/hour")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    sender: str = Form(...),
    phone: Optional[str] = Form(None),
    passphrase: Optional[str] = Form(None),
    job_metadata: Optional[str] = Form(None),
    authorization: Optional[str] = Header(None)
):
    """
    Upload a file for printing.
    
    Returns ticket number on success.
    Accepts optional job_metadata JSON with print options.
    If authenticated, associates the upload with the user account.
    """
    # Get current user if authenticated
    current_user = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        payload = auth.decode_access_token(token)
        if payload and payload.get("sub"):
            current_user = db.get_user_by_id(int(payload["sub"]))
    
    # Validate passphrase
    if not validate_passphrase(passphrase):
        raise HTTPException(status_code=403, detail="Invalid passphrase")
    
    # Check disk space
    if not check_disk_space():
        raise HTTPException(status_code=507, detail="Insufficient storage space")
    
    # Validate file type
    if not file.filename or not is_allowed_file(file.filename):
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Allowed: PDF, Office docs, Images"
        )
    
    # Parse job metadata
    metadata = {}
    if job_metadata:
        try:
            metadata = json.loads(job_metadata)
        except json.JSONDecodeError:
            logger.warning("Invalid job_metadata JSON, using defaults")
    
    # Generate ticket and prepare file path
    ticket = None
    if parent_ticket:
        # Suffix logic: Find next available suffix for this parent
        # e.g., if parent is '115', try '115-2', '115-3'...
        try:
            with db.get_connection() as conn:
                cursor = conn.cursor()
                # Find all tickets starting with parent_ticket + '-'
                cursor.execute(
                    "SELECT ticket FROM jobs WHERE ticket LIKE ? OR ticket = ?", 
                    (f"{parent_ticket}-%", parent_ticket)
                )
                existing_group = [row[0] for row in cursor.fetchall()]
                
                if not existing_group:
                    # Parent doesn't exist? Fallback to new ticket
                    ticket = db.generate_ticket()
                else:
                    # Find max suffix
                    max_suffix = 1
                    for t in existing_group:
                        if t == parent_ticket:
                            continue
                        parts = t.split('-')
                        if len(parts) > 1 and parts[-1].isdigit():
                            suffix = int(parts[-1])
                            if suffix > max_suffix:
                                max_suffix = suffix
                    
                    ticket = f"{parent_ticket}-{max_suffix + 1}"
        except Exception as e:
            logger.error(f"Error generating suffix ticket: {e}")
            ticket = db.generate_ticket()
    else:
        ticket = db.generate_ticket()
    
    extension = get_file_extension(file.filename)
    safe_filename = f"{ticket}{extension}"
    
    # Read file into memory and upload to Supabase Storage
    file_data = b""
    chunk_size = 8 * 1024 * 1024  # 8MB chunks
    
    try:
        # Read file content
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            file_data += chunk
            
            # Check size limit during upload
            if len(file_data) > settings.max_file_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Maximum size: {settings.max_file_mb}MB"
                )
        
        file_size = len(file_data)
        
        if file_size == 0:
            raise HTTPException(status_code=400, detail="Empty file")
        
        # Upload to Supabase Storage
        content_type = file.content_type or "application/octet-stream"
        storage_url = storage.upload_file(file_data, safe_filename, content_type)
        
        if not storage_url:
            raise HTTPException(status_code=500, detail="Failed to upload file to storage")
        
        # Create database entry with Supabase URL as filepath
        job = db.create_job(
            ticket=ticket,
            sender=sender,
            phone=phone,
            filename=file.filename,
            filepath=storage_url,  # Now stores Supabase URL
            size_bytes=file_size,
            color_mode=metadata.get("color_mode", "bw"),
            duplex=metadata.get("duplex", False),
            paper_size=metadata.get("paper_size", "A4"),
            binding=metadata.get("binding", "none"),
            copies=metadata.get("copies", 1),
            selected_pages=json.dumps(metadata.get("page_list")) if metadata.get("page_list") else None,
            rotations=json.dumps(metadata.get("rotations")) if metadata.get("rotations") else None,
            client_estimate_cents=metadata.get("client_estimate_cents"),
            user_id=current_user["id"] if current_user else None,
            customer_email=current_user["email"] if current_user else metadata.get("email")
        )
        
        # Process PDF if pages are selected (save processed file to Supabase too)
        page_list = metadata.get("page_list")
        rotations = metadata.get("rotations")
        if page_list and get_file_extension(file.filename) == ".pdf":
            from . import pdf_processor
            import tempfile
            
            # Write original to temp file for processing
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_in:
                tmp_in.write(file_data)
                tmp_in_path = tmp_in.name
            
            processed_filename = f"{ticket}_processed.pdf"
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_out:
                tmp_out_path = tmp_out.name
            
            try:
                rotation_dict = None
                if rotations:
                    rotation_dict = {int(k): v for k, v in rotations.items()}
                
                if pdf_processor.extract_pages(tmp_in_path, tmp_out_path, page_list, rotation_dict):
                    # Upload processed file to Supabase
                    with open(tmp_out_path, 'rb') as f:
                        processed_data = f.read()
                    
                    processed_url = storage.upload_file(processed_data, processed_filename, "application/pdf")
                    if processed_url:
                        # Delete the original file from Supabase since we have the processed version
                        from urllib.parse import urlparse
                        original_parsed = urlparse(storage_url)
                        original_filename = original_parsed.path.split("/")[-1]
                        if original_filename:
                            storage.delete_file(original_filename)
                            logger.info(f"Deleted original file after processing: {original_filename}")
                        db.update_job_processing_status(ticket, "processed", processed_url)
                        logger.info(f"PDF processed and uploaded: ticket={ticket}, pages={len(page_list)}")
                    else:
                        logger.warning(f"Failed to upload processed PDF for ticket={ticket}")
                else:
                    logger.warning(f"PDF processing failed for ticket={ticket}, using original")
            finally:
                # Clean up temp files
                os.unlink(tmp_in_path)
                if os.path.exists(tmp_out_path):
                    os.unlink(tmp_out_path)
        
        logger.info(f"File uploaded to Supabase: ticket={ticket}, sender={sender}, size={file_size}")
        
        return {
            "status": "ok",
            "ticket": ticket,
            "message": f"File uploaded successfully. Your ticket is {ticket}",
            "estimate_cents": metadata.get("client_estimate_cents")
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Upload failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Upload failed. Please try again.")


# ============== Jobs API ==============

@app.get("/api/jobs")
async def list_jobs(status: Optional[str] = None, username: str = Depends(verify_staff_credentials)):
    """Get all jobs, optionally filtered by status."""
    jobs = db.get_all_jobs(status=status)
    return {"jobs": jobs, "count": len(jobs)}


@app.get("/api/jobs/{ticket}")
async def get_job(ticket: str, username: str = Depends(verify_staff_credentials)):
    """Get a single job by ticket number."""
    job = db.get_job_by_ticket(ticket)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/download/{ticket}")
async def download_file(ticket: str, username: str = Depends(verify_staff_credentials)):
    """Download the file for a job. Handles both Supabase and Google Drive storage."""
    job = db.get_job_by_ticket(ticket)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Determine download filename
    if job.get("processed_filepath") or job.get("drive_processed_file_id"):
        base_name = Path(job["filename"]).stem
        download_filename = f"{base_name}_selected.pdf"
    else:
        download_filename = job["filename"]
    
    # Update status to downloaded if pending or paid
    if job["status"] in ("pending", "paid"):
        db.update_job_status(ticket, "downloaded")
    
    storage_type = job.get("storage_type", "supabase")
    
    # ===== Google Drive download =====
    if storage_type == "drive":
        from . import google_drive
        from fastapi.responses import StreamingResponse
        
        # Get the file owner's refresh token
        if not job.get("user_id"):
            raise HTTPException(status_code=500, detail="Drive file has no associated user")
        
        owner = db.get_user_by_id(job["user_id"])
        if not owner or not owner.get("google_refresh_token"):
            raise HTTPException(status_code=500, detail="Cannot access file — Google Drive credentials unavailable")
        
        refresh_token = owner["google_refresh_token"]
        
        # Use processed file if available
        file_id = job.get("drive_processed_file_id") or job.get("drive_file_id")
        if not file_id:
            raise HTTPException(status_code=404, detail="Drive file ID not found")
        
        # Chunked streaming — streams from Drive API without loading entire file into RAM
        # Combined with token cache (~300ms saved on repeat downloads)
        ext = Path(download_filename).suffix.lower()
        content_types = {
            ".pdf": "application/pdf",
            ".doc": "application/msword",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png",
        }
        content_type = content_types.get(ext, "application/octet-stream")
        
        stream = google_drive.stream_file(refresh_token, file_id)
        if stream:
            logger.info(f"Streaming Drive file in chunks: ticket={ticket}, file_id={file_id}")
            return StreamingResponse(
                stream,
                media_type=content_type,
                headers={
                    "Content-Disposition": f'attachment; filename="{download_filename}"',
                }
            )
        
        raise HTTPException(status_code=500, detail="Failed to download file from Google Drive")
    
    # ===== Supabase / HTTP download =====
    if job.get("processed_filepath"):
        file_url = job["processed_filepath"]
    else:
        file_url = job["filepath"]
    
    logger.info(f"File download: ticket={ticket}, storage={storage_type}, processed={job.get('processed_filepath') is not None}")
    
    # If it's a Supabase URL, redirect with download parameter
    if file_url.startswith("http"):
        from fastapi.responses import RedirectResponse
        from urllib.parse import urlencode, urlparse, urlunparse, parse_qs
        
        # Add download query parameter to force download instead of preview
        parsed = urlparse(file_url)
        query_params = parse_qs(parsed.query)
        query_params['download'] = [download_filename]
        new_query = urlencode(query_params, doseq=True)
        download_url = urlunparse(parsed._replace(query=new_query))
        
        return RedirectResponse(url=download_url)
    
    # Fallback for legacy local files (shouldn't happen with Supabase-only)
    filepath = Path(file_url)
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    return FileResponse(
        path=filepath,
        filename=download_filename,
        media_type="application/octet-stream"
    )


@app.post("/api/jobs/{ticket}/archive")
async def archive_job(ticket: str, username: str = Depends(verify_staff_credentials)):
    """Archive a job (files stay in Supabase Storage)."""
    job = db.get_job_by_ticket(ticket)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job["status"] == "archived":
        return {"status": "ok", "message": "Job already archived"}
    
    if job["status"] == "cancelled":
        raise HTTPException(status_code=400, detail="Cannot archive cancelled job")
    
    # No file move needed - files stay in Supabase Storage
    db.update_job_status(ticket, "archived")
    logger.info(f"Job archived: ticket={ticket}")
    
    return {"status": "ok", "message": f"Job {ticket} archived"}


@app.post("/api/jobs/{ticket}/cancel")
async def cancel_job(ticket: str, username: str = Depends(verify_staff_credentials)):
    """Cancel a job and delete the file from Supabase."""
    job = db.get_job_by_ticket(ticket)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job["status"] == "cancelled":
        return {"status": "ok", "message": "Job already cancelled"}
    
    if job["status"] == "archived":
        raise HTTPException(status_code=400, detail="Cannot cancel archived job")
    
    # Delete file from Supabase Storage
    file_url = job["filepath"]
    if file_url.startswith("http"):
        # Extract filename from URL
        filename = file_url.split("/")[-1]
        storage.delete_file(filename)
    
    db.update_job_status(ticket, "cancelled")
    logger.info(f"Job cancelled: ticket={ticket}")
    
    return {"status": "ok", "message": f"Job {ticket} cancelled"}


@app.delete("/api/jobs/{ticket}")
async def delete_job(ticket: str, username: str = Depends(verify_staff_credentials)):
    """Permanently delete a job and its file from Supabase."""
    job = db.get_job_by_ticket(ticket)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Delete file from Supabase Storage
    file_url = job["filepath"]
    if file_url.startswith("http"):
        filename = file_url.split("/")[-1]
        storage.delete_file(filename)
    
    # Also delete processed file if exists
    processed_url = job.get("processed_filepath")
    if processed_url and processed_url.startswith("http"):
        processed_filename = processed_url.split("/")[-1]
        storage.delete_file(processed_filename)
    
    db.delete_job(ticket)
    logger.info(f"Job deleted: ticket={ticket}")
    
    return {"status": "ok", "message": f"Job {ticket} deleted"}


# ============== Upload Cleanup (beforeunload beacon) ==============

@app.post("/api/upload/cleanup")
async def cleanup_unpaid_uploads(request: Request):
    """
    Called via navigator.sendBeacon when user navigates away or closes page with unpaid cart items.
    Cancels draft/pending jobs and deletes their files from storage (Supabase or Google Drive).
    """
    try:
        body = await request.body()
        data = json.loads(body)
        tickets = data.get("tickets", [])
        token = data.get("token", "")
        
        if not tickets or not isinstance(tickets, list):
            return {"status": "ok", "cleaned": 0}
        
        # Get current user from token (needed for Drive file deletion)
        current_user = None
        if token:
            payload = auth.decode_access_token(token)
            if payload and payload.get("sub"):
                current_user = db.get_user_by_id(int(payload["sub"]))
        
        cleaned = 0
        for ticket in tickets[:20]:
            try:
                job = db.get_job_by_ticket(ticket)
                if not job:
                    continue
                    
                # Only clean up unpaid jobs
                if job["status"] not in ("draft", "pending", "pending_payment"):
                    continue
                
                if job.get("paid_at"):
                    continue
                
                # Delete file from storage (Drive or Supabase)
                filepath = job.get("filepath", "")
                storage_type = job.get("storage_type", "supabase")
                
                if storage_type == "drive" or (filepath and filepath.startswith("drive://")):
                    # Delete from Google Drive
                    if current_user:
                        try:
                            from . import google_drive
                            drive_file_id = job.get("drive_file_id") or filepath.replace("drive://", "")
                            refresh_token = current_user.get("google_refresh_token")
                            if drive_file_id and refresh_token:
                                google_drive.delete_file(refresh_token, drive_file_id)
                                logger.info(f"Cleanup: deleted Drive file for {ticket}: {drive_file_id}")
                            
                            # Also delete processed version
                            processed_id = job.get("drive_processed_file_id")
                            if processed_id and refresh_token:
                                try:
                                    google_drive.delete_file(refresh_token, processed_id)
                                except Exception:
                                    pass
                        except Exception as e:
                            logger.warning(f"Cleanup: failed to delete Drive file for {ticket}: {e}")
                elif filepath:
                    # Delete from Supabase storage
                    storage_filename = filepath.split("/")[-1] if "/" in filepath else filepath
                    try:
                        storage.delete_file(storage_filename)
                    except Exception as e:
                        logger.warning(f"Cleanup: failed to delete Supabase file for {ticket}: {e}")
                
                # Delete the DB record (same as cancel_draft_job)
                db.delete_job(ticket)
                cleaned += 1
                logger.info(f"Cleanup: cancelled unpaid job {ticket}")
                
            except Exception as e:
                logger.warning(f"Cleanup: error processing ticket {ticket}: {e}")
                continue
        
        return {"status": "ok", "cleaned": cleaned}
        
    except Exception as e:
        logger.warning(f"Cleanup endpoint error: {e}")
        return {"status": "ok", "cleaned": 0}


# ============== Stats API ==============

@app.get("/api/stats")
async def get_stats(username: str = Depends(verify_staff_credentials)):
    """Get system statistics."""
    jobs = db.get_all_jobs()
    pending = sum(1 for j in jobs if j["status"] in ("pending", "paid"))
    downloaded = sum(1 for j in jobs if j["status"] == "downloaded")
    archived = sum(1 for j in jobs if j["status"] == "archived")
    cancelled = sum(1 for j in jobs if j["status"] == "cancelled")
    paid = sum(1 for j in jobs if j["status"] == "paid")
    printed = sum(1 for j in jobs if j["status"] == "printed")
    delivered = sum(1 for j in jobs if j["status"] == "delivered")
    
    # Count today's jobs and revenue (IST)
    # Only count PAID jobs for revenue (not drafts, cancelled, etc.)
    paid_statuses = ('paid', 'downloaded', 'printed', 'delivered')
    today_ist = db.get_ist_now().date()
    today_jobs = 0
    today_amount_cents = 0
    for j in jobs:
        try:
            created = j.get("created_at", "")
            if created:
                from datetime import datetime as _dt
                if isinstance(created, str):
                    job_date = _dt.fromisoformat(created.replace("Z", "+00:00")).date()
                else:
                    job_date = created.date() if hasattr(created, 'date') else None
                if job_date == today_ist:
                    today_jobs += 1
                    # Only count revenue from paid jobs; prefer server estimate over client estimate
                    if j.get("status") in paid_statuses:
                        today_amount_cents += j.get("client_estimate_cents") or j.get("estimate_cents") or 0
        except Exception:
            pass
    
    free_space = get_disk_free_space()
    
    return {
        "total_jobs": len(jobs),
        "today_jobs": today_jobs,
        "today_amount": today_amount_cents,
        "pending": pending,
        "downloaded": downloaded,
        "archived": archived,
        "cancelled": cancelled,
        "paid": paid,
        "printed": printed,
        "delivered": delivered,
        "disk_free_mb": free_space // (1024 * 1024),
        "disk_low": not check_disk_space()
    }


# ============== Payment API ==============

@app.post("/api/jobs/create")
@limiter.limit(f"{settings.rate_limit_per_ip_per_hour}/hour")
async def create_job_with_payment(
    request: Request,
    file: UploadFile = File(...),
    sender: str = Form(...),
    phone: Optional[str] = Form(None),
    email: Optional[str] = Form(None),
    passphrase: Optional[str] = Form(None),
    job_metadata: Optional[str] = Form(None),
    skip_payment: bool = Form(False),
    parent_ticket: Optional[str] = Form(None),
    authorization: Optional[str] = Header(None)
):
    """
    Create a job and Razorpay order for payment.
    
    Returns job_id, ticket, token, razorpay_order_id for checkout.
    REQUIRES authentication - user must be logged in to upload.
    """
    # Block uploads when shop is closed
    _check_shop_open()
    
    from . import razorpay as rp
    
    # REQUIRE authentication for uploads
    current_user = None
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    
    # Use standard auth logic with cookie fallback
    current_user = await auth.get_current_user_optional(request, token)
    
    if not current_user:
        raise HTTPException(
            status_code=401,
            detail="Login required to upload files. Please sign in first."
        )
    
    # Validate passphrase
    if not validate_passphrase(passphrase):
        raise HTTPException(status_code=403, detail="Invalid passphrase")
    
    # Check disk space
    if not check_disk_space():
        raise HTTPException(status_code=507, detail="Insufficient storage space")
    
    # Validate file type
    if not file.filename or not is_allowed_file(file.filename):
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Allowed: PDF, Office docs, Images"
        )
    
    # Parse job metadata
    metadata = {}
    if job_metadata:
        try:
            metadata = json.loads(job_metadata)
        except json.JSONDecodeError:
            logger.warning("Invalid job_metadata JSON, using defaults")
    
    # Get estimate from metadata (required for payment)
    estimate_cents = metadata.get("client_estimate_cents", 0)
    if estimate_cents <= 0:
        raise HTTPException(status_code=400, detail="Invalid estimate. Price must be positive.")
    
    # Generate ticket and prepare file path
    ticket = None
    if parent_ticket:
        # Suffix logic: Find next available suffix for this parent
        # e.g., if parent is '115', try '115-2', '115-3'...
        try:
            with db.get_connection() as conn:
                cursor = conn.cursor()
                # Find all tickets starting with parent_ticket + '-'
                cursor.execute(
                    "SELECT ticket FROM jobs WHERE ticket LIKE ? OR ticket = ?", 
                    (f"{parent_ticket}-%", parent_ticket)
                )
                existing_group = [row[0] for row in cursor.fetchall()]
                
                if not existing_group:
                    # Parent doesn't exist? Fallback to new ticket
                    ticket = db.generate_ticket()
                else:
                    # Find max suffix
                    max_suffix = 1
                    for t in existing_group:
                        if t == parent_ticket:
                            continue
                        parts = t.split('-')
                        if len(parts) > 1 and parts[-1].isdigit():
                            suffix = int(parts[-1])
                            if suffix > max_suffix:
                                max_suffix = suffix
                    
                    ticket = f"{parent_ticket}-{max_suffix + 1}"
        except Exception as e:
            logger.error(f"Error generating suffix ticket: {e}")
            ticket = db.generate_ticket()
    else:
        ticket = db.generate_ticket()

    extension = get_file_extension(file.filename)
    safe_filename = f"{ticket}{extension}"
    filepath = settings.storage_dir / safe_filename
    
    # Stream file to disk
    file_size = 0
    chunk_size = 8 * 1024 * 1024
    
    try:
        with open(filepath, "wb") as f:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                file_size += len(chunk)
                
                if file_size > settings.max_file_bytes:
                    f.close()
                    filepath.unlink()
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large. Maximum size: {settings.max_file_mb}MB"
                    )
                
                f.write(chunk)
        
        if file_size == 0:
            filepath.unlink()
            raise HTTPException(status_code=400, detail="Empty file")
        
        # Create database entry
        job = db.create_job(
            ticket=ticket,
            sender=sender,
            phone=phone,
            filename=file.filename,
            filepath=str(filepath),
            size_bytes=file_size,
            color_mode=metadata.get("color_mode", "bw"),
            duplex=metadata.get("duplex", False),
            paper_size=metadata.get("paper_size", "A4"),
            binding=metadata.get("binding", "none"),
            copies=metadata.get("copies", 1),
            selected_pages=json.dumps(metadata.get("page_list")) if metadata.get("page_list") else None,
            rotations=json.dumps(metadata.get("rotations")) if metadata.get("rotations") else None,
            client_estimate_cents=estimate_cents,
            user_id=current_user["id"] if current_user else None,
            customer_email=current_user["email"] if current_user else email,
            status="draft" if skip_payment else "pending"
        )
        
        # Process PDF if pages are selected
        page_list = metadata.get("page_list")
        rotations = metadata.get("rotations")
        if page_list and get_file_extension(file.filename) == ".pdf":
            from . import pdf_processor
            processed_filename = f"{ticket}_processed.pdf"
            processed_path = settings.storage_dir / processed_filename
            
            rotation_dict = None
            if rotations:
                rotation_dict = {int(k): v for k, v in rotations.items()}
            
            if pdf_processor.extract_pages(str(filepath), str(processed_path), page_list, rotation_dict):
                db.update_job_processing_status(ticket, "processed", str(processed_path))
                logger.info(f"PDF processed: ticket={ticket}, pages={len(page_list)}")
        
        # Generate token for payment flow
        token = db.generate_token()
        
        # Create payment order
        
        # Skip payment creation if requested (for cart flow)
        if skip_payment:
            return {
                "status": "pending_payment",
                "ticket": ticket,
                "token": token,
                "amount_cents": estimate_cents,
                "currency": "INR",
                "message": "Job created. Add to cart."
            }

        if rp.is_configured():
            internal_order_id = db.generate_order_id()
            order = rp.create_order(
                order_id=internal_order_id,
                amount_cents=estimate_cents,
                currency="INR",
                notes={"order_id": internal_order_id, "ticket": ticket, "sender": sender}
            )
            
            if not order:
                raise HTTPException(status_code=500, detail="Failed to create payment order")
            
            razorpay_order_id = order.get("id")
            
            # Update job with order info
            db.update_job_with_order(ticket, internal_order_id, estimate_cents, token)
            
            # Create payment audit record
            client_ip = request.client.host if request.client else None
            db.create_payment_record(
                job["id"], internal_order_id, estimate_cents,
                payment_gateway='razorpay',
                customer_email=current_user["email"] if current_user else email,
                customer_phone=phone,
                ip_address=client_ip
            )
            
            # Update email if provided
            if email:
                with db.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("UPDATE jobs SET customer_email = %s WHERE ticket = %s", (email, ticket))
            
            logger.info(f"Job created with payment: ticket={ticket}, order_id={internal_order_id}, rp_order={razorpay_order_id}")
            
            return {
                "status": "ok",
                "ticket": ticket,
                "token": token,
                "order_id": internal_order_id,
                "razorpay_order_id": razorpay_order_id,
                "razorpay_key_id": settings.razorpay_key_id,
                "amount_cents": estimate_cents,
                "currency": "INR"
            }
        else:
            # Razorpay not configured - fallback to legacy flow
            logger.warning("Razorpay not configured, using legacy flow")
            
            # Send mock notifications for testing
            from . import notifications, sms
            
            logger.info(f"Preparing mock notifications. Email: '{email}', Phone: '{phone}'")
            
            # Send confirmation email if email is set
            if email:
                with db.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("UPDATE jobs SET customer_email = %s WHERE ticket = %s", (email, ticket))
                
                notifications.send_payment_confirmation(
                    email=email,
                    token=db.get_display_ticket(ticket),
                    estimate_cents=estimate_cents,
                    customer_name=sender
                )
            
            # Send SMS if phone is set
            if phone:
                sms.send_ticket_notification(
                    phone=phone,
                    ticket=token
                )
            
            return {
                "status": "ok",
                "ticket": ticket,
                "token": token,
                "message": "Payment gateway not configured. Job created (Mock Payment).",
                "estimate_cents": estimate_cents
            }
    
    except HTTPException:
        raise
    except sqlite3.OperationalError as e:
        # Database lock contention - provide helpful message
        if filepath.exists():
            filepath.unlink()
        error_msg = str(e).lower()
        if "locked" in error_msg or "busy" in error_msg:
            logger.warning(f"Job creation failed due to database contention: {e}")
            raise HTTPException(
                status_code=503,
                detail="Server is busy processing other uploads. Please try again in a few seconds."
            )
        logger.error(f"Database error during job creation: {e}")
        raise HTTPException(status_code=500, detail="Job creation failed due to database error")
    except Exception as e:
        if filepath.exists():
            filepath.unlink()
        logger.error(f"Job creation failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Job creation failed. Please try again.")



class BulkOrderRequest(BaseModel):
    tickets: List[str]
    sender: str
    email: Optional[str] = None
    phone: Optional[str] = None


@app.post("/api/payment/create-bulk-order")
async def create_bulk_order(
    request: BulkOrderRequest,
    authorization: Optional[str] = Header(None)
):
    """
    Create a single Razorpay order for multiple jobs (cart checkout).
    Supports both authenticated and guest users.
    """
    # Block payments when shop is closed
    _check_shop_open()
    
    from . import razorpay as rp
    import uuid

    # Get current user if authenticated (optional for guests)
    current_user = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        payload = auth.decode_access_token(token)
        if payload and payload.get("sub"):
            current_user = db.get_user_by_id(int(payload["sub"]))

    # Fetch jobs first
    jobs = db.get_jobs_by_tickets(request.tickets)
    if len(jobs) != len(request.tickets):
        raise HTTPException(status_code=404, detail="Some jobs not found. Your cart may contain stale items — please clear your cart and re-upload.")

    # Verify ownership (only for authenticated users)
    if current_user:
        for job in jobs:
            if job["user_id"] and job["user_id"] != current_user["id"]:
                raise HTTPException(status_code=403, detail=f"Not authorized for ticket {job['ticket']}")

    # Validate jobs are in valid state for payment
    for job in jobs:
        if job["status"] not in ("draft", "created"):
            raise HTTPException(
                status_code=400,
                detail=f"Job {job['ticket']} is not eligible for payment (status: {job['status']}). Please clear your cart and re-upload."
            )
        if not job.get("filepath"):
            raise HTTPException(
                status_code=400,
                detail=f"Job {job['ticket']} has no uploaded file. It may have expired. Please clear your cart and re-upload."
            )

    # Calculate total
    total_cents = sum(job["client_estimate_cents"] for job in jobs if job["client_estimate_cents"])
    if total_cents == 0:
         raise HTTPException(status_code=400, detail="Total amount is 0")
    
    job_ids = [job["id"] for job in jobs]

    # Handle Mock Mode (Dev/No-Config)
    if not rp.is_configured():
        mock_order_id = db.generate_order_id()
        
        # Create payment record
        db.create_bulk_payment_record(job_ids, mock_order_id, total_cents)
        
        # Mark as PAID immediately for mock mode
        with db.get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now().isoformat()
            placeholders = ','.join(['%s'] * len(job_ids))
            
            # Update Jobs: set paid_at, order_id, sync sender/contact details
            # AND activate validation status to 'pending' (from draft)
            cursor.execute(
                f"""UPDATE jobs 
                   SET paid_at = %s, order_id = %s, sender = %s, customer_email = %s, phone = %s, status = 'pending' 
                   WHERE id IN ({placeholders})""",
                (now, mock_order_id, request.sender, request.email, request.phone, *job_ids)
            )
            # Update Payment: set status captured
            cursor.execute(
                "UPDATE payments SET status = 'captured', verified_at = %s WHERE order_id = %s",
                (now, mock_order_id)
            )
            
        logger.info(f"Mock bulk payment completed: {mock_order_id} for {request.tickets}")

        return {
            "status": "ok",
            "message": "Payment successful (Mock Mode)",
            "order_id": mock_order_id,
            "amount_cents": total_cents,
            "currency": "INR"
        }

    # Real Razorpay Order
    internal_order_id = db.generate_order_id()
    
    # Get customer details
    customer_email = request.email or ""
    customer_phone = request.phone or ""
    if current_user:
        customer_email = customer_email or current_user.get("email", "")
    
    order = rp.create_order(
        order_id=internal_order_id,
        amount_cents=total_cents,
        currency="INR",
        notes={
            "order_id": internal_order_id,
            "tickets": ",".join(request.tickets),
            "sender": request.sender
        }
    )
    
    if not order:
        raise HTTPException(status_code=500, detail="Failed to create Razorpay order")
    
    razorpay_order_id = order.get("id")
    
    # Create bulk payment record and link to jobs
    client_ip = None
    db.create_bulk_payment_record(
        job_ids, internal_order_id, total_cents,
        payment_gateway='razorpay',
        customer_email=customer_email,
        customer_phone=customer_phone,
        ip_address=client_ip
    )
    
    # Sync sender/contact details AND link the order_id to ALL jobs
    # This is critical — verify_payment looks up jobs by order_id
    # Note: do NOT overwrite estimate_cents — each job keeps its own per-file cost (client_estimate_cents)
    # Generate a unique token for EACH job (token column has a UNIQUE constraint)
    with db.get_connection() as conn:
        cursor = conn.cursor()
        for jid in job_ids:
            job_token = db.generate_token()
            cursor.execute(
                """UPDATE jobs 
                   SET sender = %s, customer_email = %s, phone = %s, 
                       order_id = %s, token = %s, status = 'pending_payment' 
                   WHERE id = %s""",
                (request.sender, request.email, request.phone, internal_order_id, job_token, jid)
            )

    logger.info(f"Bulk order created: order_id={internal_order_id}, rp_order={razorpay_order_id}, tickets={request.tickets}")
    
    return {
        "status": "ok",
        "order_id": internal_order_id,
        "razorpay_order_id": razorpay_order_id,
        "razorpay_key_id": settings.razorpay_key_id,
        "amount_cents": total_cents,
        "currency": "INR"
    }


@app.post("/api/payment/revert-to-draft")
async def revert_payment_to_draft(
    request: Request,
    authorization: Optional[str] = Header(None)
):
    """
    Revert job statuses from 'pending_payment' back to 'draft'.
    Called when user cancels or leaves the payment gateway without paying.
    This ensures abandoned payment attempts don't appear in user history.
    Requires authentication to prevent abuse.
    
    IMPORTANT: order_id and token are preserved so that if a payment completes
    after the modal is dismissed (e.g., UPI payment completes in background),
    the webhook can still find and update the job.
    """
    # Authenticate: require valid JWT or cookie
    current_user = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        payload = auth.decode_access_token(token)
        if payload and payload.get("sub"):
            current_user = db.get_user_by_id(int(payload["sub"]))
    if not current_user:
        # Also check cookie-based auth
        cookie_token = request.cookies.get("access_token")
        if cookie_token:
            payload = auth.decode_access_token(cookie_token)
            if payload and payload.get("sub"):
                current_user = db.get_user_by_id(int(payload["sub"]))
    
    # Allow guest mode (no auth) but log it for monitoring
    # Guest users still need to know exact ticket IDs to revert
    
    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    
    tickets = data.get("tickets", [])
    if not tickets or not isinstance(tickets, list):
        raise HTTPException(status_code=400, detail="Missing or invalid tickets list")
    
    reverted = 0
    with db.get_connection() as conn:
        cursor = conn.cursor()
        for ticket in tickets:
            # Only revert jobs that belong to this user (if authenticated)
            if current_user:
                cursor.execute(
                    "UPDATE jobs SET status = 'draft' "
                    "WHERE ticket = %s AND status = 'pending_payment' AND user_id = %s",
                    (ticket, current_user["id"])
                )
            else:
                # Guest mode: revert only guest jobs (user_id IS NULL)
                cursor.execute(
                    "UPDATE jobs SET status = 'draft' "
                    "WHERE ticket = %s AND status = 'pending_payment' AND user_id IS NULL",
                    (ticket,)
                )
            reverted += cursor.rowcount
    
    user_label = current_user["email"] if current_user else "guest"
    logger.info(f"Reverted {reverted} jobs from pending_payment to draft: tickets={tickets} by={user_label}")
    return {"status": "ok", "reverted": reverted}


@app.post("/api/pay/verify")
async def verify_payment_endpoint(request: Request):
    """
    Verify payment signature from client-side Razorpay checkout.
    
    Expects JSON body with: order_id, razorpay_order_id, razorpay_payment_id, razorpay_signature
    Razorpay uses HMAC-SHA256 signature verification.
    """
    from . import razorpay as rp
    from . import notifications
    
    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    
    order_id = data.get("order_id")  # Internal order ID
    razorpay_order_id = data.get("razorpay_order_id")
    razorpay_payment_id = data.get("razorpay_payment_id")
    razorpay_signature = data.get("razorpay_signature")
    
    if not order_id or not razorpay_payment_id or not razorpay_signature:
        raise HTTPException(status_code=400, detail="Missing payment verification parameters")
    
    # Verify payment signature
    is_valid = rp.verify_payment_signature(
        razorpay_order_id=razorpay_order_id,
        razorpay_payment_id=razorpay_payment_id,
        razorpay_signature=razorpay_signature
    )
    
    if not is_valid:
        raise HTTPException(status_code=400, detail="Payment signature verification failed")
    
    # Server-to-server verification: fetch payment details from Razorpay API
    # to confirm the payment is actually captured (defense-in-depth)
    payment_method = None
    payment_details = rp.get_payment_details(razorpay_payment_id)
    if payment_details:
        payment_method = payment_details.get("method")  # upi, card, netbanking, etc.
        payment_status = payment_details.get("status", "")
        # Only accept captured or authorized payments
        if payment_status not in ("captured", "authorized"):
            logger.warning(f"Payment status is '{payment_status}' (not captured/authorized) for payment {razorpay_payment_id}")
            raise HTTPException(
                status_code=400,
                detail=f"Payment is not confirmed (status: {payment_status}). Please wait and try again."
            )
    else:
        # If we can't reach Razorpay API, still proceed if signature verified
        # (HMAC verification already passed, so payment is authentic)
        logger.warning(f"Could not fetch payment details from Razorpay for {razorpay_payment_id}, proceeding with signature-only verification")
    
    # Update job status to paid
    job = db.update_payment_info(order_id, razorpay_payment_id, razorpay_signature)
    
    if not job:
        # Webhook may have already marked this job as paid (race condition).
        # Check if job exists and is already paid — if so, treat as success.
        with db.get_connection() as conn:
            cursor = conn.cursor(cursor_factory=db.psycopg2.extras.RealDictCursor)
            cursor.execute("SELECT * FROM jobs WHERE order_id = %s AND status = 'paid' LIMIT 1", (order_id,))
            row = cursor.fetchone()
            if row:
                job = dict(row)
                logger.info(f"Payment verify: job already paid by webhook for order={order_id} — skipping notification (webhook sent it)")
                # Webhook already sent email/SMS, just return success
                db.update_payment_verified(order_id, razorpay_payment_id, payment_method=payment_method)
                return {"status": "ok", "job_id": job["id"], "token": job["token"]}
            else:
                raise HTTPException(status_code=404, detail="Job not found for this order")
    
    # Update payment audit record
    db.update_payment_verified(order_id, razorpay_payment_id, payment_method=payment_method)
    
    # Send confirmation email if email is set
    if job.get("customer_email"):
        # Calculate TOTAL order amount by summing all jobs with the same order_id
        # (for bulk orders, each job has its own client_estimate_cents — we need the sum)
        total_amount = 0
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COALESCE(SUM(client_estimate_cents), 0) FROM jobs WHERE order_id = %s",
                (order_id,)
            )
            total_amount = cursor.fetchone()[0]
        
        # Fallback to single job amount if query returns 0
        if total_amount == 0:
            total_amount = job.get("estimate_cents") or job.get("client_estimate_cents") or 0
        
        # Use the 3-digit display ticket for the email (not the 6-char internal token)
        email_token = db.get_display_ticket(job.get("ticket", ""))
        notifications.send_payment_confirmation(
            email=job["customer_email"],
            token=email_token,
            estimate_cents=total_amount,
            customer_name=job.get("sender"),
            order_id=order_id,
            payment_id=razorpay_payment_id,
            payment_method=payment_method
        )
        
    # Send SMS if phone is set
    if job.get("phone"):
        from . import sms
        sms.send_ticket_notification(
            phone=job["phone"],
            ticket=job["token"]
        )
    
    logger.info(f"Payment verified: ticket={job['ticket']}, token={job['token']}, rp_payment={razorpay_payment_id}")
    
    return {"status": "ok", "job_id": job["id"], "token": job["token"]}


@app.post("/api/payment/webhook")
async def razorpay_webhook(request: Request):
    """
    Handle Razorpay webhook events.
    
    Verifies webhook signature and processes payment events idempotently.
    Razorpay sends X-Razorpay-Signature header for verification.
    """
    from . import razorpay as rp
    
    # Get raw body for signature verification
    body = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")
    
    # Verify webhook signature
    if not rp.verify_webhook_signature(body, signature):
        logger.warning("Invalid Razorpay webhook signature")
        raise HTTPException(status_code=400, detail="Invalid signature")
    
    try:
        payload = json.loads(body)
    except:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    event_type = payload.get("event", "")
    event_payload = payload.get("payload", {})
    payment_entity = event_payload.get("payment", {}).get("entity", {})
    
    event_id = payload.get("id") or f"{event_type}_{datetime.now().isoformat()}"
    
    # Check if already processed
    if db.check_webhook_processed(event_id):
        logger.info(f"Webhook already processed: {event_id}")
        return {"status": "ok", "message": "Already processed"}
    
    # Log the event
    db.log_webhook_event(event_id, event_type, json.dumps(payload))
    
    # Process payment.captured event (successful payment)
    if event_type == "payment.captured":
        razorpay_order_id = payment_entity.get("order_id", "")
        razorpay_payment_id = payment_entity.get("id", "")
        notes = payment_entity.get("notes", {})
        
        if razorpay_payment_id:
            # Strategy 1: Look up internal order_id from Razorpay notes
            internal_order_id = notes.get("order_id", "")
            
            # Strategy 2: Fallback — use the receipt field (set to internal_order_id at creation)
            if not internal_order_id:
                order_entity = event_payload.get("order", {}).get("entity", {})
                receipt = order_entity.get("receipt", "") or notes.get("receipt", "")
                if receipt:
                    internal_order_id = db.find_order_by_receipt(receipt)
                    if internal_order_id:
                        logger.info(f"Webhook: Found order via receipt fallback: {internal_order_id}")
            
            # Strategy 3: Fallback — fetch the order from Razorpay API to get the receipt
            # (receipt is always set to our internal_order_id during order creation)
            if not internal_order_id and razorpay_order_id:
                rp_order = rp.get_order_details(razorpay_order_id)
                if rp_order and rp_order.get("receipt"):
                    internal_order_id = rp_order["receipt"]
                    logger.info(f"Webhook: Found order via Razorpay API receipt fallback: {internal_order_id}")
            
            if internal_order_id:
                job = db.update_payment_info(internal_order_id, razorpay_payment_id)
                if job:
                    logger.info(f"Webhook: Payment captured for ticket={job['ticket']}, order={internal_order_id}")
                    
                    # Update payment audit record
                    payment_method = payment_entity.get("method")  # upi, card, netbanking
                    db.update_payment_verified(internal_order_id, razorpay_payment_id, payment_method=payment_method)
                    
                    # Send confirmation email (webhook is the safety net when client verify didn't complete)
                    try:
                        from . import notifications
                        if job.get("customer_email"):
                            total_amount = 0
                            with db.get_connection() as conn:
                                cursor = conn.cursor()
                                cursor.execute(
                                    "SELECT COALESCE(SUM(client_estimate_cents), 0) FROM jobs WHERE order_id = %s",
                                    (internal_order_id,)
                                )
                                total_amount = cursor.fetchone()[0]
                            if total_amount == 0:
                                total_amount = job.get("estimate_cents") or job.get("client_estimate_cents") or 0
                            
                            email_token = db.get_display_ticket(job.get("ticket", ""))
                            notifications.send_payment_confirmation(
                                email=job["customer_email"],
                                token=email_token,
                                estimate_cents=total_amount,
                                customer_name=job.get("sender"),
                                order_id=internal_order_id,
                                payment_id=razorpay_payment_id,
                                payment_method=payment_method
                            )
                            logger.info(f"Webhook: Sent confirmation email for ticket={job['ticket']}")
                        
                        # Send SMS if phone is set
                        if job.get("phone"):
                            from . import sms
                            sms.send_ticket_notification(phone=job["phone"], ticket=job["token"])
                    except Exception as notif_err:
                        logger.error(f"Webhook: Failed to send notification: {notif_err}")
                else:
                    logger.info(f"Webhook: Jobs already paid for order={internal_order_id} (client verify was faster)")
            else:
                logger.warning(f"Webhook: Could not resolve internal order_id for rp_payment={razorpay_payment_id}, rp_order={razorpay_order_id}")
    
    # Mark event as processed
    db.mark_webhook_processed(event_id)
    
    return {"status": "ok"}


@app.post("/api/jobs/status-check")
async def check_jobs_status(request: Request):
    """
    Check which tickets are already paid.
    
    Used by the frontend on page load to clean up stale cart items
    (e.g., when user closed tab during payment verification).
    Returns list of tickets that have status 'paid' or later.
    """
    try:
        data = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    tickets = data.get("tickets", [])
    if not tickets or not isinstance(tickets, list):
        return {"paid_tickets": []}
    
    # Limit to prevent abuse
    tickets = tickets[:20]
    
    paid_tickets = []
    with db.get_connection() as conn:
        cursor = conn.cursor()
        # Check which tickets have a non-draft/non-pending status
        placeholders = ','.join(['%s'] * len(tickets))
        cursor.execute(
            f"SELECT ticket FROM jobs WHERE ticket IN ({placeholders}) AND status IN ('paid', 'printed', 'delivered')",
            tuple(tickets)
        )
        paid_tickets = [row[0] for row in cursor.fetchall()]
    
    return {"paid_tickets": paid_tickets}

@app.get("/api/jobs/pending")
async def get_pending_paid_jobs(limit: int = 100, offset: int = 0, username: str = Depends(verify_staff_credentials)):
    """Get all paid jobs for the shopkeeper dashboard."""
    jobs = db.get_paid_jobs(limit=limit, offset=offset)
    return {"jobs": jobs, "count": len(jobs)}


@app.get("/api/jobs/token/{token}")
async def get_job_by_token(token: str, username: str = Depends(verify_staff_credentials)):
    """Get a job by its token."""
    job = db.get_job_by_token(token)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/jobs/{token}/print")
async def mark_job_printed(token: str, username: str = Depends(verify_staff_credentials)):
    """Mark a job as printed."""
    from . import notifications
    
    job = db.get_job_by_token(token)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job["status"] not in ["paid", "printed"]:
        raise HTTPException(status_code=400, detail=f"Cannot mark job as printed. Current status: {job['status']}")
    
    db.mark_job_printed(token)
    
    # Send notification if email is set
    if job.get("customer_email"):
        notifications.send_job_ready(
            email=job["customer_email"],
            token=token,
            customer_name=job.get("sender")
        )
        
    # Send SMS if phone is set
    if job.get("phone"):
        from . import sms
        sms.send_ready_notification(
            phone=job["phone"],
            ticket=token
        )
    
    logger.info(f"Job marked printed: token={token}")
    return {"status": "ok", "message": f"Job {token} marked as printed"}


@app.post("/api/jobs/{token}/deliver")
async def mark_job_delivered(token: str, username: str = Depends(verify_staff_credentials)):
    """Mark a job as delivered."""
    job = db.get_job_by_token(token)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job["status"] not in ["paid", "printed"]:
        raise HTTPException(status_code=400, detail=f"Cannot mark job as delivered. Current status: {job['status']}")
    
    db.mark_job_delivered(token)
    logger.info(f"Job marked delivered: token={token}")
    return {"status": "ok", "message": f"Job {token} marked as delivered"}


@app.post("/api/jobs/{token}/refund")
async def refund_job(token: str, username: str = Depends(verify_staff_credentials)):
    """Issue a refund for a paid job."""
    from . import razorpay as rp
    import uuid
    
    job = db.get_job_by_token(token)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job["status"] == "refunded":
        return {"status": "ok", "message": "Job already refunded"}
    
    if job["status"] not in ["paid", "printed"]:
        raise HTTPException(status_code=400, detail=f"Cannot refund job. Current status: {job['status']}")
    
    payment_id = job.get("payment_id")
    if not payment_id:
        raise HTTPException(status_code=400, detail="No payment ID found for this job")
    
    # Calculate refund amount
    refund_amount = job.get("estimate_cents") or job.get("client_estimate_cents") or 0
    if refund_amount <= 0:
        raise HTTPException(status_code=400, detail="Invalid refund amount")
    
    # Create refund via Razorpay (refunds are against payment_id, not order_id)
    refund_receipt = f"refund_{token}_{uuid.uuid4().hex[:6]}"
    refund = rp.create_refund(
        payment_id=payment_id,
        amount_cents=refund_amount,
        refund_note=f"Refund for job {token}",
        receipt=refund_receipt
    )
    if not refund:
        raise HTTPException(status_code=500, detail="Failed to create refund")
    
    # Update job and payment status
    rp_refund_id = refund.get("id") or refund_receipt
    db.mark_job_refunded(token, refund_id=rp_refund_id)
    if payment_id:
        db.update_payment_refunded(payment_id, rp_refund_id, refund_amount_cents=refund_amount)
    
    logger.info(f"Job refunded: token={token}, refund_id={rp_refund_id}, amount={refund_amount}")
    return {"status": "ok", "message": f"Refund processed for job {token}", "refund_id": rp_refund_id}


class ReconcilePaymentRequest(BaseModel):
    """Request to manually reconcile a stuck payment."""
    razorpay_payment_id: Optional[str] = None  # e.g. pay_Sis7C0VA0VRc21
    order_id: Optional[str] = None  # Internal order ID e.g. ORD20260428029
    ticket: Optional[str] = None  # Job ticket e.g. 20260428-030


@app.post("/api/admin/reconcile-payment")
async def reconcile_payment(
    data: ReconcilePaymentRequest,
    username: str = Depends(verify_staff_credentials)
):
    """
    Manually reconcile a stuck payment where the customer paid but the job
    didn't update to 'paid' status.
    
    Looks up the payment via Razorpay API or local records and force-updates
    the job status. Protected by staff credentials.
    
    Accepts one of: razorpay_payment_id, order_id, or ticket.
    """
    from . import razorpay as rp
    from . import notifications
    
    internal_order_id = data.order_id
    razorpay_payment_id = data.razorpay_payment_id
    
    # Strategy 1: If ticket is provided, look up the order_id from the job
    if data.ticket and not internal_order_id:
        job = db.get_job_by_ticket(data.ticket)
        if job and job.get("order_id"):
            internal_order_id = job["order_id"]
        elif job:
            # Job has no order_id — look it up from payments table via job_id
            with db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT order_id FROM payments WHERE job_id = %s LIMIT 1", (job["id"],))
                row = cursor.fetchone()
                if row:
                    internal_order_id = row[0]
    
    if not internal_order_id and not razorpay_payment_id:
        raise HTTPException(status_code=400, detail="Provide at least one of: order_id, razorpay_payment_id, or ticket")
    
    # Strategy 2: If we have a Razorpay payment ID, fetch details from Razorpay API
    payment_method = None
    if razorpay_payment_id and rp.is_configured():
        payment_details = rp.get_payment_details(razorpay_payment_id)
        if payment_details:
            payment_status = payment_details.get("status", "")
            payment_method = payment_details.get("method")
            rp_order_id = payment_details.get("order_id", "")
            
            # Verify payment is actually captured
            if payment_status not in ("captured", "authorized"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Payment is not confirmed on Razorpay (status: {payment_status}). Cannot reconcile."
                )
            
            # If we don't have internal_order_id, try to find it via Razorpay order
            if not internal_order_id and rp_order_id:
                rp_order = rp.get_order_details(rp_order_id)
                if rp_order:
                    # Receipt is set to internal_order_id at creation time
                    internal_order_id = rp_order.get("receipt") or rp_order.get("notes", {}).get("order_id")
        else:
            logger.warning(f"Reconcile: Could not fetch Razorpay payment {razorpay_payment_id}")
    
    if not internal_order_id:
        raise HTTPException(status_code=404, detail="Could not determine the internal order ID. Provide order_id directly.")
    
    # Try the standard update path (which now includes fallback lookup)
    job = db.update_payment_info(internal_order_id, razorpay_payment_id or "reconciled_manually")
    
    if not job:
        # Check if already paid
        with db.get_connection() as conn:
            cursor = conn.cursor(cursor_factory=db.psycopg2.extras.RealDictCursor)
            cursor.execute("SELECT * FROM jobs WHERE order_id = %s AND status = 'paid' LIMIT 1", (internal_order_id,))
            row = cursor.fetchone()
            if row:
                return {"status": "ok", "message": "Job is already paid", "ticket": row["ticket"]}
        
        raise HTTPException(status_code=404, detail=f"No job found for order_id={internal_order_id}. Manual DB update may be required.")
    
    # Update payment audit record
    db.update_payment_verified(internal_order_id, razorpay_payment_id or "reconciled_manually", payment_method=payment_method)
    
    # Send confirmation email if not already sent
    if job.get("customer_email"):
        try:
            total_amount = 0
            with db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COALESCE(SUM(client_estimate_cents), 0) FROM jobs WHERE order_id = %s",
                    (internal_order_id,)
                )
                total_amount = cursor.fetchone()[0]
            
            if total_amount == 0:
                total_amount = job.get("estimate_cents") or job.get("client_estimate_cents") or 0
            
            email_token = db.get_display_ticket(job.get("ticket", ""))
            notifications.send_payment_confirmation(
                email=job["customer_email"],
                token=email_token,
                estimate_cents=total_amount,
                customer_name=job.get("sender"),
                order_id=internal_order_id,
                payment_id=razorpay_payment_id,
                payment_method=payment_method
            )
        except Exception as e:
            logger.warning(f"Reconcile: Failed to send notification: {e}")
    
    logger.info(f"Payment reconciled: ticket={job['ticket']}, order_id={internal_order_id}, payment_id={razorpay_payment_id}, by={username}")
    
    return {
        "status": "ok",
        "message": f"Payment reconciled successfully for ticket {job['ticket']}",
        "ticket": job["ticket"],
        "order_id": internal_order_id
    }


# ============== SMS API ==============

@app.post("/api/sms/send")
async def send_sms_message(phone: str = Form(...), message: str = Form(...)):
    """
    Send an SMS notification via GSM modem.
    Requires USB GSM modem hardware.
    """
    try:
        from .sms import send_sms, log_sms, is_modem_available
        
        if not is_modem_available():
            return JSONResponse(
                status_code=503,
                content={"status": "error", "message": "GSM modem not available"}
            )
        
        result = send_sms(phone, message)
        log_sms(phone, message, 'sent' if result['success'] else 'failed', error=result.get('error'))
        
        if result['success']:
            return {"status": "ok", "message": "SMS sent", "message_id": result.get('message_id')}
        else:
            return JSONResponse(
                status_code=500,
                content={"status": "error", "message": result.get('error', 'Failed to send SMS')}
            )
    except ImportError:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "message": "SMS module not available"}
        )


@app.get("/api/sms/status")
async def get_sms_status():
    """Check SMS/GSM modem availability."""
    try:
        from .sms import is_modem_available
        available = is_modem_available()
        return {"available": available, "status": "ready" if available else "unavailable"}
    except ImportError:
        return {"available": False, "status": "module_not_installed"}


# ============== Health Check ==============

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat()
    }


# ============== Admin Dashboard ==============

import hashlib
import hmac

# In-memory store for active admin session tokens (SEC-07: random tokens instead of deterministic)
_admin_sessions: set = set()

def _create_admin_session() -> str:
    """Generate a cryptographically random admin session token."""
    token = secrets.token_hex(32)
    _admin_sessions.add(token)
    # Limit stored sessions to prevent memory leak (keep last 10)
    while len(_admin_sessions) > 10:
        _admin_sessions.pop()
    return token


def verify_admin_session(request: Request) -> str:
    """Verify admin session cookie. Raises 401 if not authenticated."""
    session = request.cookies.get("admin_session")
    if not session or session not in _admin_sessions:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return settings.admin_username


class AdminLoginRequest(BaseModel):
    username: str
    password: str


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, key: Optional[str] = None):
    """Serve the admin dashboard — hidden behind access key."""
    access_key = settings.staff_access_key
    if access_key:
        cookie_key = request.cookies.get("staff_access")
        if key != access_key and cookie_key != access_key:
            return HTMLResponse(content="<h1>404 — Page not found</h1>", status_code=404)
    
    frontend_path = Path(__file__).parent.parent / "frontend" / "admin.html"
    if frontend_path.exists():
        response = HTMLResponse(content=frontend_path.read_text(), status_code=200)
        if key == access_key and access_key:
            response.set_cookie("staff_access", access_key, max_age=30*24*3600, httponly=True, samesite="lax")
        return response
    return HTMLResponse(content="<h1>404 — Page not found</h1>", status_code=404)


@app.post("/api/admin/login")
@limiter.limit("5/minute")
async def admin_login(request: Request, data: AdminLoginRequest, response: Response):
    """Authenticate admin and set session cookie. Rate-limited + brute-force protected."""
    client_ip = get_remote_address(request)
    _check_login_lockout(client_ip)
    
    if (secrets.compare_digest(data.username, settings.admin_username) and
            secrets.compare_digest(data.password, settings.admin_password)):
        _clear_login_attempts(client_ip)
        response.set_cookie(
            key="admin_session",
            value=_create_admin_session(),
            httponly=True,
            samesite="lax",
            secure=True,
            max_age=86400,  # 24 hours
        )
        return {"status": "ok"}
    _record_failed_login(client_ip)
    raise HTTPException(status_code=401, detail="Invalid credentials")


@app.post("/api/admin/logout")
async def admin_logout(request: Request, response: Response):
    """Clear admin session cookie and invalidate server-side token."""
    session = request.cookies.get("admin_session")
    if session:
        _admin_sessions.discard(session)
    response.delete_cookie(key="admin_session")
    return {"status": "ok"}


@app.get("/api/admin/check")
async def admin_check(request: Request):
    """Check if admin session is valid."""
    try:
        verify_admin_session(request)
        return {"authenticated": True}
    except HTTPException:
        return JSONResponse(content={"authenticated": False}, status_code=401)


@app.get("/api/admin/stats")
async def admin_stats(request: Request):
    """Get aggregate admin statistics."""
    verify_admin_session(request)
    try:
        stats = db.get_admin_stats()
        return stats
    except Exception as e:
        logger.error(f"Admin stats error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/admin/users")
async def admin_users(request: Request):
    """Get all users with upload and payment stats."""
    verify_admin_session(request)
    try:
        users = db.get_all_users_with_stats()
        return {"users": users, "total": len(users)}
    except Exception as e:
        logger.error(f"Admin users error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/admin/payments")
async def admin_payments(request: Request, limit: int = 200, offset: int = 0):
    """Get full payment audit trail."""
    verify_admin_session(request)
    try:
        result = db.get_all_payments_admin(limit=limit, offset=offset)
        return result
    except Exception as e:
        logger.error(f"Admin payments error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/admin/jobs")
async def admin_jobs(request: Request, limit: int = 200, offset: int = 0, status: Optional[str] = None):
    """Get all jobs with status breakdown."""
    verify_admin_session(request)
    try:
        breakdown = db.get_job_status_breakdown()
        jobs_result = db.get_all_jobs_admin(limit=limit, offset=offset, status_filter=status)
        return {
            "breakdown": breakdown,
            **jobs_result,
        }
    except Exception as e:
        logger.error(f"Admin jobs error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/admin/errors")
async def admin_errors(request: Request, lines: int = 200):
    """Get recent error/warning logs from app.log."""
    verify_admin_session(request)
    try:
        log_path = settings.log_dir / "app.log"
        if not log_path.exists():
            return {"errors": [], "total": 0}
        
        all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        error_lines = [
            line for line in all_lines
            if "ERROR" in line or "WARNING" in line or "CRITICAL" in line
        ]
        
        recent = error_lines[-lines:] if len(error_lines) > lines else error_lines
        recent.reverse()
        
        return {
            "errors": recent,
            "total": len(error_lines),
            "log_size_bytes": log_path.stat().st_size,
        }
    except Exception as e:
        logger.error(f"Admin errors endpoint error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/admin/logs")
async def admin_logs(request: Request, lines: int = 500):
    """Get full application log (all levels) for live log viewer."""
    verify_admin_session(request)
    try:
        log_path = settings.log_dir / "app.log"
        if not log_path.exists():
            return {"logs": [], "total": 0, "log_size_bytes": 0}
        
        all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        recent = all_lines[-lines:] if len(all_lines) > lines else all_lines
        recent.reverse()
        
        return {
            "logs": recent,
            "total": len(all_lines),
            "log_size_bytes": log_path.stat().st_size,
        }
    except Exception as e:
        logger.error(f"Admin logs endpoint error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/admin/trends")
async def admin_trends(request: Request, days: int = 30):
    """Get daily trends for charts (revenue, prints, signups)."""
    verify_admin_session(request)
    try:
        trends = db.get_admin_trends(days=days)
        return trends
    except Exception as e:
        logger.error(f"Admin trends error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============== Main Entry Point ==============

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.app:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True
    )
