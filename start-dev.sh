#!/bin/bash
# Development Server Script
# Use this for local development with hot-reload

echo "🔧 Starting Xerox Upload in DEVELOPMENT mode..."
echo "   Hot-reload enabled - changes will be reflected automatically"
echo ""

python -m uvicorn backend.app:app \
    --host 0.0.0.0 \
    --port 5000 \
    --reload
