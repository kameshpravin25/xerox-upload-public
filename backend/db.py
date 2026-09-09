"""
Database helpers for the Xerox Hotspot Upload system.
Uses PostgreSQL (Supabase) for production.
"""

import time
import random
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any, Callable, TypeVar
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool

from .config import settings

logger = logging.getLogger(__name__)

# IST Timezone (UTC+5:30) - for consistent time display
IST = timezone(timedelta(hours=5, minutes=30))

# ─── Connection Pool ───
_pool = None


def _get_pool():
    """Get or initialize the connection pool (lazy singleton)."""
    global _pool
    if _pool is None or _pool.closed:
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL environment variable is not set")
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=20,
            dsn=settings.database_url,
        )
        logger.info("Database connection pool initialized (min=2, max=20)")
    return _pool


def close_pool():
    """Close the connection pool (call on app shutdown)."""
    global _pool
    if _pool and not _pool.closed:
        _pool.closeall()
        logger.info("Database connection pool closed")
        _pool = None


def get_ist_now() -> datetime:
    """Get current time in IST timezone."""
    return datetime.now(IST)


def get_ist_isoformat() -> str:
    """Get current time in IST as ISO format string."""
    return datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S")


T = TypeVar('T')


def execute_with_retry(func: Callable[[], T], max_retries: int = 5, base_delay: float = 0.1) -> T:
    """Execute a database function with retry logic for connection issues."""
    last_error = None
    
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            last_error = e
            error_msg = str(e).lower()
            
            if any(keyword in error_msg for keyword in ["connection", "timeout", "closed"]):
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 0.1)
                    logger.warning(f"Database error, retry {attempt + 1}/{max_retries} after {delay:.2f}s: {e}")
                    time.sleep(delay)
                else:
                    logger.error(f"Database error after {max_retries} retries")
                    raise
            else:
                raise
    
    raise last_error


@contextmanager
def get_connection():
    """Context manager for pooled PostgreSQL database connections.
    
    Validates the connection is alive before use. If a stale/dead connection
    is returned from the pool (e.g. server dropped it due to idle timeout),
    it is discarded and a fresh connection is obtained.
    """
    pool = _get_pool()
    conn = pool.getconn()
    
    # Health check: detect stale connections dropped by server idle timeout
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.autocommit = False
    except Exception:
        # Connection is dead — discard it and get a fresh one
        logger.warning("Stale DB connection detected, reconnecting...")
        try:
            pool.putconn(conn, close=True)
        except Exception:
            pass
        conn = pool.getconn()
        conn.autocommit = False
    
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def _run_column_migrations() -> None:
    """Run all column migrations using a separate autocommit connection.
    
    Each ALTER TABLE runs independently so failures don't affect each other.
    Uses autocommit=True so each statement is its own transaction.
    """
    if not settings.database_url:
        return
    
    migrate_conn = psycopg2.connect(settings.database_url)
    migrate_conn.autocommit = True
    migrate_cursor = migrate_conn.cursor()
    
    def _add_column(table: str, col: str, col_type: str = "TEXT", default: str = "NULL") -> None:
        try:
            migrate_cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type} DEFAULT {default}")
            logger.info(f"Added column {table}.{col}")
        except Exception:
            pass  # Column already exists
    
    def _rename_column(table: str, old_col: str, new_col: str) -> None:
        try:
            migrate_cursor.execute(f"ALTER TABLE {table} RENAME COLUMN {old_col} TO {new_col}")
            logger.info(f"Renamed {table}.{old_col} -> {new_col}")
        except Exception:
            pass  # Already renamed or doesn't exist
    
    try:
        # ── Jobs table migrations ──
        _add_column("jobs", "storage_type", "TEXT", "'supabase'")
        _add_column("jobs", "drive_file_id", "TEXT")
        _add_column("jobs", "drive_processed_file_id", "TEXT")
        _add_column("jobs", "payment_gateway", "TEXT", "'cashfree'")
        
        # Rename razorpay columns -> generic names
        _rename_column("jobs", "razorpay_order_id", "order_id")
        _rename_column("jobs", "razorpay_payment_id", "payment_id")
        _rename_column("jobs", "razorpay_signature", "payment_signature")
        
        # ── Payments table migrations ──
        _rename_column("payments", "razorpay_order_id", "order_id")
        _rename_column("payments", "razorpay_payment_id", "payment_id")
        _rename_column("payments", "razorpay_signature", "payment_signature")
        _add_column("payments", "payment_gateway", "TEXT", "'cashfree'")
        _add_column("payments", "payment_method", "TEXT")
        _add_column("payments", "error_code", "TEXT")
        _add_column("payments", "error_message", "TEXT")
        _add_column("payments", "refund_amount_cents", "INTEGER")
        _add_column("payments", "customer_email", "TEXT")
        _add_column("payments", "customer_phone", "TEXT")
        _add_column("payments", "ip_address", "TEXT")
        
        # ── Users table migrations ──
        _add_column("users", "google_refresh_token", "TEXT")
        _add_column("users", "google_drive_folder_id", "TEXT")
        
        logger.info("Column migrations completed")
    finally:
        migrate_cursor.close()
        migrate_conn.close()


