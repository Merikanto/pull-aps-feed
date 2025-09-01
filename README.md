# Physics Journals Feed Processor

A modular Python tool for fetching, filtering, and enriching physics journal articles from APS RSS feeds with arXiv data.

<br>

## How to Run It

```sh
# Install Poetry (if not already installed)
pip install poetry

# Install dependencies
poetry install

# Run the script
poetry run python aps-feed.py
```

<br>

## 📁 Project Structure

```
aps_feed/
├── __init__.py          # Package initialization and main imports
├── config.py            # Configuration constants and fuzzy matching thresholds
├── models.py            # Data models (FeedEntry class)
├── processors.py        # RSS feed processing and enhanced arXiv matching
├── utils.py             # Utility functions (keyword matching, deduplication)
├── output.py            # YAML and Markdown output generation
└── main.py              # Main orchestration logic with optimized processing flow

aps-feed.py  		         # Simple runner script
aps-feed-input.yml  	   # Configuration file (create this)
pyproject.toml           # Python dependencies
```

<br>

## 🚀 Quick Start

<br>

### 1. Install Dependencies

```bash
# Using Poetry (recommended - includes fuzzy matching libraries)
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
python -m aps_feed.main

# Method 3: Direct import and run
python -c "from aps_feed import main; main()"
```

<br>

## ✨ Enhanced Features

### 🔍 Advanced Fuzzy Matching for arXiv Articles

The processor now uses sophisticated fuzzy matching algorithms to find arXiv preprints that correspond to journal articles:

- **Multi-Algorithm Title Matching**: Combines 6 different similarity metrics

  - Word intersection similarity (original method)
  - Fuzzy ratio matching for overall string similarity
  - Fuzzy partial matching for title variations
  - Token sort ratio (order-independent word matching)
  - Token set ratio for flexible token matching
  - Word-level fuzzy matching
- **Enhanced Author Matching**: Three-strategy approach

  - Exact substring matching (original method)
  - Fuzzy string matching for name variations ("John Smith" ↔ "Smith, John")
  - Initials + last name matching ("J. Smith" ↔ "John Smith")
- **Smart Keyword Search**: Dual search strategy with prioritized scientific terms

  - Primary search with original keywords
  - Enhanced search prioritizing physics-specific terminology
  - Result deduplication and ranking

### 🚀 Optimized Processing Flow

**New Flow**: Feed Processing → arXiv Enrichment → Keyword Filtering → Output

- **All articles** are first enriched with arXiv data
- **Keyword filtering** is applied to enriched content (including richer arXiv summaries)
- **Result**: More relevant articles found through enhanced abstracts

**Previous Flow**: Feed Processing → Keyword Filtering → arXiv Enrichment → Output

- Limited to original RSS feed summaries for keyword matching

### ⚙️ Configurable Fuzzy Matching

Fine-tune matching behavior in `aps_feed/config.py`:

```python
# Fuzzy matching thresholds
TITLE_SIMILARITY_THRESHOLD = 0.75    # Overall similarity threshold (reduced for better recall)
AUTHOR_FUZZY_THRESHOLD = 85.0        # Author name fuzzy matching threshold
TITLE_FUZZY_WEIGHT_* = various       # Individual algorithm weights
```

<br>

## 📊 Output Files

- **`aps_results.yml`**: All processed articles in YAML format
- **`results/Aps_YYYYMMDD_HHMM.md`**: Filtered articles in Markdown with timestamp

<br>

## 🔧 Configuration Options

### Keyword Groups

- Articles must match **ALL** keywords in **at least one** group to be included
- Groups allow precise topic filtering with OR logic between groups
- Empty groups disable keyword filtering (includes all articles)
- **New**: Filtering applied to enriched arXiv summaries for better matches

<br>

### Network and Matching Settings

Edit `aps_feed/config.py`:

```python
# Performance settings
REQUEST_TIMEOUT = 15          # HTTP timeout in seconds
MAX_FEED_WORKERS = 20         # Parallel workers for RSS feeds
MAX_ARXIV_WORKERS = 15        # Parallel workers for arXiv enrichment
ARXIV_BATCH_SIZE = 50         # Batch size for arXiv processing

# Fuzzy matching thresholds (new)
TITLE_SIMILARITY_THRESHOLD = 0.75     # Minimum title similarity (0.0-1.0)
AUTHOR_FUZZY_THRESHOLD = 85.0         # Author name similarity (0.0-100.0)
TITLE_FUZZY_WEIGHT_INTERSECTION = 0.3 # Weight for word intersection
TITLE_FUZZY_WEIGHT_TOKEN_SORT = 0.25  # Weight for token sort matching
# ... additional fuzzy matching weights
```

<br>

### 🚀 Performance Features

- **Parallel RSS Processing**: Up to 20 concurrent feed downloads
- **Enhanced arXiv Enrichment**: Batch processing with 15 workers + fuzzy matching
- **Optimized Processing Flow**: Enrich all articles first, then filter on richer content
- **Connection Pooling**: Persistent HTTP connections with retry logic
- **Smart Caching**: Session reuse across requests
- **Pipeline Processing**: Early processing as feeds complete
- **Multi-Strategy Search**: Dual arXiv search with scientific term prioritization

<br>

## 🆘 Troubleshooting

### Import Errors

```bash
# Install missing dependencies (use Poetry for best results)
poetry install

# If using pip directly, install fuzzy matching library
pip install rapidfuzz feedparser requests beautifulsoup4 pyyaml

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
- Check network connectivity for RSS feeds and arXiv API
- Monitor arXiv API rate limits
- **New**: Adjust fuzzy matching thresholds if too many/few matches
- Consider reducing `TITLE_SIMILARITY_THRESHOLD` for more matches
- Increase `AUTHOR_FUZZY_THRESHOLD` for stricter author matching

<br>
