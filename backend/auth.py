"""
Authentication module for Xerox Hotspot Upload System.

Provides:
- Password hashing with bcrypt
- JWT token generation and verification
- OAuth2 password flow for email/password login
- Google OAuth token verification
- Dependency injection for protected routes
"""

from datetime import datetime, timedelta
from typing import Optional
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
import bcrypt
from pydantic import BaseModel, EmailStr
import requests

from .config import settings
from . import db


# OAuth2 scheme for extracting token from Authorization header
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


# ============== Pydantic Models ==============

class UserCreate(BaseModel):
    """Schema for user registration."""
    email: EmailStr
    password: str
    name: Optional[str] = None


class UserLogin(BaseModel):
    """Schema for user login."""
    email: EmailStr
    password: str
    turnstile_token: Optional[str] = None


class GoogleAuthRequest(BaseModel):
    """Schema for Google OAuth login (legacy, kept for backwards compat)."""
    token: str  # Google ID token from frontend


class GoogleAuthCodeRequest(BaseModel):
    """Schema for Google OAuth authorization code flow."""
    code: str
    redirect_uri: str


class Token(BaseModel):
    """JWT token response."""
    access_token: str
    token_type: str = "bearer"
    user: dict


class UserResponse(BaseModel):
    """User data response."""
    id: int
    email: str
    name: Optional[str]
    created_at: str


# ============== Password Utilities ==============

def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    # Encode password and generate salt
    password_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash."""
    try:
        password_bytes = plain_password.encode('utf-8')
        hashed_bytes = hashed_password.encode('utf-8')
        return bcrypt.checkpw(password_bytes, hashed_bytes)
    except Exception:
        return False


# ============== JWT Token Utilities ==============

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token."""
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.access_token_expire_minutes))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> Optional[dict]:
    """Decode and verify a JWT token."""
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        return payload
    except JWTError:
        return None


# ============== Google OAuth Utilities ==============

def verify_google_token(token: str) -> Optional[dict]:
    """
    Verify a Google ID token and extract user info (legacy method).
    Returns dict with 'sub' (Google user ID), 'email', and 'name' if valid.
    """
    if not settings.google_client_id:
        return None
    
    try:
        # Verify token with Google's tokeninfo endpoint
        response = requests.get(
            f"https://oauth2.googleapis.com/tokeninfo?id_token={token}",
            timeout=10
        )
        
        if response.status_code != 200:
            return None
        
        payload = response.json()
        
        # Verify the audience matches our client ID
        if payload.get("aud") != settings.google_client_id:
            return None
        
        return {
            "sub": payload.get("sub"),  # Google user ID
            "email": payload.get("email"),
            "name": payload.get("name"),
            "picture": payload.get("picture")
        }
    except Exception:
        return None


def build_google_auth_url(redirect_uri: str, state: str = "", force_consent: bool = False) -> str:
    """
    Build the Google OAuth2 authorization URL.
    Requests drive.file scope for Google Drive access.
    """
    from . import google_drive
    return google_drive.build_auth_url(redirect_uri, state, force_consent=force_consent)


def handle_google_callback(code: str, redirect_uri: str) -> Optional[dict]:
    """
    Handle Google OAuth callback: exchange auth code for tokens and user info.
    
    Returns:
        Dict with: access_token, refresh_token, email, name, sub, picture
        Or None if exchange failed
    """
    from . import google_drive
    return google_drive.exchange_auth_code(code, redirect_uri)


# ============== User Database Operations ==============

def create_user(email: str, password: Optional[str] = None, name: Optional[str] = None, google_id: Optional[str] = None) -> Optional[dict]:
    """Create a new user in the database."""
    password_hash = hash_password(password) if password else None
    return db.create_user(
        email=email,
        password_hash=password_hash,
        name=name,
        google_id=google_id
    )


def authenticate_user(email: str, password: str) -> Optional[dict]:
    """Authenticate a user with email and password."""
    user = db.get_user_by_email(email)
    if not user:
        return None
    if not user.get("password_hash"):
        return None  # User signed up with OAuth only
    if not verify_password(password, user["password_hash"]):
        return None
    return user


def get_or_create_google_user(google_id: str, email: str, name: Optional[str] = None, refresh_token: Optional[str] = None) -> Optional[dict]:
    """Get existing user by Google ID or create a new one. Optionally stores refresh token."""
    # Try to find by Google ID
    user = db.get_user_by_google_id(google_id)
    if user:
        db.update_user_last_login(user["id"])
        # Update refresh token if a new one was provided
        if refresh_token:
            db.update_user_google_tokens(user["id"], refresh_token)
        return db.get_user_by_id(user["id"])
    
    # Try to find by email (link accounts)
    user = db.get_user_by_email(email)
    if user:
        # Link Google ID to existing account
        db.update_user_google_id(user["id"], google_id)
        db.update_user_last_login(user["id"])
        if refresh_token:
            db.update_user_google_tokens(user["id"], refresh_token)
        return db.get_user_by_id(user["id"])
    
    # Create new user
    new_user = create_user(email=email, name=name, google_id=google_id)
    if new_user and refresh_token:
        db.update_user_google_tokens(new_user["id"], refresh_token)
        return db.get_user_by_id(new_user["id"])
    return new_user


# ============== Authentication Dependencies ==============

async def get_current_user_optional(request: Request, token: Optional[str] = Depends(oauth2_scheme)) -> Optional[dict]:
    """
    Get the current user from JWT token (optional).
    Checks Authorization header first, then access_token cookie.
    Returns None if no token or invalid token.
    """
    # Fallback to cookie if header token is missing
    if not token:
        token = request.cookies.get("access_token")
    
    if not token:
        return None
    
    payload = decode_access_token(token)
    
    # If header token failed, try cookie as fallback
    if not payload and request.cookies.get("access_token") and token != request.cookies.get("access_token"):
        token = request.cookies.get("access_token")
        payload = decode_access_token(token)

    if not payload:
        return None
    
    user_id = payload.get("sub")
    if not user_id:
        return None
    
    user = db.get_user_by_id(int(user_id))
    return user


async def get_current_user(request: Request, token: Optional[str] = Depends(oauth2_scheme)) -> dict:
    """
    Get the current user from JWT token (required).
    Raises 401 if not authenticated.
    """
    user = await get_current_user_optional(request, token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
