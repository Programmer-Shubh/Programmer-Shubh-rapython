"""WSGI entry point for PythonAnywhere - wraps FastAPI app."""
import os
import sys

# Add project root to path
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

# Set required environment variables
os.environ.setdefault("DB_PATH", os.path.join(project_root, "data", "ratrade.db"))

# Use a WSGI adapter for FastAPI
from fastapi import FastAPI
from fastapi.middleware.wsgi import WSGIMiddleware

# Import the FastAPI app
from main import app as fastapi_app

# Wrap FastAPI app with WSGIMiddleware
application = WSGIMiddleware(fastapi_app)