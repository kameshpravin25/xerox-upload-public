import pytest
from playwright.sync_api import Page, expect

def test_staff_dashboard_authentication(page: Page, base_url: str):
    # The Staff Dashboard is protected by HTTP Basic Auth
    # URL format: http://username:password@localhost:5000/staff
    # Based on config.py default settings
    username = "anna"
    password = "xerox123"
    
    # We can inject credentials in the URL
    # Or in playwright use page.goto with credentials if needed, but injecting is simple
    # e.g., if base_url is https://xerox-upload.onrender.com:
    auth_url = base_url.replace("://", f"://{username}:{password}@")
    
    # Try logging in with valid credentials
    response = page.goto(f"{auth_url}/staff")
    assert response.status == 200
    
    # Check that the dashboard has loaded (e.g. title or recognizable element)
    expect(page).to_have_title("Staff Dashboard - Xerox Upload", timeout=10000)
    
    # Ensure a basic dashboard element is visible
    expect(page.locator("text=Staff Dashboard").first).to_be_visible()

def test_staff_dashboard_unauthorized(page: Page, base_url: str):
    # Try loading without credentials
    response = page.goto(f"{base_url}/staff")
    
    # It should prompt for auth or return 401
    assert response.status == 401
