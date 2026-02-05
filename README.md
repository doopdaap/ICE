# Minneapolis ICE Activity Monitor

Real-time monitoring system for ICE (Immigration and Customs Enforcement) activity in the Minneapolis/Twin Cities area. Collects reports from multiple community sources, correlates them across platforms, and sends alerts to Discord.

## Purpose

This tool helps community members stay informed about ICE enforcement activity in their neighborhoods by:

- Aggregating reports from trusted community platforms (Iceout.org, StopICE.net)
- Monitoring social media for real-time alerts (Bluesky, Instagram, Twitter/X)
- Correlating reports across sources to reduce false positives
- Sending timely Discord notifications when activity is detected

## Features

- **Multi-Source Collection**: Pulls from 6+ data sources including community reporting platforms, social media, and news RSS feeds
- **Smart Correlation**: Groups related reports using temporal, geographic, and content similarity analysis
- **Geographic Filtering**: Focuses on Greater Minneapolis area (50km radius from downtown)
- **Source-Based Trust**: High-priority sources (Iceout, StopICE) can trigger single-source alerts
- **News Filtering**: Filters out news articles about past events, court cases, and policy discussions
- **Stale Account Detection**: Automatically skips social media accounts that haven't posted in 90+ days

## Data Sources

| Source | Type | Description |
|--------|------|-------------|
| **Iceout.org** | Community Platform | Crowd-sourced ICE sighting reports with verification |
| **StopICE.net** | Community Platform | SMS/web-based alert network with live map |
| **Bluesky** | Social Media | 8 monitored accounts + keyword searches |
| **Instagram** | Social Media | 4 monitored community organization accounts |
| **Twitter/X** | Social Media | Auto-validated active accounts only |
| **RSS Feeds** | News | Star Tribune, MPR News, KARE 11 (strict filtering) |

## Installation

### Prerequisites

- Python 3.10+
- Chrome/Chromium browser (for Playwright)

### Setup

1. Clone the repository:
```bash
git clone https://github.com/yourusername/ice-monitor.git
cd ice-monitor
```

2. Create and activate a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Install Playwright browsers:
```bash
playwright install chromium
```

5. Download the spaCy model for location extraction:
```bash
python -m spacy download en_core_web_sm
```

6. Copy `.env.example` to `.env` and configure:
```bash
cp .env.example .env
```

## Configuration

Edit `.env` with your settings:

```env
# Required: Discord webhook for notifications
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# Optional: Twitter credentials (for search, not required for profile scraping)
TWITTER_ENABLED=true
TWITTER_USERNAME=
TWITTER_PASSWORD=

# Enable/disable collectors
ICEOUT_ENABLED=true
BLUESKY_ENABLED=true
INSTAGRAM_ENABLED=true
STOPICE_ENABLED=true

# Geographic filtering (50km = ~30 miles from downtown Minneapolis)
MAX_DISTANCE_KM=50.0

# Correlation settings
MIN_CORROBORATION_SOURCES=2  # Except high-priority sources
CLUSTER_EXPIRY_HOURS=6.0     # Stop updates after this time
```

## Usage

### Run the monitor:
```bash
python main.py
```

### Dry run (logs only, no Discord):
```bash
python main.py --dry-run
```

### Verbose logging:
```bash
python main.py --log-level DEBUG
```

## How It Works

### Collection Phase
Each collector polls its source at configured intervals:
- Iceout.org: Every 90 seconds
- Bluesky: Every 2 minutes
- Instagram: Every 5 minutes
- StopICE.net: Every 30 minutes
- RSS: Every 5 minutes

### Processing Phase
1. **Freshness Filter**: Discards reports older than 3 hours
2. **Relevance Filter**: Checks for ICE keywords + Minneapolis geographic references
3. **News Filter**: Rejects news articles without real-time signals (RSS sources require explicit real-time language)
4. **Location Extraction**: Uses spaCy NER + custom gazetteer to identify neighborhoods

### Correlation Phase
1. **Cluster Updates**: Checks if new reports match existing active incidents
2. **New Clusters**: Groups unclustered reports by similarity (temporal + geographic + content)
3. **High-Priority Singles**: Trusted sources (Iceout, StopICE) can alert without corroboration
4. **Confidence Scoring**: Rates clusters by source count, diversity, temporal tightness, and location precision

### Notification Phase
- **NEW**: First-time corroborated incident
- **UPDATE**: Additional reports added to existing incident
- Discord embeds include location, source count, confidence, and report summaries

## Project Structure

```
ice-monitor/
├── main.py                 # Application entry point
├── config.py               # Configuration management
├── collectors/             # Data source collectors
│   ├── base.py            # Abstract base collector
│   ├── rss_collector.py   # RSS feed collector
│   ├── iceout_collector.py    # Iceout.org scraper
│   ├── stopice_collector.py   # StopICE.net XML feed
│   ├── bluesky_collector.py   # Bluesky API collector
│   ├── instagram_collector.py # Instagram scraper
│   └── twitter_collector.py   # Twitter/X scraper
├── processing/             # Text and location processing
│   ├── text_processor.py  # Relevance filtering, news detection
│   ├── location_extractor.py  # NER + gazetteer location extraction
│   └── similarity.py      # TF-IDF content similarity
├── correlation/            # Report correlation engine
│   └── correlator.py      # Clustering and confidence scoring
├── notifications/          # Alert dispatching
│   └── discord_notifier.py
├── storage/                # Data persistence
│   ├── database.py        # SQLite async wrapper
│   └── models.py          # Data models
├── geodata/                # Geographic reference data
│   └── minneapolis_neighborhoods.json
└── tests/                  # Test files
```

## Monitored Accounts

### Bluesky (8 accounts)
- **News**: @startribune, @bringmethenews, @sahanjournal
- **Journalists**: @maxnesterak
- **Community**: @miracmn, @conmijente, @defend612, @sunrisemvmt

### Instagram (4 accounts)
- @sunrisetwincities, @indivisible_twincities, @mnfreedomfund, @isaiah_mn

### Twitter/X
Automatically validates and filters accounts. Only scrapes accounts that have posted within 90 days. Most activist accounts have migrated to Bluesky.

## Disclaimer

This tool is for informational purposes only. It aggregates publicly available information from community sources. The accuracy of reports depends on the underlying sources. Always verify information through official channels when making safety decisions.

## Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Submit a pull request

## License

MIT License - See LICENSE file for details.
