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

from .config import (ARXIV_BATCH_SIZE, MAX_ARXIV_RESULTS, MAX_ARXIV_WORKERS,
                     MAX_SEARCH_WORDS, MIN_WORD_LENGTH, REQUEST_TIMEOUT,
                     TITLE_SIMILARITY_THRESHOLD, get_session)
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
    
    Attributes:
        base_url (str): arXiv API query endpoint URL
    """
    
    def __init__(self) -> None:
        """Initialize arXiv matcher with API configuration."""
        self.base_url = "http://export.arxiv.org/api/query"
    
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
        # Handle hyphenated words
        if '-' in word:
            # Convert "ultra-narrow" to "ultranarrow" for matching
            word = word.replace('-', '')
        return word
    
    def calculate_title_similarity(self, title1: str, title2: str) -> float:
        """Calculate similarity between two titles based on alphabetic word overlap."""
        words1_raw = self.extract_title_words(title1)
        words2_raw = self.extract_title_words(title2)
        
        # Normalize words for better matching
        words1 = set(self.normalize_word(word) for word in words1_raw)
        words2 = set(self.normalize_word(word) for word in words2_raw)
        
        if not words1 or not words2:
            return 0.0
        
        # Calculate intersection over minimum (more lenient than Jaccard)
        # This helps when one title has more descriptive words than the other
        intersection = len(words1.intersection(words2))
        min_length = min(len(words1), len(words2))
        
        return intersection / min_length if min_length > 0 else 0.0
    
    def search_arxiv(self, title: str, max_results: int = MAX_ARXIV_RESULTS) -> List[Dict[str, Any]]:
        """Search arXiv for articles matching the title."""
        try:
            # Extract key words from the title for search
            title_words = self.extract_title_words(title)
            
            if not title_words:
                logger.warning(f"No meaningful words extracted from title: '{title}'")
                return []
            
            # Use key words for search (take first N most distinctive words)
            search_words = title_words[:MAX_SEARCH_WORDS]
            search_query = ' '.join(search_words)
            
            params = {
                "search_query": f'ti:{search_query}',
                "start": 0,
                "max_results": max_results,
            }
            
            url = f"{self.base_url}?" + urllib.parse.urlencode(params)
            logger.debug(f"Searching arXiv with keywords: '{search_query}'")
            
            # Use optimized session for better performance
            session = get_session()
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            
            parsed = feedparser.parse(response.text)
            results = list(parsed.get("entries", []))
            
            return results
        
        except requests.RequestException as e:
            logger.warning(f"Network error during arXiv search for '{title}': {e}")
            return []
        except Exception as e:
            logger.warning(f"Unexpected error during arXiv search for '{title}': {e}")
            return []
    
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
        """Check if first/second PRL author appears in arXiv authors."""
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
                
                # Extract last names for better matching
                prl_last = prl_author.split()[-1].lower() if prl_author.split() else ""
                arxiv_last = arxiv_author.split()[-1].lower() if arxiv_author.split() else ""
                
                # Match if:
                # 1. Full name matches (in either direction)
                # 2. Last names match
                if (prl_author.lower() in arxiv_author.lower() or 
                    arxiv_author.lower() in prl_author.lower() or
                    (prl_last and arxiv_last and prl_last == arxiv_last)):
                    logger.debug(f"Author match found: '{prl_author}' ~ '{arxiv_author}'")
                    return True
        
        return False
    
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
        """Find matching arXiv article for given title and authors using flexible matching."""
        logger.info(f"Searching arXiv for: '{title}'")
        
        arxiv_results = self.search_arxiv(title)
        if not arxiv_results:
            logger.info(f"No arXiv results found for '{title}'")
            return None
        
        logger.debug(f"Found {len(arxiv_results)} potential matches, checking title similarity and authors...")
        
        best_match = None
        best_score = 0.0
        
        for result in arxiv_results:
            arxiv_title = result.get("title", "").strip()
            arxiv_authors = self.extract_arxiv_authors(result)
            
            # Calculate title similarity
            title_similarity = self.calculate_title_similarity(title, arxiv_title)
            
            # Check if first or second author matches
            author_match = self.verify_author_match(authors, arxiv_authors, check_first_two_only=True)
            
            logger.info(f"Title similarity: {title_similarity:.2f}, Author match: {author_match}")
            logger.info(f"  PRL: '{title}'")
            logger.info(f"  arXiv: '{arxiv_title}'")
            logger.info(f"  PRL authors: {authors[:2]}")
            logger.info(f"  arXiv authors: {arxiv_authors[:3]}...")
            
            # Debug word extraction for specific case
            if "metallic and insulating domains" in title.lower():
                prl_words = self.extract_title_words(title)
                arxiv_words = self.extract_title_words(arxiv_title)
                logger.info(f"  DEBUG PRL words: {prl_words}")
                logger.info(f"  DEBUG arXiv words: {arxiv_words}")
            
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
        
        logger.info(f"No suitable match found (need {TITLE_SIMILARITY_THRESHOLD*100:.0f}%+ alphabetic word similarity + first/second author match)")
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


def enrich_single_entry_with_arxiv(entry: FeedEntry) -> FeedEntry:
    """
    Enrich a single article entry with arXiv data if a match is found.
    
    This function searches arXiv for articles matching the given entry and
    enriches the entry with arXiv summary and link when a high-confidence
    match is found based on title similarity and author verification.
    
    Args:
        entry: FeedEntry object to enrich
        
    Returns:
        Enriched FeedEntry with arXiv data, or original entry if no match found
    """
    arxiv_matcher = ArxivMatcher()
    # Helper to consistently truncate titles for logging
    title_preview = f"{entry.title[:60]}..." if len(entry.title) > 60 else entry.title
    
    try:
        logger.debug(f"🔍 Searching arXiv for: {title_preview}")
        
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
        logger.error(f"❌ Failed to enrich entry '{title_preview}': {e}")
        return entry  # Return original entry on error to prevent data loss


def enrich_with_arxiv(entries: List[FeedEntry]) -> List[FeedEntry]:
    """
    Enrich article entries with arXiv data using optimized parallel processing.
    
    This function searches arXiv for matching articles and enriches the entries
    with arXiv summaries and links when matches are found. Processing is done
    in parallel with batch optimization for better performance while respecting API limits.
    
    Args:
        entries: List of FeedEntry objects to enrich
        
    Returns:
        List of enriched FeedEntry objects (same order as input)
        
    Note:
        Uses optimized ThreadPoolExecutor with batch processing and connection pooling.
        Failed enrichments gracefully fall back to original entry data.
    """
    if not entries:
        logger.debug("No entries provided for arXiv enrichment")
        return []
    
    logger.info(f"🔍 Starting optimized arXiv enrichment for {len(entries)} articles...")
    
    # Process in batches for memory efficiency and better performance
    enriched_entries = []
    total_batches = (len(entries) + ARXIV_BATCH_SIZE - 1) // ARXIV_BATCH_SIZE
    
    for batch_num, i in enumerate(range(0, len(entries), ARXIV_BATCH_SIZE), 1):
        batch_entries = entries[i:i + ARXIV_BATCH_SIZE]
        batch_start_time = time.time()
        logger.info(f"📦 Processing batch {batch_num}/{total_batches} ({len(batch_entries)} articles)")
        
        # Use ThreadPoolExecutor for parallel arXiv processing within batch
        max_workers = min(len(batch_entries), MAX_ARXIV_WORKERS)
        logger.debug(f"⚡ Using {max_workers} parallel workers for batch {batch_num}")
        
        batch_enriched = []
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
                except Exception as e:
                    logger.error(f"Entry '{entry.title[:60]}...' generated an exception: {e}")
                    # Add the original entry if enrichment fails
                    batch_enriched.append(entry)
        
        # Sort batch to maintain original order within the batch
        title_to_index = {entry.title: idx for idx, entry in enumerate(batch_entries)}
        batch_enriched.sort(key=lambda x: title_to_index.get(x.title, 999))
        
        enriched_entries.extend(batch_enriched)
        batch_time = time.time() - batch_start_time
        logger.info(f"✅ Completed batch {batch_num}/{total_batches} in {batch_time:.1f}s")
    
    logger.info(f"🎉 Completed arXiv enrichment for all {len(enriched_entries)} articles")
    return enriched_entries 