"""
RSS feed processing and arXiv enrichment for the Physics Journals Feed Processor.

This module contains the core processing logic for fetching RSS feeds,
extracting article data, and enriching articles with arXiv information.
"""

import logging
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional

import feedparser
import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz, process

from .config import (ARXIV_BATCH_SIZE, ARXIV_REQUEST_DELAY,
                     AUTHOR_FUZZY_THRESHOLD, MAX_ARXIV_RESULTS,
                     MAX_ARXIV_WORKERS, MAX_SEARCH_WORDS, MIN_WORD_LENGTH,
                     REQUEST_TIMEOUT, TITLE_FUZZY_WEIGHT_INTERSECTION,
                     TITLE_FUZZY_WEIGHT_PARTIAL, TITLE_FUZZY_WEIGHT_RATIO,
                     TITLE_FUZZY_WEIGHT_TOKEN_SET,
                     TITLE_FUZZY_WEIGHT_TOKEN_SORT,
                     TITLE_FUZZY_WEIGHT_WORD_FUZZY, TITLE_SIMILARITY_THRESHOLD,
                     get_rate_limit_count, get_session,
                     increment_rate_limit_count)
from .models import FeedEntry
from .utils import check_keywords

# Setup logger
logger = logging.getLogger(__name__)


class PRLFeedProcessor:
    """
    Processes RSS feeds from APS physics journals.
    
    This class handles fetching, parsing, and processing of RSS feed entries from
    American Physical Society journals. It extracts article metadata, cleans content,
    and applies keyword group filtering.
    
    Attributes:
        feed_url (str): URL of the RSS feed
        feed_description (str): Human-readable description of the feed source
    """
    
    def __init__(self, feed_url: str, feed_description: str) -> None:
        """
        Initialize RSS feed processor.
        
        Args:
            feed_url: URL of the RSS feed to process
            feed_description: Human-readable description of the feed source
        """
        self.feed_url = feed_url
        self.feed_description = feed_description
    
    def fetch_entries(self) -> List[Dict[str, Any]]:
        """
        Fetch and parse RSS feed entries from the configured URL.
        
        Returns:
            List of raw feed entry dictionaries from feedparser
            
        Raises:
            requests.RequestException: Network-related errors during feed fetching
            Exception: Other unexpected errors during parsing
        """
        try:
            logger.info(f"🌐 Fetching RSS feed: {self.feed_description}")
            logger.debug(f"Feed URL: {self.feed_url}")
            
            # Parse RSS feed using feedparser library
            parsed_feed = feedparser.parse(self.feed_url)
            
            # Check for XML parsing issues
            if parsed_feed.bozo:
                logger.warning(f"⚠️  Feed parser detected malformed XML in {self.feed_description}")
            
            entries = list(parsed_feed.get("entries", []))
            logger.info(f"📥 Retrieved {len(entries)} raw entries from {self.feed_description}")
            
            return entries
        
        except requests.RequestException as e:
            logger.error(f"❌ Network error while fetching {self.feed_description}: {e}")
            raise
        except Exception as e:
            logger.error(f"❌ Unexpected error while fetching {self.feed_description}: {e}")
            raise
    
    def extract_authors(self, entry: Dict[str, Any]) -> List[str]:
        """
        Extract and clean author names from RSS feed entry.
        
        Handles both structured author arrays and single author strings,
        with fallback processing for various author formatting patterns.
        
        Args:
            entry: Raw RSS feed entry dictionary
            
        Returns:
            List of cleaned author names
        """
        authors = []
        
        # Try structured authors field first (preferred format)
        if entry.get("authors"):
            logger.debug("Processing structured authors field")
            for author in entry.get("authors", []):
                name = author.get("name", "").strip()
                if name:
                    authors.extend(self._split_author_string(name))
        
        # Fallback to single author field (legacy format)
        elif entry.get("author"):
            logger.debug("Processing single author field")
            author_str = entry.get("author", "").strip()
            authors.extend(self._split_author_string(author_str))
        
        # Filter out empty author names
        cleaned_authors = [author for author in authors if author]
        logger.debug(f"Extracted {len(cleaned_authors)} authors")
        
        return cleaned_authors
    
    def _split_author_string(self, author_str: str) -> List[str]:
        """
        Split author string on common delimiters and handle 'et al.' cases.
        
        Args:
            author_str: Raw author string from feed entry
            
        Returns:
            List of cleaned author names
        """
        # Handle "et al." cases - extract the first author name
        if 'et al' in author_str.lower():
            # Extract everything before "et al" and clean it up
            before_et_al = re.split(r'\s+et\s+al', author_str, flags=re.IGNORECASE)[0].strip()
            # Remove any trailing punctuation, parentheses, or collaboration info
            before_et_al = re.sub(r'\s*[<(].*$', '', before_et_al)  # Remove anything after < or (
            before_et_al = re.sub(r'[,\s]+$', '', before_et_al)  # Remove trailing commas/spaces
            if before_et_al:
                return [before_et_al.strip()]
            else:
                return []
        
        # Handle different author separators
        if ';' in author_str:
            authors = [name.strip() for name in author_str.split(';')]
        elif ', and ' in author_str:
            # Handle "Author1, Author2, and Author3" format
            parts = author_str.split(', and ')
            if len(parts) == 2:
                first_authors = [name.strip() for name in parts[0].split(',')]
                last_author = [parts[1].strip()]
                authors = first_authors + last_author
            else:
                authors = [author_str.strip()]
        elif ' and ' in author_str:
            # Handle "Author1 and Author2" format
            authors = [name.strip() for name in author_str.split(' and ')]
        elif ',' in author_str:
            # Handle comma-separated authors
            authors = [name.strip() for name in author_str.split(',')]
        else:
            authors = [author_str.strip()]
        
        return [author for author in authors if author]
    
    def extract_summary(self, entry: Dict[str, Any], authors: List[str]) -> str:
        """Extract and clean summary text from feed entry."""
        # Get raw summary
        summary_html = (entry.get("summary", "") or 
                       entry.get("content", [{}])[0].get("value", ""))
        
        if not summary_html:
            return ""
        
        # Convert HTML to text
        summary_text = BeautifulSoup(summary_html, "html.parser").get_text(" ").strip()
        
        # Clean up summary
        return self._clean_summary_text(summary_text, authors)
    
    def _clean_summary_text(self, text: str, authors: List[str]) -> str:
        """
        Remove unwanted content from summary text.
        
        Args:
            text: Raw summary text
            authors: List of author names to remove from summary
            
        Returns:
            Cleaned summary text
        """
        # Remove Author(s): prefix
        text = re.sub(r'^Author\(s\):[^A-Z]*', '', text)
        
        # Remove author names from summary
        for author in authors:
            text = text.replace(author + ',', '')
            text = text.replace(author + ';', '')
            text = text.replace(author + ' and', ' and')
            text = text.replace('and ' + author, '')
            text = text.replace(author, '')
        
        # Remove journal reference brackets
        text = re.sub(r'\[Phys\..*?\]', '', text)
        
        # Remove publication date
        text = re.sub(r'Published\s+\w+\s+\w+\s+\d+,\s+\d+\s*$', '', text)
        
        # Clean up whitespace and punctuation
        text = re.sub(r'\s+', ' ', text)
        text = re.sub(r'^[\s,;]+', '', text)
        text = re.sub(r'^\s*and\s+', '', text)
        
        return text.strip()
    
    def extract_doi(self, link: str) -> str:
        """Extract DOI from article link."""
        if link and "doi/" in link:
            return link.split("doi/")[-1]
        return ""
    
    def format_published_date(self, published: Optional[str]) -> str:
        """Format published date as YYYY-MM-DD."""
        if not published:
            return ""
        
        try:
            # Parse ISO format and return date only
            dt = datetime.strptime(published[:19], "%Y-%m-%dT%H:%M:%S")
            return dt.strftime("%Y-%m-%d")
        except (ValueError, IndexError):
            # Fallback: extract date part if 'T' separator exists
            if "T" in published:
                return published.split("T")[0]
            return published
    
    def extract_link(self, entry: Dict[str, Any]) -> str:
        """Extract article link from feed entry."""
        # Try direct link field first
        if entry.get("link"):
            return entry.get("link")
        
        # Try links array
        for link in entry.get("links", []):
            href = link.get("href")
            if href:
                return href
        
        return ""
    
    def process_entry(self, entry: Dict[str, Any], keyword_groups: Dict[str, List[str]]) -> Optional[FeedEntry]:
        """Process a single feed entry into a FeedEntry object."""
        try:
            title = entry.get("title", "").strip()
            if not title:
                logger.warning("Skipping entry with no title")
                return None
            
            authors = self.extract_authors(entry)
            link = self.extract_link(entry)
            doi = self.extract_doi(link)
            summary = self.extract_summary(entry, authors)
            published = self.format_published_date(
                entry.get("published") or entry.get("updated")
            )
            
            # Check for keyword matches
            keywords = check_keywords(title, summary, keyword_groups)
            
            return FeedEntry(
                title=title,
                authors=authors,
                link=link,
                doi=doi,
                published=published,
                summary=summary,
                keywords=keywords,
                source_feed=self.feed_description
            )
        
        except Exception as e:
            logger.warning(f"Failed to process entry '{entry.get('title', 'Unknown')}': {e}")
            return None
    
    def process_feed(self, keyword_groups: Dict[str, List[str]]) -> List[FeedEntry]:
        """
        Process entire RSS feed and return filtered FeedEntry objects.
        
        This method fetches all entries from the RSS feed, processes each one to extract
        metadata and check for keyword group matches, and returns only entries that
        contain matching keywords.
        
        Args:
            keyword_groups: Dictionary mapping group names to lists of required keywords
            
        Returns:
            List of FeedEntry objects that matched at least one keyword group
        """
        # Fetch raw RSS feed entries
        raw_entries = self.fetch_entries()
        processed_entries = []
        
        logger.info(f"🔄 Processing {len(raw_entries)} entries from {self.feed_description}")
        
        # Process each entry and apply keyword filtering
        for i, raw_entry in enumerate(raw_entries, 1):
            logger.debug(f"Processing entry {i}/{len(raw_entries)}")
            processed_entry = self.process_entry(raw_entry, keyword_groups)
            if processed_entry:
                processed_entries.append(processed_entry)
        
        logger.info(f"✅ Successfully processed {len(processed_entries)} matching entries from {self.feed_description}")
        return processed_entries


