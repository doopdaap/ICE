"""Test all locale loading paths — single, multi, and merge."""
import os, sys, traceback
sys.stdout.reconfigure(encoding='utf-8')

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])

from processing.locale import load_locale, load_locales, merge_locales

LOCALES = ["minneapolis", "atlanta", "kansascity"]

def test_single_locale(name):
    print(f"\n{'='*60}")
    print(f"  SINGLE LOCALE: {name}")
    print(f"{'='*60}")
    try:
        loc = load_locale(name)
        print(f"  [OK] display_name={loc.display_name}")
        print(f"       center=({loc.center_lat}, {loc.center_lon}), radius={loc.radius_km}")
        print(f"       centers={loc.centers}")
        print(f"       timezone={loc.timezone}")
        print(f"       geo_keywords={len(loc.geo_keywords)}")
        print(f"       rss_feeds={loc.rss_feeds}")
        print(f"       neighborhoods_file='{loc.neighborhoods_file}'")
        print(f"       landmarks_file='{loc.landmarks_file}'")
        print(f"       instagram_monitored={len(loc.instagram_monitored_accounts)}: {loc.instagram_monitored_accounts}")
        print(f"       twitter_all={len(loc.twitter_all_mn_focused)}: {loc.twitter_all_mn_focused}")
        print(f"       bluesky_accounts={len(loc.bluesky_monitored_accounts)}: {loc.bluesky_monitored_accounts}")
        return loc
    except Exception as e:
        print(f"  [FAIL] {type(e).__name__}: {e}")
        traceback.print_exc()
        return None

def test_location_extractor(loc):
    nf = loc.neighborhoods_file
    lf = loc.landmarks_file
    nf_isfile = os.path.isfile(nf) if nf else False
    lf_isfile = os.path.isfile(lf) if lf else False
    nf_isdir = os.path.isdir(nf) if nf else False
    lf_isdir = os.path.isdir(lf) if lf else False
    print(f"  --- LocationExtractor ---")
    print(f"      neighborhoods: isfile={nf_isfile} isdir={nf_isdir} path='{nf}'")
    print(f"      landmarks:     isfile={lf_isfile} isdir={lf_isdir} path='{lf}'")

    try:
        from processing.location_extractor import LocationExtractor
        ext = LocationExtractor(
            neighborhoods_file=loc.neighborhoods_file,
            landmarks_file=loc.landmarks_file,
        )
        print(f"  [OK] gazetteer entries: {len(ext._gazetteer)}")
    except Exception as e:
        print(f"  [FAIL] {type(e).__name__}: {e}")

def test_geo_keywords(loc):
    from processing.text_processor import init_geo_keywords
    try:
        init_geo_keywords(loc)
        print(f"  [OK] init_geo_keywords ({len(loc.geo_keywords)} keywords)")
    except Exception as e:
        print(f"  [FAIL] init_geo_keywords: {e}")
    try:
        regex = loc.build_geo_regex()
        print(f"  [OK] build_geo_regex (len={len(regex.pattern)})")
    except Exception as e:
        print(f"  [FAIL] build_geo_regex: {e}")

def test_multi_locale():
    print(f"\n{'='*60}")
    print(f"  MULTI-LOCALE MERGE: minneapolis,atlanta")
    print(f"{'='*60}")
    try:
        os.environ["LOCALE"] = "minneapolis,atlanta"
        merged = load_locales()
        print(f"  [OK] name={merged.name}")
        print(f"       display_name={merged.display_name}")
        print(f"       centers={merged.centers}")
        print(f"       geo_keywords={len(merged.geo_keywords)}")
        print(f"       rss_feeds={merged.rss_feeds}")
        print(f"       twitter_all={len(merged.twitter_all_mn_focused)}")
        print(f"       instagram={len(merged.instagram_monitored_accounts)}")
        print(f"       neighborhoods_file='{merged.neighborhoods_file}'")
        print(f"       landmarks_file='{merged.landmarks_file}'")
        return merged
    except Exception as e:
        print(f"  [FAIL] {type(e).__name__}: {e}")
        traceback.print_exc()
        return None

def test_all_three_merge():
    print(f"\n{'='*60}")
    print(f"  MULTI-LOCALE MERGE: all 3 cities")
    print(f"{'='*60}")
    try:
        os.environ["LOCALE"] = "minneapolis,atlanta,kansascity"
        merged = load_locales()
        print(f"  [OK] name={merged.name}")
        print(f"       display_name={merged.display_name}")
        print(f"       centers={merged.centers}")
        print(f"       geo_keywords={len(merged.geo_keywords)}")
        print(f"       rss_feeds={merged.rss_feeds}")
        return merged
    except Exception as e:
        print(f"  [FAIL] {type(e).__name__}: {e}")
        traceback.print_exc()
        return None

if __name__ == "__main__":
    # Test each locale individually
    for name in LOCALES:
        loc = test_single_locale(name)
        if loc:
            test_geo_keywords(loc)
            test_location_extractor(loc)

    # Test multi-locale merge
    merged = test_multi_locale()
    if merged:
        test_geo_keywords(merged)
        test_location_extractor(merged)

    test_all_three_merge()

    print(f"\n{'='*60}")
    print("  DONE")
    print(f"{'='*60}")
