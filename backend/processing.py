"""
File Processing Pipeline for Xerox Hotspot Upload System

Provides:
- Office document to PDF conversion (LibreOffice headless)
- PDF compression (Ghostscript)
- Image optimization (Pillow)
- Background processing queue
"""

import os
import subprocess
import logging
from pathlib import Path
from typing import Optional, Tuple
import threading
from queue import Queue

logger = logging.getLogger(__name__)

# Processing queue for background tasks
processing_queue = Queue()
processing_thread = None


def is_libreoffice_available() -> bool:
    """Check if LibreOffice is installed."""
    try:
        result = subprocess.run(
            ["libreoffice", "--version"],
            capture_output=True,
            timeout=5
        )
        return result.returncode == 0
    except:
        return False


def is_ghostscript_available() -> bool:
    """Check if Ghostscript is installed."""
    try:
        result = subprocess.run(
            ["gs", "--version"],
            capture_output=True,
            timeout=5
        )
        return result.returncode == 0
    except:
        return False


def convert_office_to_pdf(input_path: Path, output_dir: Path) -> Optional[Path]:
    """
    Convert Office documents to PDF using LibreOffice headless.
    
    Supports: doc, docx, xls, xlsx, ppt, pptx, odt, ods, odp, txt, rtf
    """
    if not is_libreoffice_available():
        logger.warning("LibreOffice not available, skipping conversion")
        return None
    
    try:
        result = subprocess.run(
            [
                "libreoffice",
                "--headless",
                "--convert-to", "pdf",
                "--outdir", str(output_dir),
                "--",  # Prevent flag injection from filenames starting with --
                str(input_path)
            ],
            capture_output=True,
            timeout=120  # 2 minute timeout
        )
        
        if result.returncode == 0:
            # Output file will have same name but .pdf extension
            output_path = output_dir / (input_path.stem + ".pdf")
            if output_path.exists():
                logger.info(f"Converted {input_path.name} to PDF")
                return output_path
        else:
            logger.error(f"LibreOffice conversion failed: {result.stderr.decode()}")
    except subprocess.TimeoutExpired:
        logger.error(f"LibreOffice conversion timed out for {input_path}")
    except Exception as e:
        logger.error(f"Error converting to PDF: {e}")
    
    return None


def compress_pdf(input_path: Path, output_path: Path, quality: str = "ebook") -> bool:
    """
    Compress PDF using Ghostscript.
    
    Quality levels:
    - screen: Low quality, smallest size
    - ebook: Medium quality, good for most uses (default)
    - printer: High quality
    - prepress: Highest quality
    """
    if not is_ghostscript_available():
        logger.warning("Ghostscript not available, skipping compression")
        return False
    
    try:
        result = subprocess.run(
            [
                "gs",
                "-sDEVICE=pdfwrite",
                f"-dPDFSETTINGS=/{quality}",
                "-dCompatibilityLevel=1.4",
                "-dNOPAUSE",
                "-dQUIET",
                "-dBATCH",
                f"-sOutputFile={output_path}",
                "--",  # Prevent flag injection from filenames starting with --
                str(input_path)
            ],
            capture_output=True,
            timeout=180  # 3 minute timeout
        )
        
        if result.returncode == 0 and output_path.exists():
            original_size = input_path.stat().st_size
            compressed_size = output_path.stat().st_size
            reduction = (1 - compressed_size / original_size) * 100
            logger.info(f"Compressed PDF: {reduction:.1f}% reduction")
            return True
        else:
            logger.error(f"PDF compression failed: {result.stderr.decode()}")
    except subprocess.TimeoutExpired:
        logger.error(f"PDF compression timed out for {input_path}")
    except Exception as e:
        logger.error(f"Error compressing PDF: {e}")
    
    return False


