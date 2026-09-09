"""
Google Drive API Helper Module

Handles file uploads, downloads, and management using Google Drive API v3.
Uses OAuth2 refresh tokens stored per-user to access their Drive.
"""

import io
import time
import logging
from typing import Optional, Tuple, Generator

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

from .config import settings

logger = logging.getLogger(__name__)

# In-memory access token cache: { hash(refresh_token): (credentials, expiry_timestamp) }
# Avoids ~300ms OAuth refresh on repeated downloads. Tokens last 60 min; we cache for 50.
_token_cache: dict = {}
_TOKEN_CACHE_TTL = 3000  # 50 minutes

# Google OAuth2 scopes
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/drive.file",  # Access only files created by our app
]

# Token endpoint for exchanging authorization codes
TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"


def build_auth_url(redirect_uri: str, state: str = "", force_consent: bool = False) -> str:
    """
    Build the Google OAuth2 authorization URL.
    
    Args:
        redirect_uri: The callback URL
        state: Optional state parameter for CSRF protection
        force_consent: If True, forces full consent screen (needed for first login to get refresh token).
                       If False, just shows account picker for returning users.
    
    Returns:
        Full authorization URL to redirect the user to
    """
    from urllib.parse import urlencode
    
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",   # Request refresh token
        "include_granted_scopes": "true",
    }
    
    if force_consent:
        params["prompt"] = "consent"  # Full consent — gets refresh token
    else:
        params["prompt"] = "select_account"  # Just account picker — no permissions re-approval
    
    if state:
        params["state"] = state
    
    return f"{AUTH_URI}?{urlencode(params)}"


def exchange_auth_code(code: str, redirect_uri: str) -> Optional[dict]:
    """
    Exchange an authorization code for access and refresh tokens.
    Also fetches user info (email, name, Google ID).
    
    Args:
        code: Authorization code from callback
        redirect_uri: Must match the redirect_uri used in the auth URL
    
    Returns:
        Dict with keys: access_token, refresh_token, email, name, sub (Google ID), picture
        Or None if exchange fails
    """
    import requests
    
    try:
        # Exchange code for tokens
        token_response = requests.post(TOKEN_URI, data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }, timeout=15)
        
        if token_response.status_code != 200:
            logger.error(f"Token exchange failed: {token_response.status_code} {token_response.text}")
            return None
        
        token_data = token_response.json()
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        
        if not access_token:
            logger.error("No access token in response")
            return None
        
        # Fetch user info using the access token
        userinfo_response = requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10
        )
        
        if userinfo_response.status_code != 200:
            logger.error(f"User info fetch failed: {userinfo_response.status_code}")
            return None
        
        userinfo = userinfo_response.json()
        
        # Granted scopes — Google's granular consent may exclude some requested scopes
        granted_scope = token_data.get("scope", "")
        
        result = {
            "access_token": access_token,
            "refresh_token": refresh_token,  # May be None if user previously authorized
            "email": userinfo.get("email"),
            "name": userinfo.get("name"),
            "sub": userinfo.get("sub"),  # Google user ID
            "picture": userinfo.get("picture"),
            "scope": granted_scope,  # Space-separated scopes actually granted by the user
        }
        
        logger.info(f"Google auth code exchanged for user: {result['email']}")
        return result
        
    except Exception as e:
        logger.error(f"Google auth code exchange error: {e}")
        return None


def _get_credentials(refresh_token: str) -> Optional[Credentials]:
    """
    Build Google credentials from a stored refresh token.
    Uses in-memory cache to avoid repeated OAuth refreshes (~300ms each).
    
    Args:
        refresh_token: User's stored refresh token
    
    Returns:
        Valid Credentials object, or None if refresh fails
    """
    # Check cache first
    cache_key = hash(refresh_token)
    cached = _token_cache.get(cache_key)
    if cached:
        creds, expiry = cached
        if time.time() < expiry and creds.valid:
            return creds
    
    try:
        creds = Credentials(
            token=None,  # Will be refreshed
            refresh_token=refresh_token,
            token_uri=TOKEN_URI,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            scopes=SCOPES,
        )
        
        # Refresh to get a valid access token
        creds.refresh(GoogleAuthRequest())
        
        if not creds.valid:
            logger.error("Credentials are not valid after refresh")
            return None
        
        # Cache the refreshed credentials
        _token_cache[cache_key] = (creds, time.time() + _TOKEN_CACHE_TTL)
        logger.info("Google credentials refreshed and cached")
        
        return creds
        
    except Exception as e:
        logger.error(f"Failed to get credentials from refresh token: {e}")
        # Remove stale cache entry
        _token_cache.pop(cache_key, None)
        return None


