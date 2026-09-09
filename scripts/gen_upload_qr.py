#!/usr/bin/env python3
"""
Generate a QR code linking to the upload page.

Usage:
    python gen_upload_qr.py --url "http://192.168.1.100:5000" --output upload_qr.png
"""

import argparse
import qrcode
from pathlib import Path


def generate_upload_qr(url: str, output: str = "upload_qr.png") -> Path:
    """
    Generate a QR code that links to the upload page.
    
    Args:
        url: Full URL to the upload page
        output: Output file path
    
    Returns:
        Path to the generated QR code image
    """
    # Ensure URL doesn't have trailing slash
    url = url.rstrip("/")
    
    # Generate QR code
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    
    # Create image
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Save image
    output_path = Path(output)
    img.save(output_path)
    
    print(f"✓ Upload page QR code generated: {output_path.absolute()}")
    print(f"  URL: {url}")
    
    return output_path


def get_local_ip() -> str:
    """Try to get the local IP address of this machine."""
    import socket
    try:
        # This doesn't actually create a connection
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def main():
    parser = argparse.ArgumentParser(
        description="Generate a QR code for the upload page"
    )
    parser.add_argument(
        "--url", "-u",
        help="Full URL to upload page (default: auto-detect local IP with port 5000)"
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=5000,
        help="Port number if auto-detecting URL (default: 5000)"
    )
    parser.add_argument(
        "--output", "-o",
        default="upload_qr.png",
        help="Output file path (default: upload_qr.png)"
    )
    
    args = parser.parse_args()
    
    if args.url:
        url = args.url
    else:
        local_ip = get_local_ip()
        url = f"http://{local_ip}:{args.port}"
        print(f"ℹ Auto-detected local IP: {local_ip}")
    
    generate_upload_qr(url=url, output=args.output)


if __name__ == "__main__":
    main()