def init_db() -> None:
    """Initialize the database with required tables."""
    with get_connection() as conn:
        cursor = conn.cursor()
        
        # Jobs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id SERIAL PRIMARY KEY,
                ticket TEXT UNIQUE NOT NULL,
                token TEXT UNIQUE,
                sender TEXT NOT NULL,
                phone TEXT,
                customer_email TEXT,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'created',
                created_at TEXT NOT NULL,
                archived_at TEXT,
                color_mode TEXT DEFAULT 'bw',
                duplex INTEGER DEFAULT 0,
                paper_size TEXT DEFAULT 'A4',
                binding TEXT DEFAULT 'none',
                copies INTEGER DEFAULT 1,
                selected_pages TEXT,
                rotations TEXT,
                estimate_cents INTEGER,
                client_estimate_cents INTEGER,
                order_id TEXT,
                payment_id TEXT,
                payment_signature TEXT,
                payment_gateway TEXT DEFAULT 'cashfree',
                paid_at TEXT,
                printed_at TEXT,
                delivered_at TEXT,
                refunded_at TEXT,
                refund_id TEXT,
                processing_status TEXT DEFAULT 'pending',
                processed_filepath TEXT,
                user_id INTEGER,
                storage_type TEXT DEFAULT 'supabase',
                drive_file_id TEXT,
                drive_processed_file_id TEXT
            )
        """)
        
        # Add new columns to existing tables (safe for existing deployments)
        # Use a separate autocommit connection for migrations
        _run_column_migrations()
        
        
        # Users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT,
                name TEXT,
                google_id TEXT UNIQUE,
                google_refresh_token TEXT,
                google_drive_folder_id TEXT,
                created_at TEXT NOT NULL,
                last_login TEXT
            )
        """)
        
        # Users table migrations are handled by _run_column_migrations()
        
        # Payments table — production-grade audit trail
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                job_id INTEGER REFERENCES jobs(id),
                order_id TEXT NOT NULL,
                payment_id TEXT,
                payment_signature TEXT,
                payment_gateway TEXT DEFAULT 'cashfree',
                payment_method TEXT,
                amount_cents INTEGER NOT NULL DEFAULT 0,
                currency TEXT DEFAULT 'INR',
                status TEXT DEFAULT 'created',
                error_code TEXT,
                error_message TEXT,
                refund_id TEXT,
                refund_amount_cents INTEGER,
                customer_email TEXT,
                customer_phone TEXT,
                ip_address TEXT,
                created_at TEXT NOT NULL,
                verified_at TEXT,
                refunded_at TEXT
            )
        """)
        
        # Webhook events table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS webhook_events (
                id SERIAL PRIMARY KEY,
                event_id TEXT UNIQUE,
                event_type TEXT,
                payload TEXT,
                status TEXT DEFAULT 'received',
                received_at TEXT NOT NULL,
                processed_at TEXT
            )
        """)
        
        # SMS log table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sms_log (
                id SERIAL PRIMARY KEY,
                phone TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                job_ticket TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                error TEXT
            )
        """)
        
        # Shop settings table — key-value store for shop configuration
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS shop_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT
            )
        """)
        
        # Indexes — jobs
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_ticket ON jobs(ticket)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_token ON jobs(token)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_order_id ON jobs(order_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_payment_id ON jobs(payment_id)")
        
        # Indexes — payments
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_payments_order_id ON payments(order_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_payments_payment_id ON payments(payment_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_payments_job_id ON payments(job_id)")
        
        # Indexes — webhooks
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_webhook_event_id ON webhook_events(event_id)")
        
        logger.info("Database tables initialized (PostgreSQL)")


def generate_ticket() -> str:
    """
    Generate a daily ticket number with date prefix.
    Format: YYYYMMDD-NNN (e.g., '20260203-001')
    Resets to 001 at midnight IST.
    
    Note: Considers ALL tickets (main and sub-items) to avoid number collisions.
    If sub-items like '20260203-010-2' exist, the number 010 is considered taken
    even if the main ticket '20260203-010' was deleted.
    """
    today = get_ist_now().date()
    date_prefix = today.strftime("%Y%m%d")
    
    with get_connection() as conn:
        cursor = conn.cursor()
        
        # Extract the NNN portion (second dash-delimited segment) from ALL tickets
        # for today — both main tickets (YYYYMMDD-NNN) and sub-items (YYYYMMDD-NNN-M).
        # This prevents reusing a number if the main ticket was deleted but sub-items remain.
        cursor.execute("""
            SELECT MAX(CAST(SPLIT_PART(ticket, '-', 2) AS INTEGER))
            FROM jobs 
            WHERE ticket LIKE %s
            AND SPLIT_PART(ticket, '-', 2) ~ '^[0-9]+$'
        """, (f"{date_prefix}-%",))
        
        result = cursor.fetchone()
        last_ticket = result[0] if result and result[0] is not None else 0
        next_ticket = last_ticket + 1
        
        return f"{date_prefix}-{next_ticket:03d}"


def get_display_ticket(ticket: str) -> str:
    """Extract the display-friendly 3-digit portion from a full ticket.
    
    Ticket format: YYYYMMDD-NNN (main) or YYYYMMDD-NNN-M (bulk sub-item).
    Always returns the NNN part (second segment), not the suffix.
    """
    if ticket and '-' in ticket:
        parts = ticket.split('-')
        # Always return the second segment (the NNN main ticket number)
        # e.g. "20260502-010" → "010", "20260502-010-2" → "010"
        if len(parts) >= 2:
            return parts[1]
    return ticket


def create_job(
    ticket: str,
    sender: str,
    phone: Optional[str],
    filename: str,
    filepath: str,
    size_bytes: int,
    color_mode: str = "bw",
    duplex: bool = False,
    paper_size: str = "A4",
    binding: str = "none",
    copies: int = 1,
    selected_pages: Optional[str] = None,
    rotations: Optional[str] = None,
    client_estimate_cents: Optional[int] = None,
    user_id: Optional[int] = None,
    customer_email: Optional[str] = None,
    status: str = "pending"
) -> Dict[str, Any]:
    """Create a new job entry in the database."""
    created_at = get_ist_isoformat()
    
    def _create():
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute("""
                INSERT INTO jobs (
                    ticket, sender, phone, filename, filepath, size_bytes, 
                    status, created_at, color_mode, duplex, paper_size, 
                    binding, copies, selected_pages, rotations, client_estimate_cents,
                    user_id, customer_email
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                ticket, sender, phone, filename, filepath, size_bytes,
                status, created_at, color_mode, 1 if duplex else 0, paper_size,
                binding, copies, selected_pages, rotations, client_estimate_cents,
                user_id, customer_email
            ))
            result = cursor.fetchone()
            return result['id']

    job_id = execute_with_retry(_create)
    
    return {
        "id": job_id,
        "ticket": ticket,
        "sender": sender,
        "phone": phone,
        "filename": filename,
        "filepath": filepath,
        "size_bytes": size_bytes,
        "status": status,
        "created_at": created_at,
        "archived_at": None,
        "color_mode": color_mode,
        "duplex": duplex,
        "paper_size": paper_size,
        "binding": binding,
        "copies": copies,
        "selected_pages": selected_pages,
        "rotations": rotations,
        "client_estimate_cents": client_estimate_cents,
        "user_id": user_id,
        "customer_email": customer_email
    }


def get_all_jobs(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get all jobs, optionally filtered by status. Shows last 5 days only."""
    cutoff = (get_ist_now() - timedelta(days=5)).isoformat()
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if status:
            cursor.execute(
                """SELECT * FROM jobs 
                WHERE status = %s AND created_at >= %s
                ORDER BY created_at DESC""",
                (status, cutoff)
            )
        else:
            # Only show jobs from last 5 days that have been paid — drafts/pending_payment are hidden from staff
            cursor.execute("""
                SELECT * FROM jobs 
                WHERE status NOT IN ('draft', 'created', 'pending_payment')
                AND created_at >= %s
                ORDER BY created_at DESC
            """, (cutoff,))
        
        rows = cursor.fetchall()
        jobs = []
        for row in rows:
            job = dict(row)
            job['display_ticket'] = get_display_ticket(job.get('ticket', ''))
            jobs.append(job)
        return jobs


def get_job_by_ticket(ticket: str) -> Optional[Dict[str, Any]]:
    """Get a single job by its ticket number."""
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM jobs WHERE ticket = %s", (ticket,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_jobs_by_tickets(tickets: List[str]) -> List[dict]:
    """Get multiple jobs by their ticket numbers."""
    if not tickets:
        return []
        
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        placeholders = ','.join(['%s'] * len(tickets))
        cursor.execute(
            f"SELECT * FROM jobs WHERE ticket IN ({placeholders})",
            tickets
        )
        return [dict(row) for row in cursor.fetchall()]


def get_jobs_by_group_ticket(parent_ticket: str) -> List[str]:
    """
    Get all ticket numbers in a bulk order group.
    Returns list of tickets including the parent and all sub-items.
    
    For example, if parent_ticket is "20260205-004":
    - Returns ["20260205-004", "20260205-004-1", "20260205-004-2", ...]
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        # Find all tickets that start with the parent ticket (including sub-items)
        cursor.execute(
            "SELECT ticket FROM jobs WHERE ticket = %s OR ticket LIKE %s",
            (parent_ticket, f"{parent_ticket}-%")
        )
        return [row[0] for row in cursor.fetchall()]


def update_job_status(ticket: str, status: str) -> bool:
    """Update the status of a job."""
    archived_at = get_ist_isoformat() if status == "archived" else None
    
    def _update():
        with get_connection() as conn:
            cursor = conn.cursor()
            if archived_at:
                cursor.execute(
                    "UPDATE jobs SET status = %s, archived_at = %s WHERE ticket = %s",
                    (status, archived_at, ticket)
                )
            else:
                cursor.execute(
                    "UPDATE jobs SET status = %s WHERE ticket = %s",
                    (status, ticket)
                )
            return cursor.rowcount > 0
            
    return execute_with_retry(_update)


def delete_job(ticket: str) -> bool:
    """Delete a job from the database.
    
    Detaches any payment records first to avoid FK constraint violations.
    Payment records are preserved with job_id = NULL for audit trail.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        # Detach payment records before deleting (preserves audit trail)
        cursor.execute(
            "UPDATE payments SET job_id = NULL WHERE job_id = (SELECT id FROM jobs WHERE ticket = %s)",
            (ticket,)
        )
        cursor.execute("DELETE FROM jobs WHERE ticket = %s", (ticket,))
        return cursor.rowcount > 0


def get_pending_jobs_count() -> int:
    """Get the count of pending jobs."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM jobs WHERE status = 'pending'")
        return cursor.fetchone()[0]


def cleanup_old_archived_jobs(retention_days: int) -> int:
    """Delete archived jobs older than retention_days.
    
    Detaches payment records first to avoid FK constraint violations.
    """
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
    
    with get_connection() as conn:
        cursor = conn.cursor()
        # Detach payment records before deleting (preserves audit trail)
        cursor.execute(
            """UPDATE payments SET job_id = NULL 
               WHERE job_id IN (SELECT id FROM jobs WHERE status = 'archived' AND archived_at < %s)""",
            (cutoff,)
        )
        cursor.execute(
            "DELETE FROM jobs WHERE status = 'archived' AND archived_at < %s",
            (cutoff,)
        )
        return cursor.rowcount


def cleanup_old_jobs(retention_days: int = 1) -> Dict[str, Any]:
    """Clean up old jobs: delete files but preserve records for history.
    
    Drive files for PAID jobs are preserved permanently (they stay in user's Google Drive).
    Supabase files and unpaid Drive files are cleaned up after retention_days.
    """
    from datetime import timedelta
    
    cutoff = (get_ist_now() - timedelta(days=retention_days)).isoformat()
    
    files_to_delete = []
    drive_files_to_delete = []  # List of (user_id, drive_file_id) tuples
    drive_files_preserved = 0
    cleaned_count = 0
    payments_deleted = 0
    
    # Statuses that indicate the job was paid — Drive files for these are kept
    paid_statuses = ('paid', 'printed', 'delivered', 'downloaded', 'refunded')
    
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Find old jobs that still have file references
        # Check for both NULL and empty string since cleanup sets filepath = ''
        cursor.execute(
            """SELECT id, ticket, filepath, processed_filepath, 
                      storage_type, drive_file_id, drive_processed_file_id, user_id, status 
               FROM jobs WHERE created_at < %s 
               AND (COALESCE(filepath, '') != '' OR processed_filepath IS NOT NULL 
                    OR drive_file_id IS NOT NULL OR drive_processed_file_id IS NOT NULL)""",
            (cutoff,)
        )
        
        rows = cursor.fetchall()
        job_ids = []
        drive_preserved_job_ids = []  # Jobs where Drive files are kept
        for row in rows:
            job_ids.append(row['id'])
            
            if row.get('storage_type') == 'drive':
                # Paid Drive files are preserved permanently
                if row.get('status') in paid_statuses:
                    drive_files_preserved += 1
                    drive_preserved_job_ids.append(row['id'])
                else:
                    # Unpaid Drive files (draft, pending_payment, etc.) are cleaned up
                    if row.get('drive_file_id'):
                        drive_files_to_delete.append((row['user_id'], row['drive_file_id']))
                    if row.get('drive_processed_file_id'):
                        drive_files_to_delete.append((row['user_id'], row['drive_processed_file_id']))
            else:
                # Supabase files are always cleaned up
                if row.get('filepath'):
                    files_to_delete.append(row['filepath'])
                if row.get('processed_filepath'):
                    files_to_delete.append(row['processed_filepath'])
        
        # Payment records are PRESERVED permanently for audit/accounting/refunds.
        # We no longer delete them during cleanup.
        
        # For jobs where Drive files are preserved: clear only Supabase refs, keep Drive refs
        if drive_preserved_job_ids:
            cursor.execute(
                "UPDATE jobs SET filepath = '', processed_filepath = NULL, status = 'expired' WHERE id = ANY(%s)",
                (drive_preserved_job_ids,)
            )
        
        # For all other jobs: clear ALL file references and mark as expired
        other_job_ids = [jid for jid in job_ids if jid not in drive_preserved_job_ids]
        if other_job_ids:
            cursor.execute(
                "UPDATE jobs SET filepath = '', processed_filepath = NULL, drive_file_id = NULL, drive_processed_file_id = NULL, status = 'expired' WHERE id = ANY(%s)",
                (other_job_ids,)
            )
        cleaned_count = len(job_ids)
        
        # Fully delete guest (anonymous) job records — no history needed
        # First, detach any payment records (set job_id=NULL) so FK constraint is satisfied
        cursor.execute(
            """UPDATE payments SET job_id = NULL 
               WHERE job_id IN (SELECT id FROM jobs WHERE user_id IS NULL AND created_at < %s)""",
            (cutoff,)
        )
        payments_detached = cursor.rowcount
        
        cursor.execute(
            "DELETE FROM jobs WHERE user_id IS NULL AND created_at < %s",
            (cutoff,)
        )
        guest_deleted = cursor.rowcount
    
    return {
        "deleted_count": cleaned_count,
        "guest_deleted": guest_deleted,
        "payments_preserved": True,
        "payments_detached": payments_detached,
        "files_to_delete": files_to_delete,
        "drive_files_to_delete": drive_files_to_delete,
        "drive_files_preserved": drive_files_preserved,
        "cutoff": cutoff
    }


def get_user_jobs(user_id: int, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Get jobs for a specific user with pagination. Excludes draft and pending_payment jobs."""
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT * FROM jobs 
            WHERE user_id = %s AND status NOT IN ('draft', 'pending_payment')
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """, (user_id, limit, offset))
        
        jobs = []
        for row in cursor.fetchall():
            job = dict(row)
            job['display_ticket'] = get_display_ticket(job.get('ticket', ''))
            jobs.append(job)
        return jobs


def get_user_job_count(user_id: int) -> int:
    """Get total job count for a user. Excludes draft and pending_payment jobs."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM jobs WHERE user_id = %s AND status NOT IN ('draft', 'pending_payment')", (user_id,))
        return cursor.fetchone()[0]


def get_user_total_spent(user_id: int) -> int:
    """Get total amount spent by a user in cents. Only counts actually paid jobs."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(SUM(client_estimate_cents), 0) FROM jobs 
            WHERE user_id = %s AND status IN ('paid', 'printed', 'delivered', 'downloaded', 'expired')
            AND client_estimate_cents IS NOT NULL
        """, (user_id,))
        return cursor.fetchone()[0]


def update_job_processing_status(ticket: str, processing_status: str, processed_filepath: Optional[str] = None) -> bool:
    """Update the processing status of a job."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE jobs SET processing_status = %s, processed_filepath = %s WHERE ticket = %s",
            (processing_status, processed_filepath, ticket)
        )
        return cursor.rowcount > 0


def update_job_estimate(ticket: str, estimate_cents: int, page_count: Optional[int] = None) -> bool:
    """Update job's billing estimate with server-calculated values.
    
    Called after office doc → PDF conversion when the server determines
    the actual page count and recalculates the price.
    This overwrites the client estimate and becomes the billing source of truth.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE jobs SET client_estimate_cents = %s WHERE ticket = %s",
            (estimate_cents, ticket)
        )
        return cursor.rowcount > 0


# ============== Token and Payment Functions ==============

def generate_order_id() -> str:
    """Generate a professional order ID like ORD20260321001.
    
    Format: ORD + YYYYMMDD + 3-digit daily sequential number.
    Queries the database to find today's highest order number and increments it.
    Uses PostgreSQL advisory lock to prevent race conditions under concurrency.
    """
    from datetime import datetime
    
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"ORD{today}"
    
    # Use advisory lock keyed on the date to serialize order ID generation
    # This prevents two concurrent requests from getting the same sequence number
    lock_key = int(today) % 2147483647  # Fit into int4 range for pg_advisory_xact_lock
    
    with get_connection() as conn:
        cursor = conn.cursor()
        # Acquire advisory lock (released automatically at end of transaction/connection)
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
        
        # Find the highest sequential number for today across both tables
        cursor.execute(
            """SELECT order_id FROM (
                SELECT order_id FROM jobs WHERE order_id LIKE %s
                UNION
                SELECT order_id FROM payments WHERE order_id LIKE %s
            ) combined ORDER BY order_id DESC LIMIT 1""",
            (f"{prefix}%", f"{prefix}%")
        )
        row = cursor.fetchone()
        if row and row[0]:
            try:
                last_seq = int(row[0].replace(prefix, ""))
                next_seq = last_seq + 1
            except (ValueError, IndexError):
                next_seq = 1
        else:
            next_seq = 1
        
        return f"{prefix}{next_seq:03d}"


def generate_token() -> str:
    """Generate a unique 6-character alphanumeric token for payment tracking."""
    import string
    chars = string.ascii_uppercase + string.digits
    while True:
        token = ''.join(random.choices(chars, k=6))
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM jobs WHERE token = %s", (token,))
            if cursor.fetchone()[0] == 0:
                return token


def get_job_by_token(token: str) -> Optional[Dict[str, Any]]:
    """Get a single job by its token."""
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM jobs WHERE token = %s", (token,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_paid_jobs(limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """Get all paid jobs for the shopkeeper dashboard."""
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT * FROM jobs 
            WHERE status IN ('paid', 'printed', 'delivered', 'refunded')
            ORDER BY paid_at DESC
            LIMIT %s OFFSET %s
        """, (limit, offset))
        return [dict(row) for row in cursor.fetchall()]


def update_job_with_order(ticket: str, order_id: str, estimate_cents: int, token: str) -> bool:
    """Update job with payment order details."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE jobs 
            SET order_id = %s, estimate_cents = %s, token = %s, status = 'pending_payment'
            WHERE ticket = %s
        """, (order_id, estimate_cents, token, ticket))
        return cursor.rowcount > 0


