"""Test run: All cities loaded, dry-run mode.

With the multi-city architecture, ALL locales are loaded automatically.
Reports are tagged with their city by CityTagger, and Discord notifications
are filtered per-channel based on city subscriptions.

To verify Kansas City-specific behavior, check the logs for:
  - CityTagger tagging KC-area reports with city='kansascity'
  - Correlator grouping Kansas City reports separately
  - Bluesky rotating through all cities' search queries (including KC)
"""
from main import main
import sys

sys.argv = ["main.py", "--dry-run", "--log-level", "DEBUG"]
main()
