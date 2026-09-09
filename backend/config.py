"""
Configuration loader for the Xerox Hotspot Upload system.
Loads environment variables with sensible defaults.
"""

import os
from pathlib import Path
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

# Load .env file if it exists
load_dotenv()


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # Server configuration
    app_host: str = "0.0.0.0"
    app_port: int = 5000
    
    # Storage paths
    storage_dir: Path = Path("./incoming_files")
    archive_dir: Path = Path("./archived_files")
    db_path: Path = Path("./jobs.db")
    log_dir: Path = Path("./logs")
    
    # Upload limits (5GB for video support)
    max_file_mb: int = 5120
    
    # Optional authentication
    upload_passphrase: str = ""
    staff_username: str = "anna"
    staff_password: str = ""  # MUST be set in .env — no default for security
    admin_username: str = "admin"
    admin_password: str = ""  # MUST be set in .env — no default for security
    cron_secret: str = ""  # Secret token to protect /api/cron/cleanup
    turnstile_site_key: str = ""  # Cloudflare Turnstile public site key
    turnstile_secret_key: str = ""  # Cloudflare Turnstile secret key
    staff_access_key: str = ""  # Secret key to access /staff and /admin pages (set in .env)
    
    # Disk management (10GB minimum for large files)
    disk_low_threshold_mb: int = 10240
    retention_days: int = 14  # Auto-delete archived jobs after 2 weeks
    
    # Rate limiting (500 uploads per IP per hour for high-traffic production use)
    rate_limit_per_ip_per_hour: int = 500
    rate_limit_concurrent: int = 10
    
    # Cashfree payment gateway (legacy — kept for reference, not used)
    cashfree_app_id: str = ""
    cashfree_secret_key: str = ""
    cashfree_webhook_secret: str = ""
    cashfree_environment: str = "TEST"  # TEST or PRODUCTION
    
    # Razorpay payment gateway (active)
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    
    # SMTP Email notifications (optional — blocked on some hosts like Render)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_tls: bool = True
    
    # Brevo HTTP email API (Alternative to Resend, free tier 300/day)
    brevo_api_key: str = ""
    
    # Resend HTTP email API (recommended for Render/cloud — get free key at resend.com)
    resend_api_key: str = ""
    sender_email: str = ""  # e.g. "your_verified_sender@domain.com"
    
    # JWT Authentication
    jwt_secret_key: str = ""  # MUST be set in .env — no default for security
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 43200  # 30 days — persistent login until manual logout
    
    # Google OAuth
    google_client_id: str = ""
    google_client_secret: str = ""  # Required for authorization code flow (Drive access)
    google_drive_folder_name: str = "XeroxUploads"  # Folder name in user's Google Drive
    
    # Supabase Configuration (REQUIRED for PostgreSQL + Storage)
    database_url: str = ""  # PostgreSQL connection string from Supabase (required)
    supabase_url: str = ""  # Project URL e.g. https://xxx.supabase.co (required)
    supabase_anon_key: str = ""  # Public anon key (required)
    supabase_storage_bucket: str = "uploads"  # Storage bucket name
    
    # Error monitoring (optional — get DSN from sentry.io)
    sentry_dsn: str = ""
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"
    
    @property
    def max_file_bytes(self) -> int:
        """Return max file size in bytes."""
        return self.max_file_mb * 1024 * 1024
    
    @property
    def disk_low_threshold_bytes(self) -> int:
        """Return disk threshold in bytes."""
        return self.disk_low_threshold_mb * 1024 * 1024
    
    def ensure_directories(self) -> None:
        """Create necessary directories if they don't exist."""
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


# Global settings instance
settings = Settings()