def find_order_by_receipt(receipt: str) -> Optional[str]:
    """Find internal order_id by matching the receipt field.
    
    The Razorpay order receipt is set to our internal order_id during creation.
    This function looks it up from the payments table as a fallback for webhook processing.
    """
    if not receipt:
        return None
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT order_id FROM payments WHERE order_id = %s LIMIT 1", (receipt,))
        row = cursor.fetchone()
        if row:
            return row[0]
        # Also check jobs table directly
        cursor.execute("SELECT order_id FROM jobs WHERE order_id = %s LIMIT 1", (receipt,))
        row = cursor.fetchone()
        return row[0] if row else None


def update_payment_info(order_id: str, payment_id: str, signature: str = "") -> Optional[Dict[str, Any]]:
    """Update job with verified payment info and mark as paid.
    
    Primary lookup: finds jobs by order_id directly.
    Fallback lookup: if no jobs found by order_id (e.g., order_id was cleared by
    an older version of revert-to-draft), looks up linked job_ids from the payments
    table and updates those jobs instead. This prevents payments from being lost
    when a race condition occurs between modal dismiss and UPI payment completion.
    """
    paid_at = get_ist_isoformat()
    
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Primary: update jobs that have matching order_id
        cursor.execute("""
            UPDATE jobs 
            SET payment_id = %s, payment_signature = %s, status = 'paid', paid_at = %s
            WHERE order_id = %s AND status != 'paid'
        """, (payment_id, signature, paid_at, order_id))
        
        if cursor.rowcount > 0:
            cursor.execute("SELECT * FROM jobs WHERE order_id = %s LIMIT 1", (order_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        
        # Fallback: order_id might have been cleared from jobs (race condition).
        # Look up the linked job_ids from the payments table instead.
        cursor.execute(
            "SELECT job_id FROM payments WHERE order_id = %s AND job_id IS NOT NULL",
            (order_id,)
        )
        payment_rows = cursor.fetchall()
        if payment_rows:
            job_ids = [r['job_id'] for r in payment_rows]
            logger.info(f"Payment fallback: found {len(job_ids)} jobs via payments table for order={order_id}")
            cursor.execute("""
                UPDATE jobs 
                SET payment_id = %s, payment_signature = %s, status = 'paid', 
                    paid_at = %s, order_id = %s
                WHERE id = ANY(%s) AND status != 'paid'
            """, (payment_id, signature, paid_at, order_id, job_ids))
            
            if cursor.rowcount > 0:
                cursor.execute("SELECT * FROM jobs WHERE id = ANY(%s) LIMIT 1", (job_ids,))
                row = cursor.fetchone()
                return dict(row) if row else None
    
    return None


def mark_job_printed(token: str) -> bool:
    """Mark a job as printed."""
    printed_at = get_ist_isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE jobs SET status = 'printed', printed_at = %s WHERE token = %s",
            (printed_at, token)
        )
        return cursor.rowcount > 0


def mark_job_delivered(token: str) -> bool:
    """Mark a job as delivered."""
    delivered_at = get_ist_isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE jobs SET status = 'delivered', delivered_at = %s WHERE token = %s",
            (delivered_at, token)
        )
        return cursor.rowcount > 0


