"""
Supabase Storage Helper

Handles file uploads and downloads to/from Supabase Storage.
"""

import os
import logging
from typing import Optional, BinaryIO
from pathlib import Path

logger = logging.getLogger(__name__)

# Supabase client (lazy initialization)
_supabase_client = None

def get_supabase_client():
    """Get or create Supabase client."""
    global _supabase_client
    
    if _supabase_client is None:
        from supabase import create_client, Client
        
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_ANON_KEY")
        
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_ANON_KEY must be set")
        
        _supabase_client = create_client(url, key)
        logger.info("Supabase client initialized")
    
    return _supabase_client


# Storage bucket name
BUCKET_NAME = "uploads"


def upload_file(file_data: bytes, filename: str, content_type: str = "application/octet-stream") -> Optional[str]:
    """
    Upload a file to Supabase Storage.
    
    Args:
        file_data: File content as bytes
        filename: Name to store the file as (should be unique, e.g., ticket-based)
        content_type: MIME type of the file
    
    Returns:
        Public URL of the uploaded file, or None if upload failed
    """
    try:
        client = get_supabase_client()
        
        # Upload to storage
        result = client.storage.from_(BUCKET_NAME).upload(
            path=filename,
            file=file_data,
            file_options={"content-type": content_type}
        )
        
        # Get public URL
        public_url = client.storage.from_(BUCKET_NAME).get_public_url(filename)
        
        logger.info(f"File uploaded to Supabase: {filename}")
        return public_url
        
    except Exception as e:
        logger.error(f"Failed to upload file to Supabase: {e}")
        return None


def upload_file_stream(file_path: str, storage_name: str, content_type: str = "application/octet-stream") -> Optional[str]:
    """
    Upload a file from disk to Supabase Storage.
    
    Args:
        file_path: Local path to the file
        storage_name: Name to store the file as in Supabase
        content_type: MIME type of the file
    
    Returns:
        Public URL of the uploaded file, or None if upload failed
    """
    try:
        with open(file_path, 'rb') as f:
            file_data = f.read()
        
        return upload_file(file_data, storage_name, content_type)
        
    except Exception as e:
        logger.error(f"Failed to read/upload file: {e}")
        return None


def download_file(filename: str) -> Optional[bytes]:
    """
    Download a file from Supabase Storage.
    
    Args:
        filename: Name of the file in storage
    
    Returns:
        File content as bytes, or None if download failed
    """
    try:
        client = get_supabase_client()
        
        result = client.storage.from_(BUCKET_NAME).download(filename)
        
        logger.info(f"File downloaded from Supabase: {filename}")
        return result
        
    except Exception as e:
        logger.error(f"Failed to download file from Supabase: {e}")
        return None


def delete_file(filename: str) -> bool:
    """
    Delete a file from Supabase Storage.
    
    Args:
        filename: Name of the file in storage
    
    Returns:
        True if deleted successfully, False otherwise
    """
    try:
        client = get_supabase_client()
        
        client.storage.from_(BUCKET_NAME).remove([filename])
        
        logger.info(f"File deleted from Supabase: {filename}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to delete file from Supabase: {e}")
        return False


def list_and_delete_old_files(today_prefix: str) -> dict:
    """
    List all files in storage and delete those with date prefixes older than today.
    Files are named like YYYYMMDD-001.pdf, so we compare the date prefix.
    
    Args:
        today_prefix: Today's date prefix (e.g., '20260210')
    
    Returns:
        Dict with counts of files found and deleted
    """
    result = {"total_files": 0, "old_files_deleted": 0, "errors": []}
    
    try:
        client = get_supabase_client()
        
        # List all files in the bucket
        files = client.storage.from_(BUCKET_NAME).list()
        result["total_files"] = len(files)
        
        for file_info in files:
            filename = file_info.get("name", "")
            
            # Skip if no valid date prefix (YYYYMMDD format)
            if len(filename) < 8 or not filename[:8].isdigit():
                continue
            
            file_date = filename[:8]
            
            # Delete files older than today
            if file_date < today_prefix:
                try:
                    client.storage.from_(BUCKET_NAME).remove([filename])
                    result["old_files_deleted"] += 1
                    logger.info(f"Deleted old file from Supabase: {filename}")
                except Exception as e:
                    result["errors"].append(f"Failed to delete {filename}: {str(e)}")
    
    except Exception as e:
        result["errors"].append(f"Failed to list files: {str(e)}")
    
    return result


