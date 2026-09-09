"""
Office Document Page Counter — Google Drive Convert + pikepdf

Uploads office documents as Google Docs (which converts them),
then exports as PDF and counts pages with pikepdf.

No system dependencies needed — Google does all the rendering.
"""

import logging
import tempfile
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Google Docs MIME types for conversion
GOOGLE_DOC_MIME = 'application/vnd.google-apps.document'
GOOGLE_SHEET_MIME = 'application/vnd.google-apps.spreadsheet'
GOOGLE_SLIDES_MIME = 'application/vnd.google-apps.presentation'

# Map office extensions to their MIME type + Google equivalent
EXTENSION_MAP = {
    '.docx': ('application/vnd.openxmlformats-officedocument.wordprocessingml.document', GOOGLE_DOC_MIME),
    '.doc': ('application/msword', GOOGLE_DOC_MIME),
    '.odt': ('application/vnd.oasis.opendocument.text', GOOGLE_DOC_MIME),
    '.rtf': ('application/rtf', GOOGLE_DOC_MIME),
    '.txt': ('text/plain', GOOGLE_DOC_MIME),
    '.xlsx': None,  # Blocked
    '.xls': None,   # Blocked
    '.ods': None,   # Blocked
    '.pptx': ('application/vnd.openxmlformats-officedocument.presentationml.presentation', GOOGLE_SLIDES_MIME),
    '.ppt': ('application/vnd.ms-powerpoint', GOOGLE_SLIDES_MIME),
    '.odp': ('application/vnd.oasis.opendocument.presentation', GOOGLE_SLIDES_MIME),
}


def count_pages_from_drive(drive_service, drive_file_id: str, filename: str) -> Optional[int]:
    """
    Count exact pages by converting a Drive file to Google Docs format,
    then exporting as PDF.
    
    Steps:
    1. Download the binary file from Drive
    2. Re-upload as a Google Doc (converts it)
    3. Export the Google Doc as PDF
    4. Count pages with pikepdf
    5. Delete the temp Google Doc
    """
    try:
        import pikepdf
        from googleapiclient.http import MediaInMemoryUpload
        import io
        from googleapiclient.http import MediaIoBaseDownload
    except ImportError:
        logger.error("pikepdf not installed")
        return None

    ext = os.path.splitext(filename)[1].lower()
    ext_info = EXTENSION_MAP.get(ext)
    if not ext_info:
        logger.warning(f"Unsupported extension for page counting: {ext}")
        return None

    source_mime, google_mime = ext_info
    temp_doc_id = None

    try:
        # Step 1: Download the binary file from Drive
        logger.info(f"Downloading Drive file {drive_file_id} for conversion")
        request = drive_service.files().get_media(fileId=drive_file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        file_bytes = buffer.getvalue()

        if not file_bytes:
            logger.warning("Empty file downloaded from Drive")
            return None

        # Step 2: Upload as a Google Doc (Google converts it automatically)
        logger.info(f"Converting to Google Docs format for page counting")
        media = MediaInMemoryUpload(file_bytes, mimetype=source_mime)
        temp_doc = drive_service.files().create(
            body={
                'name': f'_temp_pagecount',
                'mimeType': google_mime  # This tells Google to CONVERT it
            },
            media_body=media,
            fields='id'
        ).execute()
        temp_doc_id = temp_doc['id']

        # Step 3: Export the Google Doc as PDF
        logger.info(f"Exporting converted doc {temp_doc_id} as PDF")
        pdf_content = drive_service.files().export(
            fileId=temp_doc_id,
            mimeType='application/pdf'
        ).execute()

        if not pdf_content:
            logger.warning("Empty PDF export")
            return None

        # Step 4: Count pages with pikepdf
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                tmp.write(pdf_content)
                tmp_path = tmp.name

            with pikepdf.open(tmp_path) as pdf:
                page_count = len(pdf.pages)

            logger.info(f"Exact page count: {page_count} pages")
            return page_count
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    except Exception as e:
        logger.error(f"Page count error: {e}")
        return None
    finally:
        # Step 5: Always delete the temp Google Doc
        if temp_doc_id:
            try:
                drive_service.files().delete(fileId=temp_doc_id).execute()
                logger.info(f"Cleaned up temp Google Doc: {temp_doc_id}")
            except Exception:
                logger.warning(f"Failed to clean up temp doc: {temp_doc_id}")


def count_pages_from_bytes(file_bytes: bytes, filename: str) -> Optional[int]:
    """
    Count pages for Supabase uploads.
    Uses any available Google refresh token to create a Drive service.
    """
    try:
        from backend.google_drive import _get_drive_service
        from googleapiclient.http import MediaInMemoryUpload
        import pikepdf

        ext = os.path.splitext(filename)[1].lower()
        ext_info = EXTENSION_MAP.get(ext)
        if not ext_info:
            return None

        source_mime, google_mime = ext_info

        # Get a refresh token for Drive access
        admin_token = _get_any_refresh_token()
        if not admin_token:
            logger.warning("No refresh token available for page counting")
            return None

        drive_service = _get_drive_service(admin_token)
        if not drive_service:
            return None

        temp_doc_id = None
        try:
            # Upload as Google Doc (converts automatically)
            media = MediaInMemoryUpload(file_bytes, mimetype=source_mime)
            temp_doc = drive_service.files().create(
                body={
                    'name': f'_temp_pagecount',
                    'mimeType': google_mime
                },
                media_body=media,
                fields='id'
            ).execute()
            temp_doc_id = temp_doc['id']

            # Export as PDF and count
            pdf_content = drive_service.files().export(
                fileId=temp_doc_id,
                mimeType='application/pdf'
            ).execute()

            if not pdf_content:
                return None

            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                    tmp.write(pdf_content)
                    tmp_path = tmp.name

                with pikepdf.open(tmp_path) as pdf:
                    page_count = len(pdf.pages)

                logger.info(f"Exact page count (Supabase): {page_count} pages")
                return page_count
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)

        finally:
            if temp_doc_id:
                try:
                    drive_service.files().delete(fileId=temp_doc_id).execute()
                except Exception:
                    pass

    except Exception as e:
        logger.error(f"Page count from bytes error: {e}")
        return None


def _get_any_refresh_token() -> Optional[str]:
    """Get any valid Google refresh token from the database."""
    try:
        from backend import db
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT google_refresh_token FROM users WHERE google_refresh_token IS NOT NULL LIMIT 1"
            )
            row = cursor.fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.error(f"Failed to get refresh token: {e}")
        return None