def mark_job_refunded(token: str, refund_id: Optional[str] = None) -> bool:
    """Mark a job as refunded and store the refund_id."""
    refunded_at = get_ist_isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE jobs SET status = 'refunded', refunded_at = %s, refund_id = %s WHERE token = %s",
            (refunded_at, refund_id, token)
        )
        return cursor.rowcount > 0


# ============== Webhook Event Functions ==============

def check_webhook_processed(event_id: str) -> bool:
    """Check if a webhook event has already been processed."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status FROM webhook_events WHERE event_id = %s",
            (event_id,)
        )
        row = cursor.fetchone()
        return row is not None and row[0] == 'processed'


def log_webhook_event(event_id: str, event_type: str, payload: str, status: str = 'received') -> int:
    """Log a webhook event for idempotent processing."""
    received_at = get_ist_isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO webhook_events (event_id, event_type, payload, status, received_at)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (event_id, event_type, payload, status, received_at))
            return cursor.fetchone()[0]
        except:
            return 0


def mark_webhook_processed(event_id: str) -> bool:
    """Mark a webhook event as processed."""
    processed_at = get_ist_isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE webhook_events SET status = 'processed', processed_at = %s WHERE event_id = %s",
            (processed_at, event_id)
        )
        return cursor.rowcount > 0


# ============== Payment Audit Functions ==============

def create_payment_record(
    job_id: int, order_id: str, amount_cents: int, currency: str = 'INR',
    payment_gateway: str = 'cashfree', customer_email: Optional[str] = None,
    customer_phone: Optional[str] = None, ip_address: Optional[str] = None
) -> int:
    """Create a payment audit record with full tracking details."""
    created_at = get_ist_isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO payments (
                job_id, order_id, amount_cents, currency, payment_gateway,
                customer_email, customer_phone, ip_address, status, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'created', %s)
            RETURNING id
        """, (job_id, order_id, amount_cents, currency, payment_gateway,
              customer_email, customer_phone, ip_address, created_at))
        return cursor.fetchone()[0]


