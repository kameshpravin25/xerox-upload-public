#!/usr/bin/env python3
"""
Stress test the upload endpoint with concurrent requests.

Usage:
    python stress_upload.py --url "http://localhost:5000" --concurrent 5 --total 20
"""

import argparse
import asyncio
import aiohttp
import time
import random
import string
from pathlib import Path
from typing import List, Tuple


async def create_test_file(size_kb: int = 100) -> Tuple[bytes, str]:
    """Create a test file of specified size in KB."""
    content = ''.join(random.choices(string.ascii_letters + string.digits, k=size_kb * 1024))
    filename = f"test_{random.randint(1000, 9999)}.pdf"
    return content.encode(), filename


async def upload_file(
    session: aiohttp.ClientSession,
    url: str,
    file_content: bytes,
    filename: str,
    sender: str,
    passphrase: str = None
) -> dict:
    """Upload a single file and return the result."""
    start_time = time.time()
    
    data = aiohttp.FormData()
    data.add_field('file', file_content, filename=filename, content_type='application/pdf')
    data.add_field('sender', sender)
    data.add_field('phone', '555-0100')
    
    if passphrase:
        data.add_field('passphrase', passphrase)
    
    try:
        async with session.post(f"{url}/api/upload", data=data) as response:
            elapsed = time.time() - start_time
            status = response.status
            
            try:
                result = await response.json()
            except:
                result = {"error": await response.text()}
            
            return {
                "success": status == 200,
                "status": status,
                "elapsed": elapsed,
                "result": result
            }
    except Exception as e:
        return {
            "success": False,
            "status": 0,
            "elapsed": time.time() - start_time,
            "result": {"error": str(e)}
        }


async def run_stress_test(
    url: str,
    concurrent: int,
    total: int,
    file_size_kb: int,
    passphrase: str = None
) -> List[dict]:
    """Run the stress test with specified parameters."""
    print(f"\n🚀 Starting stress test...")
    print(f"   URL: {url}")
    print(f"   Concurrent uploads: {concurrent}")
    print(f"   Total uploads: {total}")
    print(f"   File size: {file_size_kb} KB")
    print()
    
    results = []
    semaphore = asyncio.Semaphore(concurrent)
    
    async def bounded_upload(session, i):
        async with semaphore:
            file_content, filename = await create_test_file(file_size_kb)
            sender = f"Stress Test User {i}"
            result = await upload_file(session, url, file_content, filename, sender, passphrase)
            result["upload_id"] = i
            
            status_icon = "✓" if result["success"] else "✗"
            print(f"   {status_icon} Upload {i}: {result['status']} ({result['elapsed']:.2f}s)")
            
            return result
    
    start_time = time.time()
    
    async with aiohttp.ClientSession() as session:
        tasks = [bounded_upload(session, i) for i in range(1, total + 1)]
        results = await asyncio.gather(*tasks)
    
    total_time = time.time() - start_time
    
    # Calculate statistics
    successful = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]
    
    if successful:
        avg_time = sum(r["elapsed"] for r in successful) / len(successful)
        min_time = min(r["elapsed"] for r in successful)
        max_time = max(r["elapsed"] for r in successful)
    else:
        avg_time = min_time = max_time = 0
    
    print("\n" + "=" * 50)
    print("📊 STRESS TEST RESULTS")
    print("=" * 50)
    print(f"Total uploads:      {total}")
    print(f"Successful:         {len(successful)} ({len(successful)/total*100:.1f}%)")
    print(f"Failed:             {len(failed)} ({len(failed)/total*100:.1f}%)")
    print(f"Total time:         {total_time:.2f}s")
    print(f"Throughput:         {len(successful)/total_time:.2f} uploads/sec")
    print()
    print("Response times (successful uploads):")
    print(f"  Average:          {avg_time:.3f}s")
    print(f"  Min:              {min_time:.3f}s")
    print(f"  Max:              {max_time:.3f}s")
    
    if failed:
        print("\nFailed uploads:")
        for r in failed[:5]:  # Show first 5 failures
            print(f"  Upload {r['upload_id']}: {r['result']}")
        if len(failed) > 5:
            print(f"  ... and {len(failed) - 5} more")
    
    print("=" * 50)
    
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Stress test the Xerox upload system"
    )
    parser.add_argument(
        "--url", "-u",
        default="http://localhost:5000",
        help="Base URL of the upload server (default: http://localhost:5000)"
    )
    parser.add_argument(
        "--concurrent", "-c",
        type=int,
        default=5,
        help="Number of concurrent uploads (default: 5)"
    )
    parser.add_argument(
        "--total", "-n",
        type=int,
        default=20,
        help="Total number of uploads to perform (default: 20)"
    )
    parser.add_argument(
        "--size", "-s",
        type=int,
        default=100,
        help="Size of test files in KB (default: 100)"
    )
    parser.add_argument(
        "--passphrase", "-p",
        default=None,
        help="Upload passphrase if required"
    )
    
    args = parser.parse_args()
    
    asyncio.run(run_stress_test(
        url=args.url,
        concurrent=args.concurrent,
        total=args.total,
        file_size_kb=args.size,
        passphrase=args.passphrase
    ))


if __name__ == "__main__":
    main()
