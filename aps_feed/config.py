"""
Configuration and session management for the Physics Journals Feed Processor.

This module contains all configuration constants, optimized HTTP session management,
and configuration file loading functionality.
"""

import logging
from pathlib import Path
from typing import Any, Dict

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =============================================================================
# CONFIGURATION CONSTANTS
# =============================================================================

# File paths
INPUT_FILE = Path("aps-feed-input.yml")  # YAML config with keyword groups and feed URLs
OUTPUT_YAML_FILE = Path("aps_feed/aps_results.yml")       # All processed articles (YAML format)

# Network request settings
REQUEST_TIMEOUT = 15          # HTTP request timeout in seconds (reduced for faster failure)
MAX_ARXIV_RESULTS = 20        # Maximum number of arXiv search results to process

# Parallelism optimization
MAX_FEED_WORKERS = 20         # Maximum parallel workers for RSS feed processing
MAX_ARXIV_WORKERS = 15        # Maximum parallel workers for arXiv enrichment
ARXIV_BATCH_SIZE = 50         # Process arXiv in batches to optimize memory/performance

# Article matching thresholds
TITLE_SIMILARITY_THRESHOLD = 0.9  # Minimum title similarity for arXiv matching (0.0-1.0)
MIN_WORD_LENGTH = 3               # Minimum word length for title processing

# Search optimization
MAX_SEARCH_WORDS = 6          # Maximum number of title words to use in arXiv search queries

# =============================================================================
# LOGGING SETUP
# =============================================================================

logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# =============================================================================
# OPTIMIZED SESSION MANAGEMENT
# =============================================================================

def create_optimized_session() -> requests.Session:
    """
    Create an optimized requests session with connection pooling and retry logic.
    
    This session is optimized for concurrent requests with automatic retries
    and connection pooling to improve performance.
    
    Returns:
        Configured requests.Session with optimizations
    """
    session = requests.Session()
    
    # Configure retry strategy for transient failures
    retry_strategy = Retry(
        total=3,                    # Total number of retries
        status_forcelist=[429, 500, 502, 503, 504],  # HTTP status codes to retry
        backoff_factor=0.5,         # Backoff factor between retries
        respect_retry_after_header=True  # Respect server retry-after headers
    )
    
    # Configure HTTP adapter with connection pooling
    adapter = HTTPAdapter(
        pool_connections=50,        # Number of connection pools
        pool_maxsize=50,           # Maximum number of connections per pool
        max_retries=retry_strategy,
        pool_block=False           # Don't block when pool is full
    )
    
    # Mount adapter for both HTTP and HTTPS
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    # Set default headers for better performance
    session.headers.update({
        'User-Agent': 'APS-Feed-Processor/1.0 (Scientific Research Tool)',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive'
    })
    
    return session

# Global session instance for reuse across requests
_global_session = None

def get_session() -> requests.Session:
    """Get or create the global optimized session."""
    global _global_session
    if _global_session is None:
        _global_session = create_optimized_session()
    return _global_session

# =============================================================================
# CONFIGURATION LOADING
# =============================================================================

def load_config() -> Dict[str, Any]:
    """
    Load keyword groups and RSS feed configurations from YAML input file.
    
    This function reads the configuration file and handles both legacy flat keyword
    lists and the new group-based keyword structure. If the input file doesn't exist,
    it raises an error.
    
    Returns:
        Dictionary containing:
        - "keywords": Dict[str, List[str]] mapping group names to keyword lists
        - "feeds": List[Dict[str, str]] containing feed URL and description pairs
        
    Raises:
        FileNotFoundError: If configuration file doesn't exist
        Exception: If configuration file cannot be read or parsed
    """
    try:
        # Check if configuration file exists
        if not INPUT_FILE.exists():
            logger.error(f"❌ Configuration file {INPUT_FILE} not found")
            raise FileNotFoundError(f"Configuration file {INPUT_FILE} is required but not found. Please create it with Keywords and Feed-URL sections.")
        
        with INPUT_FILE.open('r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
            keywords_data = data.get("Keywords", {})
            feed_url_data = data.get("Feed-URL", {})
            
            # Handle different keyword data formats and edge cases
            no_filtering_msg = "⚠️  Keywords section is empty - keyword filtering is DISABLED. All articles will be included."
            
            if keywords_data is None or (isinstance(keywords_data, dict) and not keywords_data):
                # Empty or None keywords section - disable keyword filtering
                keyword_groups = {}
                logger.warning(no_filtering_msg)
            elif isinstance(keywords_data, list):
                # Old format: convert to single group (backwards compatibility)
                if not keywords_data:
                    keyword_groups = {}
                    logger.warning(no_filtering_msg)
                else:
                    keyword_groups = {"Default Group": keywords_data}
                    logger.info(f"Loaded {len(keywords_data)} keywords in legacy format from {INPUT_FILE}")
            else:
                # New format: use groups as-is
                keyword_groups = keywords_data
                if keyword_groups:
                    total_keywords = sum(len(group) for group in keyword_groups.values())
                    group_summary = ", ".join([f"{name}({len(group)} keywords)" for name, group in keyword_groups.items()])
                    logger.info(f"Loaded {total_keywords} keywords in {len(keyword_groups)} groups from {INPUT_FILE}: {group_summary}")
                else:
                    logger.warning(no_filtering_msg)
            
            # Convert Feed-URL structure to list of feed info
            feed_info = []
            for description, urls in feed_url_data.items():
                # Normalize to list format for consistent processing
                url_list = urls if isinstance(urls, list) else [urls]
                for url in url_list:
                    feed_info.append({"description": description, "url": url})
            
            logger.info(f"Loaded {len(feed_info)} feeds from {INPUT_FILE}")
            
            return {
                "keywords": keyword_groups,
                "feeds": feed_info
            }
    
    except FileNotFoundError:
        # Re-raise FileNotFoundError to stop execution
        raise
    except Exception as e:
        logger.error(f"❌ Error loading configuration from {INPUT_FILE}: {e}")
        raise Exception(f"Failed to load configuration file {INPUT_FILE}: {e}") 