def _get_drive_service(refresh_token: str):
    """Build a Google Drive API service object."""
    creds = _get_credentials(refresh_token)
    if not creds:
        return None
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_or_create_app_folder(refresh_token: str, folder_id_cache: Optional[str] = None) -> Optional[str]:
    """
    Get or create the app's folder in the user's Google Drive.
    Returns the folder ID.
    
    Args:
        refresh_token: User's refresh token
        folder_id_cache: Previously cached folder ID (checked first for speed)
    
    Returns:
        Google Drive folder ID, or None if failed
    """
    service = _get_drive_service(refresh_token)
    if not service:
        return None
    
    folder_name = settings.google_drive_folder_name
    
    try:
        # If we have a cached folder ID, verify it still exists
        if folder_id_cache:
            try:
                folder = service.files().get(
                    fileId=folder_id_cache,
                    fields="id, trashed"
                ).execute()
                if not folder.get("trashed"):
                    return folder_id_cache
            except Exception:
                pass  # Folder deleted or inaccessible, create new one
        
        # Search for existing folder
        query = (
            f"name = '{folder_name}' and "
            f"mimeType = 'application/vnd.google-apps.folder' and "
            f"trashed = false"
        )
        results = service.files().list(
            q=query,
            spaces="drive",
            fields="files(id, name)",
            pageSize=1
        ).execute()
        
        files = results.get("files", [])
        if files:
            folder_id = files[0]["id"]
            logger.info(f"Found existing Drive folder: {folder_name} ({folder_id})")
            return folder_id
        
        # Create new folder
        file_metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        folder = service.files().create(
            body=file_metadata,
            fields="id"
        ).execute()
        
        folder_id = folder.get("id")
        logger.info(f"Created Drive folder: {folder_name} ({folder_id})")
        return folder_id
        
    except Exception as e:
        logger.error(f"Failed to get/create Drive folder: {e}")
        return None


def upload_file(
    refresh_token: str,
    file_data: bytes,
    filename: str,
    content_type: str = "application/octet-stream",
    folder_id: Optional[str] = None
) -> Optional[str]:
    """
    Upload a file to the user's Google Drive from bytes (legacy, for small files).
    
    Args:
        refresh_token: User's refresh token
        file_data: File content as bytes
        filename: Display name for the file
        content_type: MIME type
        folder_id: Drive folder ID to upload into (auto-detected if None)
    
    Returns:
        Google Drive file ID, or None if upload failed
    """
    service = _get_drive_service(refresh_token)
    if not service:
        return None
    
    try:
        # Get or create folder if not provided
        if not folder_id:
            folder_id = get_or_create_app_folder(refresh_token)
        
        file_metadata = {"name": filename}
        if folder_id:
            file_metadata["parents"] = [folder_id]
        
        media = MediaIoBaseUpload(
            io.BytesIO(file_data),
            mimetype=content_type,
            resumable=True
        )
        
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id"
        ).execute()
        
        file_id = file.get("id")
        logger.info(f"File uploaded to Drive: {filename} ({file_id})")
        return file_id
        
    except Exception as e:
        logger.error(f"Failed to upload file to Drive: {e}")
        return None


def upload_file_from_path(
    refresh_token: str,
    file_path: str,
    filename: str,
    content_type: str = "application/octet-stream",
    folder_id: Optional[str] = None
) -> Optional[str]:
    """
    Upload a file to Google Drive by streaming from a file path on disk.
    Memory-efficient: does NOT load the entire file into RAM.
    
    Args:
        refresh_token: User's refresh token
        file_path: Path to the file on disk
        filename: Display name for the file in Drive
        content_type: MIME type
        folder_id: Drive folder ID to upload into
    
    Returns:
        Google Drive file ID, or None if upload failed
    """
    from googleapiclient.http import MediaFileUpload
    
    service = _get_drive_service(refresh_token)
    if not service:
        return None
    
    try:
        if not folder_id:
            folder_id = get_or_create_app_folder(refresh_token)
        
        file_metadata = {"name": filename}
        if folder_id:
            file_metadata["parents"] = [folder_id]
        
        # Stream from disk — uses chunked upload, not loaded into memory
        media = MediaFileUpload(
            file_path,
            mimetype=content_type,
            resumable=True,
            chunksize=5 * 1024 * 1024  # 5MB chunks
        )
        
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id"
        ).execute()
        
        file_id = file.get("id")
        logger.info(f"File uploaded to Drive from path: {filename} ({file_id})")
        return file_id
        
    except Exception as e:
        logger.error(f"Failed to upload file to Drive from path: {e}")
        return None