def optimize_image(input_path: Path, output_path: Path, max_dimension: int = 2048) -> bool:
    """
    Optimize image for printing using Pillow.
    
    - Resize if larger than max_dimension
    - Convert to RGB if necessary
    - Save with optimized quality
    """
    try:
        from PIL import Image
        
        with Image.open(input_path) as img:
            # Convert to RGB if necessary (for CMYK, RGBA, etc.)
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            elif img.mode == 'CMYK':
                img = img.convert('RGB')
            
            # Resize if too large
            if max(img.size) > max_dimension:
                ratio = max_dimension / max(img.size)
                new_size = tuple(int(dim * ratio) for dim in img.size)
                img = img.resize(new_size, Image.Resampling.LANCZOS)
                logger.info(f"Resized image to {new_size}")
            
            # Save optimized
            img.save(output_path, "JPEG", quality=85, optimize=True)
            
            original_size = input_path.stat().st_size
            optimized_size = output_path.stat().st_size
            reduction = (1 - optimized_size / original_size) * 100
            logger.info(f"Optimized image: {reduction:.1f}% reduction")
            return True
            
    except ImportError:
        logger.warning("Pillow not available, skipping image optimization")
    except Exception as e:
        logger.error(f"Error optimizing image: {e}")
    
    return False


def image_to_pdf(input_path: Path, output_path: Path) -> bool:
    """Convert an image to PDF."""
    try:
        from PIL import Image
        
        with Image.open(input_path) as img:
            # Convert to RGB if necessary
            if img.mode in ('RGBA', 'P'):
                rgb_img = img.convert('RGB')
            else:
                rgb_img = img
            
            rgb_img.save(output_path, "PDF", resolution=100.0)
            logger.info(f"Converted image to PDF: {output_path.name}")
            return True
            
    except ImportError:
        logger.warning("Pillow not available, skipping image to PDF conversion")
    except Exception as e:
        logger.error(f"Error converting image to PDF: {e}")
    
    return False


def process_file(filepath: Path, output_dir: Path) -> Tuple[Optional[Path], str]:
    """
    Process a file through the optimization pipeline.
    
    Returns: (processed_filepath, status)
    """
    extension = filepath.suffix.lower()
    
    # Office documents -> PDF
    office_extensions = {'.doc', '.docx', '.ppt', '.pptx',
                         '.odt', '.odp', '.txt', '.rtf'}
    if extension in office_extensions:
        result = convert_office_to_pdf(filepath, output_dir)
        if result:
            return result, 'converted'
        return None, 'conversion_failed'
    
    # PDF -> Compress
    if extension == '.pdf':
        output_path = output_dir / f"{filepath.stem}_optimized.pdf"
        if compress_pdf(filepath, output_path):
            return output_path, 'compressed'
        return filepath, 'original'  # Return original if compression fails
    
    # Images -> Optimize and optionally convert to PDF
    image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff'}
    if extension in image_extensions:
        output_path = output_dir / f"{filepath.stem}_optimized.jpg"
        if optimize_image(filepath, output_path):
            return output_path, 'optimized'
        return filepath, 'original'
    
    # Videos and other files - no processing
    return filepath, 'no_processing'


def process_job_background(ticket: str, filepath: str, output_dir: str):
    """Background task to process a job."""
    try:
        from . import db
        
        filepath = Path(filepath)
        output_dir = Path(output_dir)
        
        processed_path, status = process_file(filepath, output_dir)
        
        # Update job in database
        db.update_job_processing_status(
            ticket,
            status,
            str(processed_path) if processed_path else None
        )
        
        logger.info(f"Job {ticket} processed: {status}")
    except Exception as e:
        logger.error(f"Error processing job {ticket}: {e}")


def start_processing_worker():
    """Start the background processing worker thread."""
    global processing_thread
    
    def worker():
        while True:
            try:
                task = processing_queue.get()
                if task is None:
                    break
                process_job_background(*task)
                processing_queue.task_done()
            except Exception as e:
                logger.error(f"Processing worker error: {e}")
    
    processing_thread = threading.Thread(target=worker, daemon=True)
    processing_thread.start()
    logger.info("Processing worker started")


def queue_job_for_processing(ticket: str, filepath: str, output_dir: str):
    """Add a job to the processing queue."""
    processing_queue.put((ticket, filepath, output_dir))