class ArxivMatcher:
    """
    Handles arXiv article matching and enrichment.
    
    This class provides functionality to search the arXiv database for articles that match
    APS journal entries based on title similarity and author verification. It uses the
    arXiv API to find potential matches and applies sophisticated matching algorithms
    to identify the same article across both platforms.
    
    Key Features:
    - Title-based semantic search using meaningful words
    - Author name verification with flexible matching
    - Title similarity scoring with configurable thresholds
    - Parallel processing support for multiple articles
    - Search result caching for performance optimization
    
    Attributes:
        base_url (str): arXiv API query endpoint URL
        search_cache (dict): Cache for arXiv search results to avoid duplicate API calls
    """
    
    def __init__(self) -> None:
        """Initialize arXiv matcher with API configuration and caching."""
        self.base_url = "http://export.arxiv.org/api/query"
        self.search_cache = {}  # Simple cache to avoid duplicate searches
    
    def warm_cache_for_likely_matches(self, entries: List[FeedEntry]) -> None:
        """
        Pre-identify and cache search results for articles likely to have arXiv matches.
        
        This method analyzes titles to identify articles that are likely to have arXiv
        preprints and performs searches for them early to warm the cache.
        
        Args:
            entries: List of FeedEntry objects to analyze
        """
        # Identify articles likely to have arXiv matches based on title characteristics
        likely_matches = []
        
        for entry in entries:
            title = entry.title.lower()
            
            # Look for indicators of recent research that's likely on arXiv
            indicators = [
                'quantum', 'topological', 'superconducting', 'magnetic', 'electronic',
                'phase transition', 'condensed matter', 'field theory', 'many-body',
                'entanglement', 'correlation', 'simulation', 'calculation', 'study',
                'theory', 'model', 'effect', 'properties', 'dynamics'
            ]
            
            # Check if title contains research indicators and has sufficient length
            if (len(entry.title.split()) >= 6 and  # Reasonable title length
                any(indicator in title for indicator in indicators) and
                not any(exclusion in title for exclusion in ['review', 'comment', 'erratum'])):
                likely_matches.append(entry)
        
        if likely_matches:
            logger.info(f"🔥 Pre-warming cache for {len(likely_matches)} likely arXiv matches...")
            
            # Perform searches for likely matches with conservative parallelism
            cache_workers = min(5, len(likely_matches))  # Very conservative for cache warming
            
            with ThreadPoolExecutor(max_workers=cache_workers) as executor:
                futures = [
                    executor.submit(self.search_arxiv, entry.title, MAX_ARXIV_RESULTS)
                    for entry in likely_matches[:20]  # Limit to first 20 to avoid overwhelming API
                ]
                
                completed = 0
                for future in as_completed(futures):
                    try:
                        future.result()  # Just trigger the search to warm cache
                        completed += 1
                    except Exception as e:
                        logger.debug(f"Cache warming failed for one entry: {e}")
            
            logger.info(f"🔥 Cache warming completed: {completed}/{len(futures)} searches cached")
    
    def extract_title_words(self, title: str) -> List[str]:
        """
        Extract meaningful alphabetic words from article title for semantic matching.
        
        This method cleans and preprocesses article titles to extract words suitable
        for arXiv search queries. It removes mathematical notation, expands common
        scientific abbreviations, and filters out stop words.
        
        Args:
            title: Raw article title string
            
        Returns:
            List of meaningful words suitable for search queries
        """
        # Remove all mathematical notation, special characters, and numbers
        # Keep only alphabetic characters and spaces
        clean_title = re.sub(r'[\$\{\}\[\]\\^_{}]', '', title)  # Remove LaTeX/math symbols
        clean_title = re.sub(r'\b\d+\b', '', clean_title)  # Remove standalone numbers
        clean_title = re.sub(r'[^a-zA-Z\s\-]', ' ', clean_title)  # Keep only letters, spaces, hyphens
        clean_title = re.sub(r'\s+', ' ', clean_title).strip().lower()
        
        # Handle common scientific abbreviations - expand them for better matching
        # This improves search success by using full terms instead of abbreviations
        clean_title = re.sub(r'\bcdw\b', 'charge density wave', clean_title)  # CDW = Charge Density Wave
        clean_title = re.sub(r'\bqhe\b', 'quantum hall effect', clean_title)  # QHE = Quantum Hall Effect
        clean_title = re.sub(r'\bafm\b', 'antiferromagnetic', clean_title)    # AFM = Antiferromagnetic
        clean_title = re.sub(r'\bfm\b', 'ferromagnetic', clean_title)         # FM = Ferromagnetic
        
        # Split into words and filter out short/common words
        words = clean_title.split()
        # Filter out very short words (1-2 chars) and common words
        stop_words = {
            'the', 'and', 'for', 'with', 'via', 'from', 'into', 'using', 
            'ion', 'an', 'in', 'of', 'to', 'on', 'at', 'by', 'as'
        }
        meaningful_words = [
            word for word in words 
            if len(word) >= MIN_WORD_LENGTH and word not in stop_words
        ]
        
        return meaningful_words
    
    def normalize_word(self, word: str) -> str:
        """Normalize a word for better matching."""
        # Handle common variations
        word = word.lower().strip()
        # Handle hyphenated words - normalize to non-hyphenated form
        if '-' in word:
            # Convert "multi-band" to "multiband", "ultra-narrow" to "ultranarrow" 
            word = word.replace('-', '')
        
        # Handle common scientific term variations
        word_mappings = {
            'hunt': 'search',
            'investigate': 'study',
            'explore': 'study',
            'analyse': 'analyze',  # British vs American spelling
            'realise': 'realize',
            'optimise': 'optimize'
        }
        
        return word_mappings.get(word, word)
    
    def calculate_title_similarity(self, title1: str, title2: str) -> float:
        """
        Calculate similarity between two titles using multiple fuzzy matching approaches.
        
        Combines several similarity metrics to provide robust title matching:
        1. Word-based intersection similarity (original method)
        2. Fuzzy string similarity for overall title matching
        3. Fuzzy partial matching for handling title variations
        
        Args:
            title1: First title to compare
            title2: Second title to compare
            
        Returns:
            Float between 0.0 and 1.0 representing similarity confidence
        """
        # Method 1: Original word-based intersection similarity
        words1_raw = self.extract_title_words(title1)
        words2_raw = self.extract_title_words(title2)
        
        # Normalize words for better matching
        words1 = set(self.normalize_word(word) for word in words1_raw)
        words2 = set(self.normalize_word(word) for word in words2_raw)
        
        word_similarity = 0.0
        if words1 and words2:
            # Calculate intersection over minimum (more lenient than Jaccard)
            intersection = len(words1.intersection(words2))
            min_length = min(len(words1), len(words2))
            word_similarity = intersection / min_length if min_length > 0 else 0.0
        
        # Method 2: Fuzzy string similarity on cleaned titles
        # Clean titles for fuzzy matching
        clean_title1 = self._clean_title_for_fuzzy_match(title1)
        clean_title2 = self._clean_title_for_fuzzy_match(title2)
        
        # Use multiple fuzzy matching algorithms
        ratio_similarity = fuzz.ratio(clean_title1, clean_title2) / 100.0
        partial_similarity = fuzz.partial_ratio(clean_title1, clean_title2) / 100.0
        token_sort_similarity = fuzz.token_sort_ratio(clean_title1, clean_title2) / 100.0
        token_set_similarity = fuzz.token_set_ratio(clean_title1, clean_title2) / 100.0
        
        # Method 3: Fuzzy matching on key word strings
        words1_str = ' '.join(words1)
        words2_str = ' '.join(words2)
        word_fuzzy_similarity = fuzz.ratio(words1_str, words2_str) / 100.0 if words1_str and words2_str else 0.0
        
        # Combine all similarity scores with weighted average
        # Use configurable weights for different matching methods
        combined_similarity = (
            word_similarity * TITLE_FUZZY_WEIGHT_INTERSECTION +           # Original word intersection method
            ratio_similarity * TITLE_FUZZY_WEIGHT_RATIO +                 # Overall string similarity
            partial_similarity * TITLE_FUZZY_WEIGHT_PARTIAL +             # Partial matching for title variations
            token_sort_similarity * TITLE_FUZZY_WEIGHT_TOKEN_SORT +       # Order-independent word matching
            token_set_similarity * TITLE_FUZZY_WEIGHT_TOKEN_SET +         # Set-based token matching
            word_fuzzy_similarity * TITLE_FUZZY_WEIGHT_WORD_FUZZY         # Fuzzy matching on key words
        )
        
        # Only log detailed breakdown for high similarity scores or debug mode
        if combined_similarity > 0.7:
            logger.debug(f"Title similarity breakdown:")
            logger.debug(f"  Word intersection: {word_similarity:.3f}")
            logger.debug(f"  Fuzzy ratio: {ratio_similarity:.3f}")
            logger.debug(f"  Fuzzy partial: {partial_similarity:.3f}")
            logger.debug(f"  Token sort: {token_sort_similarity:.3f}")
            logger.debug(f"  Token set: {token_set_similarity:.3f}")
            logger.debug(f"  Word fuzzy: {word_fuzzy_similarity:.3f}")
            logger.debug(f"  Combined: {combined_similarity:.3f}")
        
        return combined_similarity
    
    def _clean_title_for_fuzzy_match(self, title: str) -> str:
        """
        Clean title for fuzzy string matching by removing/normalizing problematic characters.
        
        Args:
            title: Raw title string
            
        Returns:
            Cleaned title suitable for fuzzy matching
        """
        # Convert to lowercase for case-insensitive matching
        clean_title = title.lower()
        
        # Remove common mathematical notation that can interfere with matching
        clean_title = re.sub(r'[\$\{\}\[\]\\^_{}]', '', clean_title)
        
        # Normalize hyphenation variations (Multi-Band → multiband)
        clean_title = re.sub(r'\b(\w+)-(\w+)\b', r'\1\2', clean_title)
        
        # Normalize common verb variations for better matching
        clean_title = re.sub(r'\bhunt\s+for\b', 'search for', clean_title)
        clean_title = re.sub(r'\binvestigate\s+', 'study ', clean_title)
        clean_title = re.sub(r'\bexplore\s+', 'study ', clean_title)
        
        # Normalize whitespace
        clean_title = re.sub(r'\s+', ' ', clean_title).strip()
        
        # Remove common prefixes/suffixes that might differ between venues
        clean_title = re.sub(r'^(the\s+)?', '', clean_title)
        clean_title = re.sub(r'\s+(article|paper|study)$', '', clean_title)
        
        return clean_title
    
    def search_arxiv(self, title: str, max_results: int = MAX_ARXIV_RESULTS) -> List[Dict[str, Any]]:
        """
        Search arXiv for articles matching the title using optimized search strategy.
        
        Uses single optimized search with enhanced keyword selection for better performance.
        
        Args:
            title: Article title to search for
            max_results: Maximum number of results to return
            
        Returns:
            List of arXiv article entries
        """
        try:
            # Extract key words from the title for search
            title_words = self.extract_title_words(title)
            
            if not title_words:
                logger.warning(f"No meaningful words extracted from title: '{title}'")
                return []
            
            # Use single optimized search strategy (faster than dual search)
            if len(title_words) <= MAX_SEARCH_WORDS:
                # If few words, use all of them
                search_words = title_words
            else:
                # Use enhanced keyword selection for better results
                search_words = self._select_enhanced_keywords(title_words, MAX_SEARCH_WORDS)
            
            results = self._perform_arxiv_search(search_words, max_results)
            logger.debug(f"ArXiv search returned {len(results)} results")
            return results
        
        except requests.RequestException as e:
            logger.warning(f"Network error during arXiv search for '{title}': {e}")
            return []
        except Exception as e:
            logger.warning(f"Unexpected error during arXiv search for '{title}': {e}")
            return []
    
    def _perform_arxiv_search(self, search_words: List[str], max_results: int) -> List[Dict[str, Any]]:
        """Perform actual arXiv API search with given keywords and caching."""
        if not search_words:
            return []
        
        # Sanitize search words for arXiv API
        sanitized_words = []
        for word in search_words:
            # Remove special characters that might cause issues
            clean_word = re.sub(r'[^\w\s-]', '', word)
            if clean_word and len(clean_word) >= 2:
                sanitized_words.append(clean_word)
        
        if not sanitized_words:
            logger.debug("No valid search words after sanitization")
            return []
        
        search_query = ' '.join(sanitized_words)
        
        # Check cache first to avoid duplicate API calls
        cache_key = f"{search_query}:{max_results}"
        if cache_key in self.search_cache:
            logger.debug(f"Using cached results for: '{search_query}'")
            return self.search_cache[cache_key]
        
        # Add configurable delay to respect arXiv API rate limits
        time.sleep(ARXIV_REQUEST_DELAY)  # Configurable delay between requests
        
        try:
            # Try multiple query formats, starting with most compatible
            query_attempts = [
                search_query,            # Simple keyword search (most compatible)
                f'ti:{search_query}',    # Title search 
                f'au:{search_query}'     # Author search (if title fails)
            ]
            
            for attempt, query_format in enumerate(query_attempts, 1):
                try:
                    params = {
                        "search_query": query_format,
                        "start": 0,
                        "max_results": max_results,
                    }
                    
                    # Use safe URL encoding to avoid malformed queries
                    url = f"{self.base_url}?" + urllib.parse.urlencode(params, safe=':')
                    logger.debug(f"Searching arXiv (attempt {attempt}): '{search_query}' using {query_format}")
                    
                    # Use optimized session for better performance
                    session = get_session()
                    response = session.get(url, timeout=REQUEST_TIMEOUT)
                    
                    # Handle specific error cases
                    if response.status_code == 406:
                        # 406 = Query format not accepted, try next format silently
                        logger.debug(f"Query format '{query_format}' not accepted, trying next format...")
                        continue
                    elif response.status_code == 429:
                        # 429 = Rate limited - this is a real issue that needs attention
                        current_count = increment_rate_limit_count()
                        backoff_time = ARXIV_REQUEST_DELAY * (2 ** attempt)
                        logger.error(f"🚨🚨🚨 RATE LIMITED BY ARXIV API (HTTP 429) #{current_count} 🚨🚨🚨")
                        logger.error(f"🔥 Query: '{search_query}' - Attempt {attempt}")
                        logger.error(f"⏱️  Backing off for {backoff_time:.1f}s before retry...")
                        print(f"\n🚨🚨🚨 RATE LIMITED BY ARXIV #{current_count}! Waiting {backoff_time:.1f}s... 🚨🚨🚨")
                        time.sleep(backoff_time)
                        continue
                    
                    response.raise_for_status()
                    
                    parsed = feedparser.parse(response.text)
                    results = list(parsed.get("entries", []))
                    
                    # Cache successful results
                    self.search_cache[cache_key] = results
                    logger.debug(f"✅ arXiv search successful with format: {query_format}")
                    
                    return results
                    
                except requests.HTTPError as e:
                    if e.response.status_code == 406 and attempt < len(query_attempts):
                        # 406 just means query format not accepted, try next format silently
                        logger.debug(f"Query format {attempt} not accepted, trying next format...")
                        continue
                    elif e.response.status_code == 429:
                        # Rate limiting - this needs attention
                        current_count = increment_rate_limit_count()
                        logger.error(f"🚨🚨🚨 RATE LIMITED BY ARXIV API (HTTP 429) #{current_count} 🚨🚨🚨")
                        logger.error(f"🔥 Exception on attempt {attempt} for query: '{search_query}'")
                        print(f"\n🚨🚨🚨 RATE LIMITED BY ARXIV #{current_count}! (Exception handler) 🚨🚨🚨")
                        if attempt == len(query_attempts):
                            raise
                    else:
                        # Other HTTP errors are worth logging
                        logger.warning(f"HTTP error {e.response.status_code} on attempt {attempt}: {e}")
                        if attempt == len(query_attempts):
                            raise
                except requests.RequestException as e:
                    logger.warning(f"Network error on attempt {attempt}: {e}")
                    if attempt == len(query_attempts):
                        raise
            
            # If all attempts failed, try simplified search as last resort
            logger.debug(f"All query formats rejected for '{search_query}' - trying simplified search")
            if len(sanitized_words) > 2:
                return self._perform_arxiv_search(sanitized_words[:2], max_results)
            
            # Return empty results if everything fails (this is normal, not an error)
            logger.debug(f"No compatible query format found for '{search_query}' - no results")
            return []
            
        except Exception as e:
            logger.error(f"Unexpected error during arXiv search for '{search_query}': {e}")
            return []
    
    def _select_enhanced_keywords(self, title_words: List[str], max_words: int) -> List[str]:
        """
        Select enhanced keywords using better prioritization strategies.
        
        Prioritizes words that are more likely to be distinctive for scientific articles.
        
        Args:
            title_words: List of extracted title words
            max_words: Maximum number of keywords to select
            
        Returns:
            List of selected keywords for search
        """
        if len(title_words) <= max_words:
            return title_words
        
        # Define word categories with different priorities
        high_priority_words = []
        medium_priority_words = []
        low_priority_words = []
        
        # Scientific terms that are often distinctive
        scientific_indicators = {
            'quantum', 'magnetic', 'electronic', 'optical', 'thermal', 'mechanical',
            'superconducting', 'ferromagnetic', 'antiferromagnetic', 'topological',
            'phase', 'transition', 'crystal', 'molecular', 'atomic', 'nuclear',
            'photonic', 'plasmonic', 'metamaterial', 'nanoscale', 'microscale'
        }
        
        # Physics-specific terms
        physics_terms = {
            'field', 'wave', 'particle', 'energy', 'momentum', 'spin', 'charge',
            'current', 'voltage', 'resistance', 'conductivity', 'temperature',
            'pressure', 'density', 'frequency', 'wavelength', 'amplitude'
        }
        
        for word in title_words:
            word_lower = word.lower()
            
            # High priority: distinctive scientific terms
            if word_lower in scientific_indicators:
                high_priority_words.append(word)
            # Medium priority: physics terms and longer words
            elif word_lower in physics_terms or len(word) >= 7:
                medium_priority_words.append(word)
            # Low priority: common words
            else:
                low_priority_words.append(word)
        
        # Select keywords in priority order
        selected_keywords = []
        
        # Add high priority words first
        for word in high_priority_words[:max_words]:
            selected_keywords.append(word)
        
        # Fill remaining slots with medium priority words
        remaining_slots = max_words - len(selected_keywords)
        for word in medium_priority_words[:remaining_slots]:
            selected_keywords.append(word)
        
        # Fill any remaining slots with low priority words
        remaining_slots = max_words - len(selected_keywords)
        for word in low_priority_words[:remaining_slots]:
            selected_keywords.append(word)
        
        # If we still don't have enough words, use original order
        if len(selected_keywords) < max_words:
            for word in title_words:
                if word not in selected_keywords:
                    selected_keywords.append(word)
                    if len(selected_keywords) >= max_words:
                        break
        
        return selected_keywords[:max_words]
    
    def extract_arxiv_authors(self, entry: Dict[str, Any]) -> List[str]:
        """Extract author names from arXiv entry."""
        authors = []
        for author in entry.get("authors", []):
            name = author.get("name", "").strip()
            if name:
                authors.append(name)
        return authors
    
    def verify_author_match(
        self, 
        prl_authors: List[str], 
        arxiv_authors: List[str], 
        check_first_two_only: bool = False
    ) -> bool:
        """
        Check if PRL authors match arXiv authors using fuzzy string matching.
        
        Uses multiple matching strategies:
        1. Exact substring matching (original method)
        2. Last name exact matching
        3. Fuzzy string matching for name variations
        4. Initials + last name matching
        
        Args:
            prl_authors: List of author names from PRL article
            arxiv_authors: List of author names from arXiv article
            check_first_two_only: Whether to only check first two PRL authors
            
        Returns:
            True if at least one author match is found
        """
        if not prl_authors or not arxiv_authors:
            return False
        
        # Check first two PRL authors against all arXiv authors
        authors_to_check = prl_authors[:2] if check_first_two_only else prl_authors
        
        for prl_author in authors_to_check:
            prl_author = prl_author.strip()
            if not prl_author:
                continue
                
            for arxiv_author in arxiv_authors:
                arxiv_author = arxiv_author.strip()
                if not arxiv_author:
                    continue
                
                # Strategy 1: Original exact matching
                if self._exact_author_match(prl_author, arxiv_author):
                    logger.debug(f"Exact author match: '{prl_author}' ~ '{arxiv_author}'")
                    return True
                
                # Strategy 2: Fuzzy matching for name variations
                if self._fuzzy_author_match(prl_author, arxiv_author):
                    logger.debug(f"Fuzzy author match: '{prl_author}' ~ '{arxiv_author}'")
                    return True
                
                # Strategy 3: Initials + last name matching
                if self._initials_author_match(prl_author, arxiv_author):
                    logger.debug(f"Initials author match: '{prl_author}' ~ '{arxiv_author}'")
                    return True
        
        return False
    
    def _exact_author_match(self, prl_author: str, arxiv_author: str) -> bool:
        """Check for exact author name matches using original method."""
        # Extract last names for comparison
        prl_last = prl_author.split()[-1].lower() if prl_author.split() else ""
        arxiv_last = arxiv_author.split()[-1].lower() if arxiv_author.split() else ""
        
        # Match if:
        # 1. Full name matches (in either direction)
        # 2. Last names match exactly
        return (prl_author.lower() in arxiv_author.lower() or 
                arxiv_author.lower() in prl_author.lower() or
                (prl_last and arxiv_last and prl_last == arxiv_last))
    
    def _fuzzy_author_match(self, prl_author: str, arxiv_author: str, threshold: float = AUTHOR_FUZZY_THRESHOLD) -> bool:
        """Check for fuzzy author name matches to handle variations."""
        # Clean author names for comparison
        prl_clean = re.sub(r'[^\w\s]', '', prl_author.lower()).strip()
        arxiv_clean = re.sub(r'[^\w\s]', '', arxiv_author.lower()).strip()
        
        if not prl_clean or not arxiv_clean:
            return False
        
        # Use fuzzy string matching
        similarity = fuzz.ratio(prl_clean, arxiv_clean)
        
        # Also try partial matching for cases like "John Smith" vs "Smith, John"
        partial_similarity = fuzz.partial_ratio(prl_clean, arxiv_clean)
        
        # Use the better of the two scores
        best_similarity = max(similarity, partial_similarity)
        
        return best_similarity >= threshold
    
    def _initials_author_match(self, prl_author: str, arxiv_author: str) -> bool:
        """Check for matches using initials + last name pattern."""
        def extract_initials_lastname(name: str) -> tuple:
            """Extract initials and last name from author name."""
            parts = name.strip().split()
            if len(parts) < 2:
                return ("", "")
            
            # Last part is assumed to be last name
            last_name = parts[-1].lower()
            
            # Extract initials from first/middle names
            initials = "".join([part[0].lower() for part in parts[:-1] if part])
            
            return (initials, last_name)
        
        prl_initials, prl_last = extract_initials_lastname(prl_author)
        arxiv_initials, arxiv_last = extract_initials_lastname(arxiv_author)
        
        # Match if last names match and initials are compatible
        if not (prl_last and arxiv_last and prl_initials and arxiv_initials):
            return False
        
        # Last names must match
        if prl_last != arxiv_last:
            return False
        
        # Check if initials are compatible (one is subset of other)
        return (prl_initials in arxiv_initials or 
                arxiv_initials in prl_initials or
                prl_initials == arxiv_initials)
    
    def extract_arxiv_summary(self, entry: Dict[str, Any]) -> str:
        """Extract clean summary from arXiv entry."""
        summary = entry.get("summary", "").strip()
        if summary:
            # Clean up the summary
            summary = BeautifulSoup(summary, "html.parser").get_text(" ").strip()
            summary = re.sub(r'\s+', ' ', summary)
        return summary
    
    def get_arxiv_link(self, entry: Dict[str, Any]) -> str:
        """Extract arXiv link from entry."""
        link = entry.get("id", "") or entry.get("link", "")
        # Ensure it's an abs link, not pdf
        if link and "/pdf/" in link:
            link = link.replace("/pdf/", "/abs/")
        return link
    
    def find_matching_article(
        self, 
        title: str, 
        authors: List[str]
    ) -> Optional[Dict[str, str]]:
        """Find matching arXiv article for given title and authors using optimized matching with fallback strategies."""
        logger.info(f"Searching arXiv for: '{title}'")
        
        # Quick pre-filter: skip articles with very short titles (likely not research papers)
        if len(title.split()) < 3:
            logger.debug(f"Skipping short title: '{title}'")
            return None
        
        # Primary Strategy: Use API search
        rate_limit_count = get_rate_limit_count()
        api_blocked = rate_limit_count > 20  # If many 406/429 errors, API is likely blocked
        
        if not api_blocked:
            # Try normal API search first
            arxiv_results = self.search_arxiv(title)
            if arxiv_results:
                return self._process_search_results(title, authors, arxiv_results)
        
        # Fallback Strategy: Alternative search methods when API is blocked/failing
        logger.info(f"🔄 Primary search failed - trying alternative strategies for: '{title[:50]}...'")
        
        # Strategy 1: Direct arXiv ID extraction from title/text
        direct_match = self._try_direct_arxiv_id_extraction(title)
        if direct_match:
            return direct_match
        
        # Strategy 2: Author-based search with simplified queries
        if authors:
            author_match = self._try_author_based_search(title, authors)
            if author_match:
                return author_match
        
        # Strategy 3: Web-based verification (last resort)
        web_match = self._try_web_based_search(title, authors)
        if web_match:
            return web_match
        
        # Check if this is due to systematic API blocking
        if api_blocked:
            logger.warning(f"⚠️ arXiv API may be blocking requests - '{title}' enrichment skipped")
        else:
            logger.debug(f"No arXiv results found for '{title}'")
        return None
    
    def _process_search_results(self, title: str, authors: List[str], arxiv_results: List[Dict[str, Any]]) -> Optional[Dict[str, str]]:
        """Process arXiv search results and find best match."""
        logger.debug(f"Found {len(arxiv_results)} potential matches, checking title similarity and authors...")
        
        best_match = None
        best_score = 0.0
        
        for result in arxiv_results:
            arxiv_title = result.get("title", "").strip()
            arxiv_authors = self.extract_arxiv_authors(result)
            
            # Quick pre-check: if no authors, skip
            if not arxiv_authors:
                logger.debug(f"Skipping arXiv article with no authors: '{arxiv_title[:50]}...'")
                continue
            
            # Calculate title similarity
            title_similarity = self.calculate_title_similarity(title, arxiv_title)
            
            # Early termination: if title similarity is very low, skip author check
            if title_similarity < 0.3:  # Much lower than threshold for early exit
                logger.debug(f"Skipping low similarity ({title_similarity:.2f}): '{arxiv_title[:50]}...'")
                continue
            
            # Check if first or second author matches
            author_match = self.verify_author_match(authors, arxiv_authors, check_first_two_only=True)
            
            logger.debug(f"Title similarity: {title_similarity:.2f}, Author match: {author_match}")
            logger.debug(f"  PRL: '{title}'")
            logger.debug(f"  arXiv: '{arxiv_title}'")
            logger.debug(f"  PRL authors: {authors[:2]}")
            logger.debug(f"  arXiv authors: {arxiv_authors[:3]}...")
            
            # Accept if title similarity >= threshold AND (first or second author matches)
            if title_similarity >= TITLE_SIMILARITY_THRESHOLD and author_match:
                if title_similarity > best_score:
                    best_score = title_similarity
                    best_match = result
                    
        if best_match:
            summary = self.extract_arxiv_summary(best_match)
            link = self.get_arxiv_link(best_match)
            
            logger.info(f"Found matching arXiv article (similarity: {best_score:.2f}): {link}")
            return {
                "summary": summary,
                "link": link
            }
        
        return None
    
    def _try_direct_arxiv_id_extraction(self, title: str) -> Optional[Dict[str, str]]:
        """
        Try to extract arXiv ID directly from title or look for known patterns.
        
        This method attempts to find arXiv identifiers that might be embedded
        in the title or use pattern matching for well-known paper formats.
        """
        logger.debug("🔍 Trying direct arXiv ID extraction...")
        
        # Look for arXiv ID patterns in title (sometimes papers reference their arXiv version)
        arxiv_id_pattern = r'(?:arXiv:)?(\d{4}\.\d{4,5}(?:v\d+)?)'
        match = re.search(arxiv_id_pattern, title, re.IGNORECASE)
        
        if match:
            arxiv_id = match.group(1)
            logger.info(f"🎯 Found arXiv ID in title: {arxiv_id}")
            
            # Construct arXiv link directly
            arxiv_link = f"http://arxiv.org/abs/{arxiv_id}"
            
            # Try to get summary by direct API call to this specific paper
            try:
                paper_details = self._get_paper_details_by_id(arxiv_id)
                if paper_details:
                    return {
                        "summary": paper_details.get("summary", ""),
                        "link": arxiv_link
                    }
            except Exception as e:
                logger.debug(f"Failed to get details for arXiv:{arxiv_id}: {e}")
            
            # Even if we can't get summary, return the link
            return {
                "summary": "",
                "link": arxiv_link
            }
        
        return None
    
    def _try_author_based_search(self, title: str, authors: List[str]) -> Optional[Dict[str, str]]:
        """
        Try simplified author-based search when main API search fails.
        
        Uses only author names with very simple queries to avoid API rejection.
        """
        logger.debug("🔍 Trying author-based search...")
        
        # Try with first author only using very simple query format
        if not authors:
            return None
        
        first_author = authors[0]
        # Extract just the last name for search
        author_parts = first_author.strip().split()
        if not author_parts:
            return None
        
        last_name = author_parts[-1]
        
        # Use very simple search query
        simple_query = last_name.lower()
        
        try:
            # Direct API call with minimal query
            params = {
                "search_query": f"au:{simple_query}",
                "start": 0,
                "max_results": 20,  # Get more results for author search
            }
            
            session = get_session()
            url = f"{self.base_url}?" + urllib.parse.urlencode(params)
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            
            if response.status_code == 200:
                parsed = feedparser.parse(response.text)
                results = list(parsed.get("entries", []))
                
                if results:
                    logger.debug(f"Found {len(results)} results for author '{last_name}', checking for title matches...")
                    return self._process_search_results(title, authors, results)
            
        except Exception as e:
            logger.debug(f"Author-based search failed: {e}")
        
        return None
    
    def _try_web_based_search(self, title: str, authors: List[str]) -> Optional[Dict[str, str]]:
        """
        Last resort: Try web-based search using search engines or direct arXiv search.
        
        This method uses web scraping techniques to find arXiv papers when the API fails.
        """
        logger.debug("🔍 Trying web-based search (last resort)...")
        
        # Extract key terms from title for web search
        title_words = self.extract_title_words(title)
        if len(title_words) < 2:
            return None
        
        # Use top 3-4 most distinctive words
        search_terms = title_words[:4]
        
        # Try arXiv search page directly
        try:
            # Construct arXiv search URL (their web interface)
            search_query = "+".join(search_terms)
            search_url = f"https://arxiv.org/search/?query={search_query}&searchtype=all"
            
            session = get_session()
            response = session.get(search_url, timeout=REQUEST_TIMEOUT * 2)  # Longer timeout for web scraping
            
            if response.status_code == 200:
                # Parse the HTML response to look for matching papers
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(response.content, 'html.parser')
                
                # Look for paper links in search results
                paper_links = soup.find_all('a', href=re.compile(r'/abs/\d{4}\.\d{4}'))
                
                for link in paper_links[:10]:  # Check first 10 results
                    arxiv_url = f"https://arxiv.org{link['href']}"
                    
                    # Extract arXiv ID from URL
                    arxiv_id_match = re.search(r'/abs/(\d{4}\.\d{4,5})', link['href'])
                    if arxiv_id_match:
                        arxiv_id = arxiv_id_match.group(1)
                        
                        # Get paper details
                        try:
                            paper_details = self._get_paper_details_by_web_scraping(arxiv_url)
                            if paper_details:
                                # Check if this paper matches our criteria
                                web_title = paper_details.get("title", "")
                                web_authors = paper_details.get("authors", [])
                                
                                if web_title and web_authors:
                                    title_similarity = self.calculate_title_similarity(title, web_title)
                                    author_match = self.verify_author_match(authors, web_authors, check_first_two_only=True)
                                    
                                    if title_similarity >= TITLE_SIMILARITY_THRESHOLD and author_match:
                                        logger.info(f"🌐 Found match via web search: {arxiv_url}")
                                        return {
                                            "summary": paper_details.get("summary", ""),
                                            "link": arxiv_url.replace("https://", "http://")  # Standardize to http
                                        }
                        except Exception as e:
                            logger.debug(f"Failed to scrape {arxiv_url}: {e}")
                            continue
            
        except Exception as e:
            logger.debug(f"Web-based search failed: {e}")
        
        return None
    
    def _get_paper_details_by_id(self, arxiv_id: str) -> Optional[Dict[str, Any]]:
        """
        Get paper details for a specific arXiv ID using direct API call.
        
        Args:
            arxiv_id: arXiv identifier (e.g., "2408.11220")
            
        Returns:
            Dictionary with paper details or None if failed
        """
        try:
            params = {
                "id_list": arxiv_id,
                "max_results": 1,
            }
            
            session = get_session()
            url = f"{self.base_url}?" + urllib.parse.urlencode(params)
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            
            if response.status_code == 200:
                parsed = feedparser.parse(response.text)
                entries = list(parsed.get("entries", []))
                
                if entries:
                    entry = entries[0]
                    return {
                        "title": entry.get("title", "").strip(),
                        "summary": self.extract_arxiv_summary(entry),
                        "authors": self.extract_arxiv_authors(entry),
                        "link": self.get_arxiv_link(entry)
                    }
            
        except Exception as e:
            logger.debug(f"Direct ID lookup failed for {arxiv_id}: {e}")
        
        return None
    
    def _get_paper_details_by_web_scraping(self, arxiv_url: str) -> Optional[Dict[str, Any]]:
        """
        Get paper details by scraping the arXiv web page directly.
        
        Args:
            arxiv_url: Full URL to arXiv paper page
            
        Returns:
            Dictionary with paper details or None if failed
        """
        try:
            session = get_session()
            response = session.get(arxiv_url, timeout=REQUEST_TIMEOUT * 2)
            
            if response.status_code == 200:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(response.content, 'html.parser')
                
                # Extract title
                title_elem = soup.find('h1', class_='title mathjax')
                title = ""
                if title_elem:
                    title = title_elem.get_text().replace('Title:', '').strip()
                
                # Extract authors
                authors = []
                authors_elem = soup.find('div', class_='authors')
                if authors_elem:
                    author_links = authors_elem.find_all('a')
                    authors = [link.get_text().strip() for link in author_links]
                
                # Extract abstract
                abstract = ""
                abstract_elem = soup.find('blockquote', class_='abstract mathjax')
                if abstract_elem:
                    abstract = abstract_elem.get_text().replace('Abstract:', '').strip()
                
                if title:  # Only return if we at least got a title
                    return {
                        "title": title,
                        "summary": abstract,
                        "authors": authors,
                        "link": arxiv_url
                    }
            
        except Exception as e:
            logger.debug(f"Web scraping failed for {arxiv_url}: {e}")
        
        return None