def delete_orphaned_files(db_module) -> dict:
    """
    Delete files from Supabase Storage that are NOT referenced by any current DB job.
    This catches orphaned files regardless of their naming format.
    
    Args:
        db_module: The db module (to query current jobs)
    
    Returns:
        Dict with counts of files found, orphans deleted, and errors
    """
    from urllib.parse import urlparse
    
    result = {"total_files": 0, "orphans_deleted": 0, "orphan_filenames": [], "errors": []}
    
    try:
        client = get_supabase_client()
        
        # List all files in the bucket
        files = client.storage.from_(BUCKET_NAME).list()
        result["total_files"] = len(files)
        
        if not files:
            return result
        
        # Collect all filenames currently referenced by jobs in the DB
        referenced_filenames = set()
        try:
            with db_module.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT filepath, processed_filepath FROM jobs")
                for row in cursor.fetchall():
                    for url in row:
                        if url:
                            if url.startswith("http"):
                                parsed = urlparse(url)
                                fname = parsed.path.split("/")[-1]
                            else:
                                fname = url.split("/")[-1]
                            if fname:
                                referenced_filenames.add(fname)
        except Exception as e:
            result["errors"].append(f"Failed to query DB for referenced files: {str(e)}")
            return result  # Don't delete anything if we can't check the DB
        
        # Collect orphan filenames
        orphan_filenames = []
        for file_info in files:
            filename = file_info.get("name", "")
            if not filename or filename.startswith("."):
                continue
            if filename not in referenced_filenames:
                orphan_filenames.append(filename)
        
        result["orphan_filenames"] = orphan_filenames
        
        if not orphan_filenames:
            return result
        
        # Delete in batches of 20
        batch_size = 20
        for i in range(0, len(orphan_filenames), batch_size):
            batch = orphan_filenames[i:i + batch_size]
            try:
                response = client.storage.from_(BUCKET_NAME).remove(batch)
                logger.info(f"Supabase remove batch response: {response}")
                result["orphans_deleted"] += len(batch)
            except Exception as e:
                result["errors"].append(f"Batch delete failed for {batch}: {str(e)}")
        
        # Verify by re-listing
        try:
            remaining = client.storage.from_(BUCKET_NAME).list()
            remaining_names = [f.get("name", "") for f in remaining]
            still_exists = [f for f in orphan_filenames if f in remaining_names]
            if still_exists:
                result["errors"].append(f"Files still exist after delete: {still_exists[:5]}")
                result["orphans_deleted"] -= len(still_exists)
                logger.warning(f"Supabase remove() did not delete {len(still_exists)} files: {still_exists[:5]}")
        except Exception as e:
            result["errors"].append(f"Verification failed: {str(e)}")
    
    except Exception as e:
        result["errors"].append(f"Failed to list files: {str(e)}")
    
    return result


def get_public_url(filename: str) -> str:
    """
    Get the public URL for a file in Supabase Storage.
    
    Args:
        filename: Name of the file in storage
    
    Returns:
        Public URL string
    """
    client = get_supabase_client()
    return client.storage.from_(BUCKET_NAME).get_public_url(filename)


def file_exists(filename: str) -> bool:
    """
    Check if a file exists in Supabase Storage.
    
    Args:
        filename: Name of the file to check
    
    Returns:
        True if file exists, False otherwise
    """
    try:
        client = get_supabase_client()
        
        # List files with the exact name
        result = client.storage.from_(BUCKET_NAME).list(path="", options={"search": filename})
        
        for item in result:
            if item.get("name") == filename:
                return True
        
        return False
        
    except Exception as e:
        logger.error(f"Failed to check file existence: {e}")
        return False


def create_signed_upload_url(filename: str) -> Optional[dict]:
    """
    Create a signed URL for direct upload to Supabase Storage.
    This allows frontend to upload directly to Supabase, bypassing the server.
    
    Args:
        filename: Name to store the file as
    
    Returns:
        Dict with 'signed_url', 'token', and 'path' for the signed upload, or None if failed
    """
    try:
        client = get_supabase_client()
        
        result = client.storage.from_(BUCKET_NAME).create_signed_upload_url(filename)
        
        if result:
            logger.info(f"Created signed upload URL for: {filename}, result keys: {result.keys() if isinstance(result, dict) else 'not a dict'}")
            
            # Normalize response format
            if isinstance(result, dict):
                return {
                    "signed_url": result.get("signedUrl") or result.get("signed_url") or result.get("signedURL"),
                    "token": result.get("token"),
                    "path": result.get("path") or filename
                }
            
            return result
        
        return None
        
    except Exception as e:
        logger.error(f"Failed to create signed upload URL: {e}")
        return None


def upload_to_signed_url(signed_url: str, token: str, file_data: bytes, content_type: str = "application/octet-stream") -> bool:
    """
    Upload a file using a signed URL (alternative to direct upload).
    
    Args:
        signed_url: The signed URL from create_signed_upload_url
        token: The token from create_signed_upload_url
        file_data: File content as bytes
        content_type: MIME type of the file
    
    Returns:
        True if upload succeeded, False otherwise
    """
    try:
        client = get_supabase_client()
        
        result = client.storage.from_(BUCKET_NAME).upload_to_signed_url(
            signed_url,
            file_data,
            file_options={"content-type": content_type}
        )
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to upload to signed URL: {e}")
        return False