def update_payment_verified(
    order_id: str, payment_id: str, signature: str = "",
    payment_method: Optional[str] = None
) -> bool:
    """Mark payment as verified/captured with payment method details."""
    verified_at = get_ist_isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE payments 
            SET payment_id = %s, payment_signature = %s, payment_method = %s,
                status = 'captured', verified_at = %s
            WHERE order_id = %s
        """, (payment_id, signature, payment_method, verified_at, order_id))
        return cursor.rowcount > 0


def update_payment_failed(order_id: str, error_code: str = "", error_message: str = "") -> bool:
    """Mark payment as failed with error details."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE payments 
            SET status = 'failed', error_code = %s, error_message = %s
            WHERE order_id = %s
        """, (error_code, error_message, order_id))
        return cursor.rowcount > 0


def update_payment_refunded(
    payment_id: str, refund_id: str, refund_amount_cents: Optional[int] = None
) -> bool:
    """Mark payment as refunded with refund details."""
    refunded_at = get_ist_isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE payments 
            SET status = 'refunded', refunded_at = %s, refund_id = %s,
                refund_amount_cents = %s
            WHERE payment_id = %s
        """, (refunded_at, refund_id, refund_amount_cents, payment_id))
        return cursor.rowcount > 0


# ============== User Functions (Authentication) ==============

