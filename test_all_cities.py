"""Test run: All cities loaded, dry-run mode.

With the multi-city architecture, ALL locales are loaded automatically
(no LOCALE env var needed). Reports are tagged with their city by
CityTagger, and Discord notifications are filtered per-channel based
on city subscriptions.
"""
from main import main
import sys

sys.argv = ["main.py", "--dry-run", "--log-level", "DEBUG"]
main()