def download_file(refresh_token: str, file_id: str) -> Optional[bytes]:
    """
    Download a file from Google Drive.
    
    Args:
        refresh_token: File owner's refresh token
        file_id: Google Drive file ID
    
    Returns:
        File content as bytes, or None if download failed
    """
    service = _get_drive_service(refresh_token)
    if not service:
        return None
    
    try:
        request = service.files().get_media(fileId=file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        
        done = False
        while not done:
            _, done = downloader.next_chunk()
        
        buffer.seek(0)
        file_bytes = buffer.read()
        logger.info(f"File downloaded from Drive: {file_id} ({len(file_bytes)} bytes)")
        return file_bytes
        
    except Exception as e:
        logger.error(f"Failed to download file from Drive: {e}")
        return None


def stream_file(refresh_token: str, file_id: str, chunk_size: int = 2 * 1024 * 1024) -> Optional[Generator[bytes, None, None]]:
    """
    Stream a file from Google Drive in chunks — never loads entire file in RAM.
    Used as fallback when direct CDN redirect fails.
    
    Args:
        refresh_token: File owner's refresh token
        file_id: Google Drive file ID
        chunk_size: Size of each chunk in bytes (default 2MB)
    
    Returns:
        Generator yielding file chunks, or None if setup fails
    """
    service = _get_drive_service(refresh_token)
    if not service:
        return None
    
    def _chunk_generator():
        try:
            request = service.files().get_media(fileId=file_id)
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request, chunksize=chunk_size)
            
            done = False
            while not done:
                _, done = downloader.next_chunk()
                buffer.seek(0)
                yield buffer.read()
                buffer.seek(0)
                buffer.truncate(0)
        except Exception as e:
            logger.error(f"Error streaming file from Drive: {e}")
            raise
    
    return _chunk_generator()


def delete_file(refresh_token: str, file_id: str) -> bool:
    """
    Delete a file from Google Drive.
    
    Args:
        refresh_token: File owner's refresh token
        file_id: Google Drive file ID
    
    Returns:
        True if deleted, False otherwise
    """
    service = _get_drive_service(refresh_token)
    if not service:
        return False
    
    try:
        service.files().delete(fileId=file_id).execute()
        logger.info(f"File deleted from Drive: {file_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to delete file from Drive: {e}")
        return False


def get_file_metadata(refresh_token: str, file_id: str) -> Optional[dict]:
    """
    Get metadata for a file in Google Drive.
    
    Args:
        refresh_token: File owner's refresh token
        file_id: Google Drive file ID
    
    Returns:
        Dict with name, mimeType, size, etc. or None
    """
    service = _get_drive_service(refresh_token)
    if not service:
        return None
    
    try:
        file = service.files().get(
            fileId=file_id,
            fields="id, name, mimeType, size, createdTime"
        ).execute()
        return file
    except Exception as e:
        logger.error(f"Failed to get file metadata from Drive: {e}")
        return None


def get_direct_download_url(refresh_token: str, file_id: str) -> Optional[str]:
    """
    Generate a direct Google Drive download URL using a fresh access token.
    This lets the staff browser download directly from Google's CDN instead of
    proxying through our backend.
    
    The URL uses Google's alt=media endpoint with a Bearer token, but since
    browsers can't send Authorization headers via redirects, we use Google's
    built-in access_token query parameter instead.
    
    The URL is valid for ~1 hour (until the access token expires).
    
    Args:
        refresh_token: File owner's refresh token
        file_id: Google Drive file ID
    
    Returns:
        Direct download URL string, or None if failed
    """
    credentials = _get_credentials(refresh_token)
    if not credentials:
        return None
    
    try:
        # Ensure credentials are fresh  
        if credentials.expired or not credentials.token:
            from google.auth.transport.requests import Request as GoogleRequest
            credentials.refresh(GoogleRequest())
        
        access_token = credentials.token
        if not access_token:
            logger.error("No access token available for direct download URL")
            return None
        
        # Google Drive direct download URL with access token as query param
        # This bypasses the backend entirely — browser downloads from Google CDN
        direct_url = (
            f"https://www.googleapis.com/drive/v3/files/{file_id}"
            f"?alt=media&access_token={access_token}"
        )
        
        logger.info(f"Generated direct download URL for file: {file_id}")
        return direct_url
        
    except Exception as e:
        logger.error(f"Failed to generate direct download URL: {e}")
        return None