# =============================================================================
# FEED PROCESSING FUNCTIONS
# =============================================================================

def process_single_feed(feed_info: Dict[str, str], keyword_groups: Dict[str, List[str]]) -> List[FeedEntry]:
    """
    Process a single RSS feed and return matching articles.
    
    This function is designed to be called in parallel for multiple feeds.
    It handles all aspects of processing a single feed: fetching, parsing,
    filtering, and error handling.
    
    Args:
        feed_info: Dictionary containing "url" and "description" keys
        keyword_groups: Dictionary mapping group names to lists of required keywords
        
    Returns:
        List of FeedEntry objects that matched keyword groups (empty on failure)
    """
    feed_url = feed_info["url"]
    feed_description = feed_info["description"]
    
    try:
        logger.debug(f"🔄 Starting processing for feed: {feed_description}")
        processor = PRLFeedProcessor(feed_url, feed_description)
        entries = processor.process_feed(keyword_groups)
        logger.info(f"✅ Completed {feed_description}: {len(entries)} matching articles")
        return entries
    except Exception as e:
        logger.error(f"❌ Failed to process feed {feed_description}: {e}")
        return []  # Return empty list to allow other feeds to continue processing


def enrich_single_entry_with_arxiv(entry: FeedEntry, max_retries: int = 2) -> FeedEntry:
    """
    Enrich a single article entry with arXiv data if a match is found.
    
    This function searches arXiv for articles matching the given entry and
    enriches the entry with arXiv summary and link when a high-confidence
    match is found based on title similarity and author verification.
    
    Args:
        entry: FeedEntry object to enrich
        max_retries: Maximum number of retry attempts for failed enrichments
        
    Returns:
        Enriched FeedEntry with arXiv data, or original entry if no match found
    """
    arxiv_matcher = ArxivMatcher()
    # Helper to consistently truncate titles for logging
    title_preview = f"{entry.title[:60]}..." if len(entry.title) > 60 else entry.title
    
    for attempt in range(max_retries + 1):
        try:
            logger.debug(f"🔍 Searching arXiv for: {title_preview} (attempt {attempt + 1})")
            
            # Add small delay between retries to avoid API conflicts
            if attempt > 0:
                retry_delay = ARXIV_REQUEST_DELAY * (2 ** attempt)  # Exponential backoff
                logger.debug(f"⏱️ Retry delay: {retry_delay:.2f}s")
                time.sleep(retry_delay)
            
            # Attempt to find matching arXiv article using title and author matching
            arxiv_match = arxiv_matcher.find_matching_article(entry.title, entry.authors)
            
            if arxiv_match:
                # Create enriched entry with arXiv data while preserving original metadata
                enriched_entry = FeedEntry(
                    title=entry.title,
                    authors=entry.authors,
                    link=entry.link,
                    doi=entry.doi,
                    published=entry.published,
                    summary=arxiv_match["summary"],  # Replace with richer arXiv summary
                    arxiv=arxiv_match["link"],       # Add arXiv preprint link
                    keywords=entry.keywords,         # Preserve matched keywords
                    source_feed=entry.source_feed    # Preserve original feed source
                )
                logger.info(f"🔖 Enriched with arXiv data: {title_preview}")
                return enriched_entry
            else:
                # Keep original entry when no suitable arXiv match is found
                logger.debug(f"❌ No arXiv match found: {title_preview}")
                return entry
        
        except Exception as e:
            if attempt < max_retries:
                logger.warning(f"⚠️ Attempt {attempt + 1} failed for '{title_preview}': {e}")
                logger.info(f"🔄 Retrying enrichment (attempt {attempt + 2}/{max_retries + 1})...")
                continue
            else:
                logger.error(f"❌ All {max_retries + 1} attempts failed for '{title_preview}': {e}")
                return entry  # Return original entry on final failure to prevent data loss
    
    return entry  # Fallback (should not reach here)


