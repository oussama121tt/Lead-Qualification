"""Test bootstrap: repo root on sys.path, offline-only fixtures."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
