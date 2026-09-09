# Xerox Hotspot Upload System

An online xerox/print shop upload system with Razorpay payment integration.

## Features
- File upload (PDF, images, documents, videos)
- Cost estimation with print options
- Razorpay payment integration
- Staff dashboard for queue management
- Email notifications

## Local Development

```bash
# Install dependencies
pip install -r backend/requirements.txt

# Run server
python -m uvicorn backend.app:app --host 0.0.0.0 --port 5000
```

## Environment Variables

Copy `.env.example` to `.env` and configure:

```
RAZORPAY_KEY_ID=your_key_id
RAZORPAY_KEY_SECRET=your_key_secret
RAZORPAY_WEBHOOK_SECRET=your_webhook_secret
```

## Deployment

### Render

1. Push to GitHub
2. Connect repo on [render.com](https://render.com)
3. Create Web Service with:
   - **Build Command:** `pip install -r backend/requirements.txt`
   - **Start Command:** `uvicorn backend.app:app --host 0.0.0.0 --port $PORT`
4. Add environment variables in Render dashboard
