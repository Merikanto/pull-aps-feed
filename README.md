# Physics Journals Feed Processor - Modular Version

A modular Python tool for fetching, filtering, and enriching physics journal articles from APS RSS feeds with arXiv data.

<br>

## How to Run It

```sh
# install poetry
pip install poetry

# install deps - In project Root Folder
poetry install

# Run the script
poetry run python aps-feed.py
```

<br>

## 📁 Project Structure

```
physics_journals_feed/
├── __init__.py          # Package initialization and main imports
├── config.py            # Configuration constants and YAML loading
├── models.py            # Data models (FeedEntry class)
├── processors.py        # RSS feed processing and arXiv enrichment
├── utils.py             # Utility functions (keyword matching, deduplication)
├── output.py            # YAML and Markdown output generation
└── main.py              # Main orchestration logic

aps-feed.py  # Simple runner script
requirements.txt         # Python dependencies
aps-feed-input.yml  # Configuration file (create this)
```

<br>

## 🚀 Quick Start

<br>

### 1. Install Dependencies

```bash
# Option 1: Using pip
pip install -r requirements.txt

# Option 2: Using poetry (if you have it)
poetry install
```

<br>

### 2. Create Configuration File

Create `aps-feed-input.yml` with your keyword groups and feed URLs:

```yaml
# Matching is done by groups of keywords
Keywords:
  Group 1:
    - Bose-Einstein
    - Gross-Pitaevskii
  Group 2:
    - Bose-Einstein
    - Fifth force
  Group 3:
    - Bose-Einstein
    - Symmetry

Feed-URL:
  Recently published in Reviews of Modern Physics:
  - https://feeds.aps.org/rss/recent/rmp.xml
  
  Recently published in PRL:
  - https://feeds.aps.org/rss/recent/prl.xml
  
  PRL - Quantum Information, Science, and Technology:
  - https://feeds.aps.org/rss/tocsec/PRL-GeneralPhysicsStatisticalandQuantumMechanicsQuantumInformationetc.xml
```

<br>

### 3. Run the Processor

```bash
# Method 1: Using the runner script
poetry run python aps-feed.py

# Method 2: Running as a module
python -m physics_journals_feed.main

# Method 3: Direct import and run
python -c "from physics_journals_feed import main; main()"
```

<br>

## 📊 Output Files

- **`aps_results.yml`**: All processed articles in YAML format
- **`results/Aps_YYYYMMDD_HHMM.md`**: Filtered articles in Markdown with timestamp

<br>

## 🔧 Configuration Options

### Keyword Groups

- Articles must match **ALL** keywords in **at least one** group
- Groups allow precise topic filtering
- Empty groups disable keyword filtering

<br>

### Network Settings

Edit `physics_journals_feed/config.py`:

```python
REQUEST_TIMEOUT = 15          # HTTP timeout in seconds
MAX_FEED_WORKERS = 20         # Parallel workers for RSS feeds
MAX_ARXIV_WORKERS = 15        # Parallel workers for arXiv enrichment
ARXIV_BATCH_SIZE = 50         # Batch size for arXiv processing
```

<br>

### 🚀 Performance Features

- **Parallel RSS Processing**: Up to 20 concurrent feed downloads
- **Optimized arXiv Enrichment**: Batch processing with 15 workers
- **Connection Pooling**: Persistent HTTP connections with retry logic
- **Smart Caching**: Session reuse across requests
- **Pipeline Processing**: Early processing as feeds complete

<br>

## 🆘 Troubleshooting

### Import Errors

```bash
# Install missing dependencies
pip install -r requirements.txt

# Check Python path
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
```

<br>

### Configuration Errors

- Ensure `aps-feed-input.yml` exists in the current directory
- Validate YAML syntax
- Check keyword group format

<br>

### Performance Issues

- Adjust worker limits in `config.py`
- Check network connectivity
- Monitor arXiv API rate limits

<br>
