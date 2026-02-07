"""Test run: All cities loaded, dry-run mode.

With the multi-city architecture, ALL locales are loaded automatically.
Reports are tagged with their city by CityTagger, and Discord notifications
are filtered per-channel based on city subscriptions.

To verify Atlanta-specific behavior, check the logs for:
  - CityTagger tagging Atlanta-area reports with city='atlanta'
  - Correlator grouping Atlanta reports separately
  - Bluesky rotating through all cities' search queries (including Atlanta)
"""
from main import main
import sys

sys.argv = ["main.py", "--dry-run"]
main()