def create_user(
    email: str,
    password_hash: Optional[str] = None,
    name: Optional[str] = None,
    google_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Create a new user in the database."""
    created_at = get_ist_isoformat()
    
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO users (email, password_hash, name, google_id, created_at, last_login)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (email, password_hash, name, google_id, created_at, created_at))
            
            user_id = cursor.fetchone()[0]
            
            return {
                "id": user_id,
                "email": email,
                "name": name,
                "google_id": google_id,
                "created_at": created_at,
                "last_login": created_at
            }
        except Exception:
            return None


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    """Get a user by their ID."""
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Get a user by their email address."""
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_user_by_google_id(google_id: str) -> Optional[Dict[str, Any]]:
    """Get a user by their Google ID."""
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM users WHERE google_id = %s", (google_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def update_user_last_login(user_id: int) -> bool:
    """Update the last login timestamp for a user."""
    last_login = get_ist_isoformat()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET last_login = %s WHERE id = %s",
            (last_login, user_id)
        )
        return cursor.rowcount > 0


def update_user_password(user_id: int, password_hash: str) -> bool:
    """Update a user's password hash."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (password_hash, user_id)
        )
        return cursor.rowcount > 0


def update_user_google_id(user_id: int, google_id: str) -> bool:
    """Link a Google ID to an existing user account."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET google_id = %s WHERE id = %s",
            (google_id, user_id)
        )
        return cursor.rowcount > 0


def update_user_google_tokens(user_id: int, refresh_token: str, drive_folder_id: Optional[str] = None) -> bool:
    """Store Google refresh token and Drive folder ID for a user."""
    with get_connection() as conn:
        cursor = conn.cursor()
        if drive_folder_id:
            cursor.execute(
                "UPDATE users SET google_refresh_token = %s, google_drive_folder_id = %s WHERE id = %s",
                (refresh_token, drive_folder_id, user_id)
            )
        else:
            cursor.execute(
                "UPDATE users SET google_refresh_token = %s WHERE id = %s",
                (refresh_token, user_id)
            )
        return cursor.rowcount > 0


