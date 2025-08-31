#!/usr/bin/env python3
"""
Runner script for the Physics Journals Feed Processor.

This script provides a simple entry point to run the modular physics journals
feed processor from the command line.

Usage:
    poetry run python aps-feed.py
"""

from physics_journals_feed import main

if __name__ == "__main__":
    main() 