#!/usr/bin/env python3
"""
APS Physics Journals Feed Processor

This script fetches articles from multiple American Physical Society (APS) journal RSS feeds,
filters them based on configurable keyword groups, enriches them with arXiv data, and outputs
results to both structured YAML and formatted Markdown files.

Features:
- Group-based keyword matching (all keywords in a group must be present)
- Parallel processing of multiple RSS feeds
- ArXiv article matching and enrichment
- Duplicate detection and removal
- Configurable feed sources via YAML

Input:
    physics-journals-input.yml: Configuration file with keyword groups and feed URLs

Output:
    aps_results.yml: All processed articles in YAML format
    Results/Aps_YYYYMMDD_HHMM.md: Filtered and enriched articles in Markdown format

Usage:
    poetry run python physics-journals-feed.py

TODO:
- [x] Improve Markdown formatting
- [ ] Improve group-based keyword matching
- [ ] Improve Article matching in Arxiv
"""

import logging
import re
import time
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

# =============================================================================
# CONFIGURATION CONSTANTS
# =============================================================================

# File paths
INPUT_FILE = Path("physics-journals-input.yml")  # YAML config with keyword groups and feed URLs
OUTPUT_YAML_FILE = Path("aps_results.yml")       # All processed articles (YAML format)

# Network request settings
REQUEST_TIMEOUT = 30          # HTTP request timeout in seconds
MAX_ARXIV_RESULTS = 20        # Maximum number of arXiv search results to process

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


class FeedEntry:
    """
    Represents a single article entry from an RSS feed.
    
    This class encapsulates all the data extracted from a journal RSS feed entry,
    including optional enrichment data from arXiv searches and keyword matching results.
    
    Attributes:
        title (str): Article title
        authors (List[str]): List of author names
        link (str): Direct link to the published article
        doi (str): Digital Object Identifier
        published (str): Publication date (YYYY-MM-DD format)
        summary (str): Article abstract/summary
        arxiv (Optional[str]): arXiv URL if matching article found
        keywords (List[str]): Keywords that matched during filtering
        source_feed (Optional[str]): Description of the RSS feed source
    """
    
    def __init__(
        self, 
        title: str, 
        authors: List[str], 
        link: str, 
        doi: str, 
        published: str, 
        summary: str, 
        arxiv: Optional[str] = None,
        keywords: Optional[List[str]] = None,
        source_feed: Optional[str] = None
    ) -> None:
        """
        Initialize a FeedEntry with article data.
        
        Args:
            title: Article title
            authors: List of author names
            link: Direct link to the published article
            doi: Digital Object Identifier
            published: Publication date (YYYY-MM-DD format)
            summary: Article abstract/summary
            arxiv: arXiv URL if matching article found
            keywords: Keywords that matched during filtering
            source_feed: Description of the RSS feed source
        """
        self.title = title
        self.authors = authors
        self.link = link
        self.doi = doi
        self.published = published
        self.arxiv = arxiv
        self.summary = summary
        self.keywords = keywords or []
        self.source_feed = source_feed
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert entry to dictionary for YAML serialization."""
        result = {
            "title": self.title,
            "authors": self.authors,
            "link": self.link,
            "doi": self.doi,
            "published": self.published,
        }
        
        # Add arxiv before summary if it exists
        if self.arxiv:
            result["arxiv"] = self.arxiv
        
        # Add keywords if they exist
        if self.keywords:
            result["keywords"] = self.keywords
        
        # Add source feed if it exists
        if self.source_feed:
            result["source_feed"] = self.source_feed

        result["summary"] = self.summary
        
        return result
    
    def to_markdown_table(self, article_number: int) -> str:
        """Convert entry to markdown table format."""
        authors_str = ", ".join(self.authors) if self.authors else "-"
        arxiv_link = f"{self.arxiv}" if self.arxiv else "-"
        prl_link = f"{self.link}" if self.link else "-"
        
        # Highlight matched keywords in title and summary
        highlighted_title = highlight_keywords(self.title, self.keywords)
        highlighted_summary = highlight_keywords(self.summary, self.keywords)
        
        # Escape pipe characters in content for markdown table compatibility
        title = highlighted_title.replace("|", "\\|")
        authors_str = authors_str.replace("|", "\\|")
        summary = highlighted_summary.replace("|", "\\|")
        
        # Add 🔖 emoji if arXiv link is present
        article_header = f"Article {article_number}"
        if self.arxiv:
            article_header += "  🔖"
        
        keywords_str = ", ".join(self.keywords) if self.keywords else "-"
        
        # Use stored source feed description
        topic_str = self.source_feed if self.source_feed else "-"
        
        return f"""\n\n\n### {article_header}

