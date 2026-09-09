"""
PDF Processing Module

Provides functions for:
- Extracting selected pages from PDF
- Applying rotations to pages
"""

import logging
from pathlib import Path
from typing import List, Optional, Dict

logger = logging.getLogger(__name__)


def extract_pages(
    input_path: str,
    output_path: str,
    page_list: List[int],
    rotations: Optional[Dict[int, int]] = None
) -> bool:
    """
    Extract specific pages from a PDF and optionally rotate them.
    
    Args:
        input_path: Path to source PDF
        output_path: Path to save processed PDF
        page_list: List of 1-indexed page numbers to extract
        rotations: Dict of page_number -> rotation_degrees (90, 180, 270)
    
    Returns:
        True if successful, False otherwise
    """
    try:
        import pikepdf
        
        with pikepdf.open(input_path) as pdf:
            new_pdf = pikepdf.Pdf.new()
            
            for page_num in page_list:
                # Convert to 0-indexed
                idx = page_num - 1
                
                if 0 <= idx < len(pdf.pages):
                    page = pdf.pages[idx]
                    
                    # Apply rotation if specified
                    if rotations and page_num in rotations:
                        rotation = rotations[page_num]
                        # pikepdf rotation is cumulative, so we add to existing
                        current_rotation = int(page.get('/Rotate', 0))
                        page.Rotate = (current_rotation + rotation) % 360
                    
                    new_pdf.pages.append(page)
                else:
                    logger.warning(f"Page {page_num} out of range, skipping")
            
            if len(new_pdf.pages) > 0:
                new_pdf.save(output_path)
                logger.info(f"Extracted {len(new_pdf.pages)} pages to {output_path}")
                return True
            else:
                logger.error("No valid pages to extract")
                return False
                
    except ImportError:
        logger.error("pikepdf not installed, cannot process PDF")
        return False
    except Exception as e:
        logger.error(f"Failed to extract pages: {e}")
        return False


def get_page_count(pdf_path: str) -> int:
    """Get the number of pages in a PDF."""
    try:
        import pikepdf
        with pikepdf.open(pdf_path) as pdf:
            return len(pdf.pages)
    except:
        return 0
