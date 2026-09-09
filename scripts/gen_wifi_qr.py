#!/usr/bin/env python3
"""
Generate a Wi-Fi QR code for customers to scan and join the network.

Usage:
    python gen_wifi_qr.py --ssid "XeroxShop" --password "secret123" --output wifi_qr.png
"""

import argparse
import qrcode
from pathlib import Path


def generate_wifi_qr(
    ssid: str,
    password: str,
    security: str = "WPA",
    hidden: bool = False,
    output: str = "wifi_qr.png"
) -> Path:
    """
    Generate a QR code for Wi-Fi network connection.
    
    Args:
        ssid: Network name
        password: Network password
        security: Security type (WPA, WEP, or nopass)
        hidden: Whether the network is hidden
        output: Output file path
    
    Returns:
        Path to the generated QR code image
    """
    # Build Wi-Fi QR payload
    # Format: WIFI:T:WPA;S:SSID;P:password;H:bool;;
    hidden_str = "true" if hidden else "false"
    
    if security.upper() == "NOPASS":
        payload = f"WIFI:T:nopass;S:{ssid};H:{hidden_str};;"
    else:
        payload = f"WIFI:T:{security.upper()};S:{ssid};P:{password};H:{hidden_str};;"
    
    # Generate QR code
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    
    # Create image
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Save image
    output_path = Path(output)
    img.save(output_path)
    
    print(f"✓ Wi-Fi QR code generated: {output_path.absolute()}")
    print(f"  SSID: {ssid}")
    print(f"  Security: {security}")
    print(f"  Hidden: {hidden}")
    
    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Generate a Wi-Fi QR code for customers"
    )
    parser.add_argument(
        "--ssid", "-s",
        required=True,
        help="Wi-Fi network name (SSID)"
    )
    parser.add_argument(
        "--password", "-p",
        default="",
        help="Wi-Fi password (leave empty for open networks)"
    )
    parser.add_argument(
        "--security", "-t",
        choices=["WPA", "WEP", "nopass"],
        default="WPA",
        help="Security type (default: WPA)"
    )
    parser.add_argument(
        "--hidden",
        action="store_true",
        help="Network is hidden"
    )
    parser.add_argument(
        "--output", "-o",
        default="wifi_qr.png",
        help="Output file path (default: wifi_qr.png)"
    )
    
    args = parser.parse_args()
    
    if args.security != "nopass" and not args.password:
        parser.error("Password is required for WPA/WEP networks")
    
    generate_wifi_qr(
        ssid=args.ssid,
        password=args.password,
        security=args.security,
        hidden=args.hidden,
        output=args.output
    )


if __name__ == "__main__":
    main()
