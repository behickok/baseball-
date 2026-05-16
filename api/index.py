"""Vercel serverless entry point. All routes are rewritten here via vercel.json."""
import sys
from pathlib import Path

# Make the project root importable so we can pull in app.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import app  # noqa: E402,F401  (re-exported as the WSGI handler)