def enrich_with_arxiv(entries: List[FeedEntry]) -> List[FeedEntry]:
    """
    Enrich article entries with arXiv data using optimized parallel processing.
    
    This function searches arXiv for matching articles and enriches the entries
    with arXiv summaries and links when matches are found. Processing is done
    in parallel with adaptive batch optimization for better performance while respecting API limits.
    
    Args:
        entries: List of FeedEntry objects to enrich
        
    Returns:
        List of enriched FeedEntry objects (same order as input)
        
    Note:
        Uses adaptive ThreadPoolExecutor with dynamic batch sizing based on success rates.
        Failed enrichments gracefully fall back to original entry data.
    """
    if not entries:
        logger.debug("No entries provided for arXiv enrichment")
        return []
    
    logger.info(f"🔍 Starting optimized arXiv enrichment for {len(entries)} articles...")
    
    # Pre-warm cache for likely matches to improve success rate
    if len(entries) > 50:  # Only for large batches where this helps
        arxiv_matcher = ArxivMatcher()
        arxiv_matcher.warm_cache_for_likely_matches(entries)
    
    # Start with optimal batch size but allow adaptive sizing
    initial_batch_size = min(ARXIV_BATCH_SIZE, len(entries))
    current_batch_size = initial_batch_size
    
    enriched_entries = []
    total_batches = (len(entries) + current_batch_size - 1) // current_batch_size
    
    logger.info(f"⚡ Using adaptive batching: starting with {current_batch_size} articles per batch")
    
    batch_num = 0
    for i in range(0, len(entries), current_batch_size):
        batch_num += 1
        batch_entries = entries[i:i + current_batch_size]
        batch_start_time = time.time()
        logger.info(f"📦 📦 📦 Processing batch {batch_num} ({len(batch_entries)} articles) 📦 📦 📦")
        
        # Use more workers for larger batches, fewer for smaller batches
        max_workers = min(len(batch_entries), MAX_ARXIV_WORKERS)
        
        # Reduce workers if we've had API issues
        rate_limit_count = get_rate_limit_count()
        if rate_limit_count > 0:
            max_workers = min(max_workers, 10)  # Be more conservative
            logger.warning(f"⚠️ Reducing workers to {max_workers} due to {rate_limit_count} rate limit events")
        
        logger.debug(f"⚡ Using {max_workers} parallel workers for batch {batch_num}")
        
        batch_enriched = []
        successful_enrichments = 0
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all enrichment tasks for this batch
            future_to_entry = {
                executor.submit(enrich_single_entry_with_arxiv, entry): entry
                for entry in batch_entries
            }
            
            # Collect results as they complete
            for future in as_completed(future_to_entry):
                entry = future_to_entry[future]
                try:
                    enriched_entry = future.result()
                    batch_enriched.append(enriched_entry)
                    
                    # Track successful enrichments (entries that got arXiv data)
                    if enriched_entry.arxiv:
                        successful_enrichments += 1
                        
                except Exception as e:
                    logger.error(f"Entry '{entry.title[:60]}...' generated an exception: {e}")
                    # Add the original entry if enrichment fails
                    batch_enriched.append(entry)
        
        # Sort batch to maintain original order within the batch
        title_to_index = {entry.title: idx for idx, entry in enumerate(batch_entries)}
        batch_enriched.sort(key=lambda x: title_to_index.get(x.title, 999))
        
        enriched_entries.extend(batch_enriched)
        batch_time = time.time() - batch_start_time
        avg_time_per_article = batch_time / len(batch_entries)
        success_rate = (successful_enrichments / len(batch_entries)) * 100
        
        logger.info(f"✅ ✅ ✅ Completed batch {batch_num} in {batch_time:.1f}s ({avg_time_per_article:.2f}s/article, {success_rate:.1f}% enriched) ✅ ✅ ✅")
        
        # Adaptive batch sizing based on success rate and timing
        if success_rate < 20 and current_batch_size > 20:
            # Low success rate - reduce batch size
            current_batch_size = max(20, current_batch_size // 2)
            logger.info(f"📉 Low success rate ({success_rate:.1f}%) - reducing batch size to {current_batch_size}")
        elif success_rate > 80 and batch_time < 3.0 and current_batch_size < initial_batch_size:
            # High success rate and fast processing - increase batch size
            current_batch_size = min(initial_batch_size, current_batch_size * 2)
            logger.info(f"📈 High success rate ({success_rate:.1f}%) - increasing batch size to {current_batch_size}")
        
        # Recalculate remaining batches
        remaining_entries = len(entries) - len(enriched_entries)
        if remaining_entries > 0:
            remaining_batches = (remaining_entries + current_batch_size - 1) // current_batch_size
            logger.debug(f"📊 {remaining_entries} entries remaining in ~{remaining_batches} batches")
    
    total_enriched = sum(1 for entry in enriched_entries if entry.arxiv)
    overall_success_rate = (total_enriched / len(enriched_entries)) * 100
    logger.info(f"🎉 Completed arXiv enrichment for all {len(enriched_entries)} articles ({total_enriched} enriched, {overall_success_rate:.1f}% success rate)")
    return enriched_entries 