def update_user_drive_folder_id(user_id: int, folder_id: str) -> bool:
    """Cache the Google Drive folder ID for a user."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET google_drive_folder_id = %s WHERE id = %s",
            (folder_id, user_id)
        )
        return cursor.rowcount > 0


def update_job_drive_info(ticket: str, storage_type: str, drive_file_id: Optional[str] = None) -> bool:
    """Update a job's storage type and Drive file ID."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE jobs SET storage_type = %s, drive_file_id = %s WHERE ticket = %s",
            (storage_type, drive_file_id, ticket)
        )
        return cursor.rowcount > 0


def update_job_drive_processed(ticket: str, drive_processed_file_id: str) -> bool:
    """Update a job's processed Drive file ID."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE jobs SET drive_processed_file_id = %s, processing_status = 'processed' WHERE ticket = %s",
            (drive_processed_file_id, ticket)
        )
        return cursor.rowcount > 0


# ============== Bulk Payment Functions ==============

def create_bulk_payment_record(
    job_ids: List[int], order_id: str, total_cents: int,
    payment_gateway: str = 'cashfree', customer_email: Optional[str] = None,
    customer_phone: Optional[str] = None, ip_address: Optional[str] = None
) -> None:
    """Create payment records for multiple jobs in a bulk order."""
    created_at = get_ist_isoformat()
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Fetch each job's actual price for accurate per-file payment records
        placeholders = ','.join(['%s'] * len(job_ids))
        cursor.execute(
            f"SELECT id, client_estimate_cents FROM jobs WHERE id IN ({placeholders})",
            tuple(job_ids)
        )
        job_prices = {row['id']: row['client_estimate_cents'] or 0 for row in cursor.fetchall()}
        
        for job_id in job_ids:
            job_amount = job_prices.get(job_id, total_cents // len(job_ids))
            cursor.execute("""
                INSERT INTO payments (
                    job_id, order_id, amount_cents, currency, payment_gateway,
                    customer_email, customer_phone, ip_address, status, created_at
                )
                VALUES (%s, %s, %s, 'INR', %s, %s, %s, %s, 'created', %s)
            """, (job_id, order_id, job_amount, payment_gateway,
                  customer_email, customer_phone, ip_address, created_at))


def update_bulk_jobs_with_order(tickets: List[str], order_id: str, total_cents: int, token: str) -> bool:
    """Update multiple jobs with payment order details."""
    with get_connection() as conn:
        cursor = conn.cursor()
        for ticket in tickets:
            cursor.execute("""
                UPDATE jobs 
                SET order_id = %s, estimate_cents = %s, token = %s, status = 'pending_payment'
                WHERE ticket = %s
            """, (order_id, total_cents, token, ticket))
        return cursor.rowcount > 0


# ============== Admin Dashboard Functions ==============

def get_admin_stats() -> Dict[str, Any]:
    """Get aggregate statistics for the admin dashboard."""
    today = get_ist_now().strftime("%Y-%m-%d")
    seven_days_ago = (get_ist_now() - timedelta(days=7)).isoformat()
    
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Total users
        cursor.execute("SELECT COUNT(*) as count FROM users")
        total_users = cursor.fetchone()['count']
        
        # Total jobs (excluding drafts)
        cursor.execute("SELECT COUNT(*) as count FROM jobs WHERE status NOT IN ('draft', 'created')")
        total_jobs = cursor.fetchone()['count']
        
        # Today's jobs
        cursor.execute(
            "SELECT COUNT(*) as count FROM jobs WHERE created_at >= %s AND status NOT IN ('draft', 'created')",
            (today,)
        )
        today_jobs = cursor.fetchone()['count']
        
        # Total revenue (from captured payments)
        cursor.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) as total FROM payments WHERE status = 'captured'"
        )
        total_revenue_cents = cursor.fetchone()['total']
        
        # Today's revenue
        cursor.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) as total FROM payments WHERE status = 'captured' AND created_at >= %s",
            (today,)
        )
        today_revenue_cents = cursor.fetchone()['total']
        
        # Active users (logged in within 7 days)
        cursor.execute(
            "SELECT COUNT(*) as count FROM users WHERE last_login >= %s",
            (seven_days_ago,)
        )
        active_users_7d = cursor.fetchone()['count']
        
        # Failed payments count
        cursor.execute("SELECT COUNT(*) as count FROM payments WHERE status = 'failed'")
        failed_payments = cursor.fetchone()['count']
        
        # Pending jobs
        cursor.execute("SELECT COUNT(*) as count FROM jobs WHERE status IN ('pending', 'paid')")
        pending_jobs = cursor.fetchone()['count']
        
        return {
            "total_users": total_users,
            "total_jobs": total_jobs,
            "today_jobs": today_jobs,
            "total_revenue_cents": total_revenue_cents,
            "today_revenue_cents": today_revenue_cents,
            "active_users_7d": active_users_7d,
            "failed_payments": failed_payments,
            "pending_jobs": pending_jobs,
        }


def get_all_users_with_stats() -> List[Dict[str, Any]]:
    """Get all users with their upload counts and total spend."""
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT 
                u.id, u.email, u.name, u.google_id, u.created_at, u.last_login,
                COALESCE(j.job_count, 0) as job_count,
                COALESCE(p.total_spent_cents, 0) as total_spent_cents
            FROM users u
            LEFT JOIN (
                SELECT user_id, COUNT(*) as job_count 
                FROM jobs WHERE status NOT IN ('draft', 'created')
                GROUP BY user_id
            ) j ON u.id = j.user_id
            LEFT JOIN (
                SELECT j2.user_id, SUM(pay.amount_cents) as total_spent_cents
                FROM payments pay
                JOIN jobs j2 ON pay.job_id = j2.id
                WHERE pay.status = 'captured'
                GROUP BY j2.user_id
            ) p ON u.id = p.user_id
            ORDER BY u.created_at DESC
        """)
        return [dict(row) for row in cursor.fetchall()]


