"""
passenger_wsgi.py — cPanel / Phusion Passenger entry point

cPanel's Python App feature uses this file to start the Flask app.
Make sure your Application Startup File is set to: passenger_wsgi.py
"""

import sys
import os

# ── Path setup ────────────────────────────────────────────────────────────────
# Add the app directory to Python's path so imports work correctly.
app_dir = os.path.dirname(os.path.abspath(__file__))
if app_dir not in sys.path:
    sys.path.insert(0, app_dir)

# ── Load environment variables ────────────────────────────────────────────────
# python-dotenv reads your .env file even when Passenger doesn't forward it.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(app_dir, '.env'))
except ImportError:
    pass  # fall back to system environment

# ── Import the Flask app ──────────────────────────────────────────────────────
from app import app as application  # Passenger looks for a variable named "application"
