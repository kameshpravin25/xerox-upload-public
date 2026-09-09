import pytest
from playwright.sync_api import Page, expect

def test_homepage_upload(page: Page, base_url: str):
    # Go to the homepage with guest overlay bypass if needed
    page.goto(f"{base_url}/?guest=1")
    
    # Check title
    expect(page).to_have_title("Upload & Print - Upload Your Document")

    # The file input might be hidden or styled, so Playwright sets it directly
    # We will create a small temporary correct file to upload
    with open("test_upload.txt", "w") as f:
        f.write("This is a test document.")

    # Upload the file using the specific file input
    # Assuming form structure has <input type="file" id="file" name="file">
    page.locator('input#file[type="file"]').set_input_files("test_upload.txt")
    
    # Wait for file to render in the UI
    page.wait_for_selector(".file-name", state="visible")
    
    # Fill out the guest name which is required when guest=1
    page.locator("#guestName").fill("Automated Test User")
    
    # Click the Add to Cart or Submit button
    submit_btn = page.locator('button#submitBtn')
    expect(submit_btn).to_be_visible()
    
    # Set copies and options just to be sure
    # using select or setting values where visible
    # Then add to cart
    submit_btn.click()
    
    # Wait for the item to appear in the cart
    page.wait_for_selector(".cart-item", state="visible")
    
    # Click the Pay Now button
    checkout_btn = page.locator('button#mainCheckoutBtn')
    expect(checkout_btn).to_be_enabled()
    checkout_btn.click()
    
    # After submission, it should show Cashfree checkout or a success message.
    # We will wait for either the cashfree checkout iframe or the success card.
    try:
        page.wait_for_selector("iframe[src*='cashfree'], .success-ticket-card", timeout=15000)
    except Exception:
        pytest.fail("Neither Cashfree checkout nor success card appeared.")
    
    # Cleanup temporary test file happens automatically or could be ignored for this simple verification.