| Field | Value |
|-------|-------|
| **Keywords** | **{keywords_str}** |
| **Title** | {title} |
| **Topic** | *{topic_str}* |
| **Authors** | {authors_str} |
| **APS Link** | {prl_link} |
| **DOI** | {self.doi} |
| **Published Date** | {self.published} |
| **arXiv Link** | {arxiv_link} |
| **Summary** | {summary} |

"""


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
            
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
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
# OUTPUT FILE GENERATION FUNCTIONS
# =============================================================================

def write_yaml_file(entries: List[FeedEntry], output_path: Path) -> None:
    """
    Write article entries to structured YAML output file.
    
    This function converts all entries to dictionary format and writes them
    to a YAML file with proper formatting and Unicode support. The YAML output
    contains all processed articles regardless of keyword filtering.
    
    Args:
        entries: List of FeedEntry objects to write
        output_path: Path where YAML file should be written
        
    Raises:
        OSError: File system errors during writing
        Exception: Other unexpected errors during YAML serialization
    """
    try:
        logger.info(f"💾 Writing {len(entries)} entries to YAML file: {output_path}")
        
        # Convert entries to dictionary format for YAML serialization
        data = [entry.to_dict() for entry in entries]
        
        # Write YAML with proper formatting and encoding
        with output_path.open('w', encoding='utf-8') as f:
            yaml.safe_dump(
                data, 
                f, 
                sort_keys=False,          # Preserve field order from to_dict()
                allow_unicode=True,       # Support Unicode characters in content
                width=88,                 # Line width for better readability
                default_flow_style=False  # Use block style (more readable)
            )
        
        logger.info(f"✅ Successfully wrote {len(entries)} entries to {output_path}")
    
    except OSError as e:
        logger.error(f"❌ File system error writing {output_path}: {e}")
        raise
    except Exception as e:
        logger.error(f"❌ Unexpected error writing {output_path}: {e}")
        raise


def write_markdown_file(entries: List[FeedEntry], output_path: Path, keyword_groups: Dict[str, List[str]], feeds: List[Dict[str, str]], feed_order: Dict[str, int]) -> None:
    """
    Write filtered article entries to formatted Markdown output file.
    
    This function creates a comprehensive Markdown report with:
    - Header with generation timestamp and summary statistics
    - Feed source listings with links
    - Keyword group descriptions and matching rules
    - Articles organized by source feed with highlighted keywords
    - Emoji indicators for arXiv availability
    
    Args:
        entries: List of filtered and deduplicated FeedEntry objects
        output_path: Path where Markdown file should be written
        keyword_groups: Dictionary mapping group names to keyword lists (for documentation)
        feeds: List of feed information dictionaries (for source attribution)
        feed_order: Dictionary mapping feed descriptions to order indices
        
    Raises:
        OSError: File system errors during writing
        Exception: Other unexpected errors during file generation
    """
    try:
        logger.info(f"📝 Writing {len(entries)} entries to Markdown file: {output_path}")
        
        # Ensure output directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with output_path.open('w', encoding='utf-8') as f:
            # Write header
            f.write("# Physics Journals - Recent Articles\n\n")
            f.write(f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
            f.write(f"Total articles: {len(entries)} (from {len(feeds)} feeds)\n\n")
            
            # Write feed sources
            feed_info = get_feed_descriptions(feeds)
            for feed in feed_info:
                f.write(f"-   [{feed['description']}]({feed['url']})\n")
            
            f.write(f"\n\n\n>   Note: Each article with  🔖 means that it can be found on Arxiv.\n")
            f.write(f">   Matched keywords are ==highlighted== in titles and summaries.\n\n\n")
            
            # Format keyword groups for display
            if keyword_groups:
                group_display = []
                for group_name, keywords in keyword_groups.items():
                    group_display.append(f"{group_name} - {', '.join(keywords)}")
                
                f.write(f">   Articles must match ALL keywords in at least one group:\n")
                for group_desc in group_display:
                    f.write(f">   - {group_desc}\n")
                f.write(f"\n")
            else:
                f.write(f">   *No keyword filtering applied - all articles included*\n\n")
            
            # Group entries by source feed
            grouped_entries = defaultdict(list)
            for entry in entries:
                source = entry.source_feed if entry.source_feed else "Unknown Source"
                grouped_entries[source].append(entry)
            
            # Write entries grouped by source in original YAML order
            for source in sorted(grouped_entries.keys(), key=lambda x: feed_order.get(x, 999)):
                source_entries = grouped_entries[source]
                f.write(f"\n\n\n## {source}\n\n")
                for i, entry in enumerate(source_entries, 1):
                    f.write(entry.to_markdown_table(i))
        
        logger.info(f"✅ Successfully wrote {len(entries)} entries to {output_path}")
    
    except OSError as e:
        logger.error(f"❌ File system error writing {output_path}: {e}")
        raise
    except Exception as e:
        logger.error(f"❌ Unexpected error writing {output_path}: {e}")
        raise


def load_config() -> Dict[str, Any]:
    """
    Load keyword groups and RSS feed configurations from YAML input file.
    
    This function reads the configuration file and handles both legacy flat keyword
    lists and the new group-based keyword structure. If the input file doesn't exist,
    it creates a default configuration file.
    
    Returns:
        Dictionary containing:
        - "keywords": Dict[str, List[str]] mapping group names to keyword lists
        - "feeds": List[Dict[str, str]] containing feed URL and description pairs
        
    Raises:
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


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def get_feed_descriptions(feeds: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Extract feed URLs and their descriptions for markdown display."""
    # Since feeds already contains descriptions and URLs from YAML, just return it
    return feeds


def highlight_keywords(text: str, keywords: List[str]) -> str:
    """
    Highlight matched keywords in text using Markdown emphasis.
    
    This function wraps matched keywords with ==text== for Markdown highlighting,
    handling word variations and preserving original case. Keywords are processed
    in order of length (longest first) to avoid partial replacements.
    
    Args:
        text: Input text to process
        keywords: List of keywords to highlight
        
    Returns:
        Text with keywords wrapped in ==keyword== Markdown highlighting
        
    Example:
        highlight_keywords("Bose-Einstein condensate", ["Bose-Einstein"])
        → "==Bose-Einstein== condensate"
    """
    if not text or not keywords:
        return text
    
    highlighted_text = text
    
    # Sort keywords by length (longest first) to avoid partial replacements
    # This prevents "BEC" from interfering with "Bose-Einstein" highlighting
    sorted_keywords = sorted(keywords, key=len, reverse=True)
    
    for keyword in sorted_keywords:
        # Create flexible regex pattern to match word variations
        escaped_keyword = re.escape(keyword.lower())
        # Match at word boundary, allowing common suffixes (plural, etc.)
        pattern = re.compile(rf'\b{escaped_keyword}(s|ies|y)?\b', re.IGNORECASE)
        
        def replacement(match):
            # Preserve the original case and form found in text
            original_match = match.group(0)
            return f"=={original_match}=="
        
        highlighted_text = pattern.sub(replacement, highlighted_text)
    
    return highlighted_text


def check_keywords(title: str, summary: str, keyword_groups: Dict[str, List[str]]) -> List[str]:
    """
    Check if article content matches any complete keyword group.
    
    This function implements group-based keyword matching where an article must contain
    ALL keywords from at least one group to be considered a match. This provides more
    precise filtering than individual keyword matching.
    
    Special case: If keyword_groups is empty, returns ["​​-​"] to indicate
    that all articles should be included (no keyword filtering).
    
    Args:
        title: Article title text
        summary: Article summary/abstract text
        keyword_groups: Dictionary mapping group names to lists of required keywords
        
    Returns:
        List of all keywords that were matched (from all matching groups)
        Returns ["​-​​"] if no keyword groups are defined
        
    Example:
        If groups are {"Group1": ["quantum", "tunneling"], "Group2": ["bose", "einstein"]}
        and article contains "quantum tunneling effect", only Group1 keywords are returned.
        An article with just "quantum" would return empty list (incomplete group match).
    """
    # Handle edge case: no keyword filtering when groups are empty
    if not keyword_groups:
        logger.debug("No keyword groups defined - accepting all articles")
        return ["​-​"]  # Special marker to indicate no filtering
    
    matched_keywords = []
    # Combine title and summary for comprehensive text search (case-insensitive)
    text_to_search = f"{title} {summary}".lower()
    
    # Check each keyword group for complete matches
    for group_name, keywords in keyword_groups.items():
        logger.debug(f"Checking keyword group '{group_name}' with {len(keywords)} keywords")
        
        # Skip empty groups
        if not keywords:
            logger.debug(f"  ⚠️  Skipping empty group: '{group_name}'")
            continue
        
        # Track keywords found in this group
        group_matches = []
        all_keywords_found = True
        
        # ALL keywords in the group must be present for a match
        for keyword in keywords:
            if keyword.lower() in text_to_search:
                group_matches.append(keyword)
                logger.debug(f"  ✓ Found keyword: '{keyword}'")
            else:
                all_keywords_found = False
                logger.debug(f"  ✗ Missing keyword: '{keyword}'")
                break  # Early exit if any keyword is missing
        
        # If all keywords in this group were found, include them in results
        if all_keywords_found and group_matches:
            matched_keywords.extend(group_matches)
            logger.debug(f"  🎯 Group '{group_name}' fully matched!")
            # Continue checking other groups to allow multiple group matches
    
    return matched_keywords


def remove_duplicates_by_title(entries: List[FeedEntry]) -> List[FeedEntry]:
    """
    Remove duplicate entries based on title similarity.
    
    For articles with identical titles (case-insensitive), this function keeps
    the "best" entry based on a priority system. This is important because the
    same article may appear in multiple RSS feeds.
    
    Selection Priority:
    1. Entries with arXiv links (more complete data)
    2. Entries with more matched keywords
    3. Entries with longer summaries (more detailed)
    
    Args:
        entries: List of FeedEntry objects that may contain duplicates
        
    Returns:
        List of deduplicated FeedEntry objects with best versions retained
    """
    if not entries:
        logger.debug("No entries provided for deduplication")
        return []
    
    logger.info(f"🔍 Checking {len(entries)} entries for duplicates...")
    
    # Group entries by normalized title for duplicate detection
    title_groups = {}
    for entry in entries:
        normalized_title = entry.title.lower().strip()
        if normalized_title not in title_groups:
            title_groups[normalized_title] = []
        title_groups[normalized_title].append(entry)
    
    # Process each title group and select the best entry
    deduplicated_entries = []
    duplicates_removed = 0
    
    for title, group_entries in title_groups.items():
        if len(group_entries) == 1:
            # No duplicates for this title - add directly
            deduplicated_entries.append(group_entries[0])
        else:
            # Multiple entries with same title - select best one using priority system
            duplicates_removed += len(group_entries) - 1
            
            # Apply selection priority: arXiv link > keyword count > summary length
            best_entry = max(group_entries, key=lambda e: (
                1 if e.arxiv else 0,           # Prioritize entries with arXiv links
                len(e.keywords),               # Prioritize more keyword matches
                len(e.summary)                 # Prioritize longer/more detailed summaries
            ))
            
            deduplicated_entries.append(best_entry)
            logger.debug(f"🧹 Removed {len(group_entries)-1} duplicate(s) for: '{title[:50]}...'")
    
    # Log deduplication results
    if duplicates_removed > 0:
        logger.info(f"🧹 Removed {duplicates_removed} duplicate entries, kept {len(deduplicated_entries)} unique articles")
    else:
        logger.info("✅ No duplicate titles found")
    
    return deduplicated_entries


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
    Enrich article entries with arXiv data using parallel processing.
    
    This function searches arXiv for matching articles and enriches the entries
    with arXiv summaries and links when matches are found. Processing is done
    in parallel to optimize performance while respecting API rate limits.
    
    Args:
        entries: List of FeedEntry objects to enrich
        
    Returns:
        List of enriched FeedEntry objects (same order as input)
        
    Note:
        Uses ThreadPoolExecutor with limited workers to avoid overwhelming arXiv API.
        Failed enrichments gracefully fall back to original entry data.
    """
    if not entries:
        logger.debug("No entries provided for arXiv enrichment")
        return []
    
    logger.info(f"🔍 Starting arXiv enrichment for {len(entries)} articles...")
    enriched_entries = []
    
    # Use ThreadPoolExecutor for parallel arXiv processing
    max_workers = min(len(entries), 5)  # Limit concurrent requests to respect arXiv API
    logger.info(f"⚡ Using {max_workers} parallel workers for arXiv enrichment")
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all enrichment tasks
        future_to_entry = {
            executor.submit(enrich_single_entry_with_arxiv, entry): entry
            for entry in entries
        }
        
        # Collect results as they complete
        for future in as_completed(future_to_entry):
            entry = future_to_entry[future]
            try:
                enriched_entry = future.result()
                enriched_entries.append(enriched_entry)
            except Exception as e:
                logger.error(f"Entry '{entry.title}' generated an exception: {e}")
                # Add the original entry if enrichment fails
                enriched_entries.append(entry)
    
    # Sort to maintain original order (since parallel processing can change order)
    # Create a mapping of title to original index for sorting
    title_to_index = {entry.title: i for i, entry in enumerate(entries)}
    enriched_entries.sort(key=lambda x: title_to_index.get(x.title, 999))
    
    return enriched_entries


# =============================================================================
# MAIN EXECUTION
# =============================================================================

def main() -> None:
    """
    Main entry point for the APS physics journals feed processor.
    
    This function orchestrates the entire workflow:
    1. Loads configuration from YAML file
    2. Processes multiple RSS feeds in parallel
    3. Enriches articles with arXiv data
    4. Removes duplicates
    5. Outputs results to YAML and timestamped Markdown files
    
    The process uses parallel execution for both feed processing and arXiv enrichment
    to optimize performance when handling multiple feeds and articles.
    """
    try:
        start_time = time.time()
        logger.info("🚀 Starting APS Physics Journals Feed Processor")
        
        # Generate timestamped markdown filename to avoid overwriting previous runs
        timestamp = datetime.now().strftime('%Y%m%d_%H%M')
        OUTPUT_MARKDOWN_FILE = Path(f"Results/Aps_{timestamp}.md")
        logger.info(f"📝 Output will be saved to: {OUTPUT_MARKDOWN_FILE}")
        
        # Load configuration from YAML input file
        logger.info("📋 Loading configuration...")
        config = load_config()
        keyword_groups = config["keywords"]
        feeds = config["feeds"]
        
        # Process all RSS feeds in parallel for optimal performance
        logger.info(f"🔄 Processing {len(feeds)} RSS feeds in parallel...")
        feed_start_time = time.time()
        
        # Create mapping to maintain original feed order in output
        feed_order = {feed['description']: i for i, feed in enumerate(feeds)}
        
        # Use ThreadPoolExecutor for parallel RSS feed processing
        max_workers = min(len(feeds), 10)  # Limit concurrent connections to avoid overwhelming servers
        logger.info(f"⚡ Using {max_workers} parallel workers for feed processing")
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all feed processing tasks simultaneously
            future_to_feed = {
                executor.submit(process_single_feed, feed, keyword_groups): feed
                for feed in feeds
            }
            
            # Collect results as they complete, maintaining feed association
            feed_results = {}
            completed_feeds = 0
            for future in as_completed(future_to_feed):
                feed = future_to_feed[future]
                completed_feeds += 1
                try:
                    entries = future.result()
                    feed_results[feed['description']] = entries
                    logger.info(f"📦 Feed {completed_feeds}/{len(feeds)} completed: {feed['description']} ({len(entries)} entries)")
                except Exception as e:
                    logger.error(f"❌ Feed {feed['description']} failed: {e}")
                    feed_results[feed['description']] = []
        
        # Combine all entries in the original feed order
        all_entries = []
        for feed in feeds:
            feed_desc = feed['description']
            if feed_desc in feed_results:
                all_entries.extend(feed_results[feed_desc])
        
        # Check if any entries were found across all feeds
        if not all_entries:
            logger.warning("❌ No articles found matching keyword groups across all feeds")
            print("No entries to process")
            return
        
        feed_time = time.time() - feed_start_time
        logger.info(f"📊 Total entries from {len(feeds)} feeds: {len(all_entries)} (completed in {feed_time:.2f}s)")
        entries = all_entries
        
        # Enrich articles with arXiv data using parallel processing
        logger.info(f"🔍 Starting arXiv enrichment for {len(entries)} articles...")
        arxiv_start_time = time.time()
        enriched_entries = enrich_with_arxiv(entries)
        arxiv_time = time.time() - arxiv_start_time
        logger.info(f"✅ arXiv enrichment completed in {arxiv_time:.2f}s")
        
        # Filter entries that matched keyword groups for markdown output
        # Special case: if keyword filtering is disabled, include all entries
        if not keyword_groups:
            filtered_entries = enriched_entries
            logger.info(f"📋 No keyword filtering - including all {len(filtered_entries)} articles")
        else:
            filtered_entries = [entry for entry in enriched_entries if entry.keywords]
            logger.info(f"🎯 Filtered to {len(filtered_entries)} articles with keyword group matches")
        
        # Remove duplicate articles based on title similarity before final output
        logger.info("🔍 Removing duplicate articles from filtered results...")
        deduplicated_entries = remove_duplicates_by_title(filtered_entries)
        
        # Write results to output files
        logger.info("💾 Writing output files...")
        write_yaml_file(enriched_entries, OUTPUT_YAML_FILE)  # All enriched entries (no filtering)
        write_markdown_file(deduplicated_entries, OUTPUT_MARKDOWN_FILE, keyword_groups, feeds, feed_order)  # Filtered & deduplicated
        
        # Calculate and report final processing statistics
        arxiv_matches = sum(1 for entry in enriched_entries if entry.arxiv)
        keyword_matches = len(filtered_entries)
        final_articles = len(deduplicated_entries)
        duplicates_removed = keyword_matches - final_articles
        
        # Log completion and display comprehensive results summary
        logger.info("🎉 Processing completed successfully!")
        
        print(f"\n{'='*60}")
        print(f"🏁 PROCESSING COMPLETE - RESULTS SUMMARY")
        print(f"{'='*60}")
        print(f"🎉  Successfully processed {len(enriched_entries)} articles from {len(feeds)} RSS feeds")
        print(f"📄 Output files:")
        print(f"  • YAML: {OUTPUT_YAML_FILE} ({len(enriched_entries)} articles)")
        print(f"  • Markdown: {OUTPUT_MARKDOWN_FILE} ({final_articles} articles after deduplication)")
        print(f"🎉🎉 Found {arxiv_matches} arXiv matches out of {len(enriched_entries)} articles")
        
        # Format keyword groups for user-friendly display
        if keyword_groups:
            group_summary = []
            for group_name, keywords in keyword_groups.items():
                group_summary.append(f"{group_name}=[{', '.join(keywords)}]")
            
            print(f"🎯 Found {keyword_matches} articles matching keyword groups:")
            print(f"    {' OR '.join(group_summary)}")
        else:
            print(f"📋 Keyword filtering disabled - included all {keyword_matches} articles")
        
        if duplicates_removed > 0:
            print(f"🧹 Removed {duplicates_removed} duplicate articles from markdown output")
        
        # Display performance metrics
        total_time = time.time() - start_time
        print(f"\n⏱️  Performance Summary:")
        print(f"  • Total execution time: {total_time:.2f}s")
        print(f"  • Feed processing: {feed_time:.2f}s ({len(feeds)} feeds in parallel)")
        print(f"  • arXiv enrichment: {arxiv_time:.2f}s ({len(entries)} articles in parallel)")
        print(f"{'='*60}")
    
    except Exception as e:
        logger.error(f"💥 Application failed with unexpected error: {e}")
        logger.error("Check logs above for detailed error information")
        raise


if __name__ == "__main__":
    main()