def get_all_payments_admin(limit: int = 200, offset: int = 0) -> Dict[str, Any]:
    """Get all payment records with job details for admin view."""
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Total count
        cursor.execute("SELECT COUNT(*) as count FROM payments")
        total = cursor.fetchone()['count']
        
        # Paginated results
        cursor.execute("""
            SELECT 
                p.*,
                j.ticket, j.sender, j.filename, j.customer_email as job_email,
                j.client_estimate_cents as job_amount_cents
            FROM payments p
            LEFT JOIN jobs j ON p.job_id = j.id
            ORDER BY p.created_at DESC
            LIMIT %s OFFSET %s
        """, (limit, offset))
        
        return {
            "payments": [dict(row) for row in cursor.fetchall()],
            "total": total,
        }


def get_job_status_breakdown() -> List[Dict[str, Any]]:
    """Get count of jobs per status for admin dashboard."""
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT status, COUNT(*) as count 
            FROM jobs 
            GROUP BY status 
            ORDER BY count DESC
        """)
        return [dict(row) for row in cursor.fetchall()]


def get_all_jobs_admin(limit: int = 200, offset: int = 0, status_filter: Optional[str] = None) -> Dict[str, Any]:
    """Get all jobs with pagination for admin view."""
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        if status_filter:
            cursor.execute("SELECT COUNT(*) as count FROM jobs WHERE status = %s", (status_filter,))
            total = cursor.fetchone()['count']
            cursor.execute("""
                SELECT j.*, u.email as user_email, u.name as user_name
                FROM jobs j
                LEFT JOIN users u ON j.user_id = u.id
                WHERE j.status = %s
                ORDER BY j.created_at DESC
                LIMIT %s OFFSET %s
            """, (status_filter, limit, offset))
        else:
            cursor.execute("SELECT COUNT(*) as count FROM jobs")
            total = cursor.fetchone()['count']
            cursor.execute("""
                SELECT j.*, u.email as user_email, u.name as user_name
                FROM jobs j
                LEFT JOIN users u ON j.user_id = u.id
                ORDER BY j.created_at DESC
                LIMIT %s OFFSET %s
            """, (limit, offset))
        
        return {
            "jobs": [dict(row) for row in cursor.fetchall()],
            "total": total,
        }


def get_admin_trends(days: int = 30) -> Dict[str, Any]:
    """Get daily aggregated trends for admin charts."""
    with get_connection() as conn:
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Revenue per day (last N days)
        cursor.execute("""
            SELECT DATE(created_at::timestamptz) as day, 
                   COALESCE(SUM(amount_cents), 0) as revenue_cents,
                   COUNT(*) as payment_count
            FROM payments 
            WHERE status = 'captured' AND created_at::timestamptz >= NOW() - INTERVAL '%s days'
            GROUP BY DATE(created_at::timestamptz) 
            ORDER BY day
        """, (days,))
        revenue_daily = [dict(row) for row in cursor.fetchall()]
        
        # Prints per day
        cursor.execute("""
            SELECT DATE(created_at::timestamptz) as day, COUNT(*) as count
            FROM jobs 
            WHERE status NOT IN ('draft', 'created') AND created_at::timestamptz >= NOW() - INTERVAL '%s days'
            GROUP BY DATE(created_at::timestamptz) 
            ORDER BY day
        """, (days,))
        prints_daily = [dict(row) for row in cursor.fetchall()]
        
        # User signups per day
        cursor.execute("""
            SELECT DATE(created_at::timestamptz) as day, COUNT(*) as count
            FROM users 
            WHERE created_at::timestamptz >= NOW() - INTERVAL '%s days'
            GROUP BY DATE(created_at::timestamptz) 
            ORDER BY day
        """, (days,))
        signups_daily = [dict(row) for row in cursor.fetchall()]
        
        return {
            "revenue_daily": revenue_daily,
            "prints_daily": prints_daily,
            "signups_daily": signups_daily,
        }


# ============== Shop Settings Functions ==============

def get_shop_status() -> Dict[str, Any]:
    """Get the current shop open/closed status.
    
    Returns:
        dict with 'status' ('open' or 'closed') and 'message' (custom closed message).
        Defaults to 'open' if no setting exists.
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute(
                "SELECT key, value FROM shop_settings WHERE key IN ('shop_status', 'shop_closed_message')"
            )
            rows = cursor.fetchall()
            settings_map = {row['key']: row['value'] for row in rows}
            return {
                "status": settings_map.get("shop_status", "open"),
                "message": settings_map.get("shop_closed_message", "")
            }
    except Exception as e:
        logger.error(f"Error fetching shop status: {e}")
        # Default to open if there's a DB error — don't block students unnecessarily
        return {"status": "open", "message": ""}


def set_shop_status(status: str, message: str = "") -> bool:
    """Set the shop open/closed status.
    
    Args:
        status: 'open' or 'closed'
        message: Optional custom message to show students when closed
    
    Returns:
        True if update was successful
    """
    now = get_ist_isoformat()
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            # Upsert shop_status
            cursor.execute("""
                INSERT INTO shop_settings (key, value, updated_at)
                VALUES ('shop_status', %s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
            """, (status, now))
            # Upsert shop_closed_message
            cursor.execute("""
                INSERT INTO shop_settings (key, value, updated_at)
                VALUES ('shop_closed_message', %s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
            """, (message, now))
        logger.info(f"Shop status set to '{status}' with message: '{message}'")
        return True
    except Exception as e:
        logger.error(f"Error setting shop status: {e}")
        return False
