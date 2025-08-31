"""
APS Physics Journals Feed Processor

A tool for fetching, filtering, and enriching physics journal articles from APS RSS feeds.
"""

__version__ = "1.0.0"
__author__ = "Physics Journals Feed Processor"

# Main entry point for the package
from .main import main

__all__ = ["main"] 
