#!/usr/bin/env python3
"""
Fetch the PRL RSS feed and write all items to YAML and Markdown files.

This script fetches articles from the Physical Review Letters RSS feed,
extracts relevant information, and outputs it to both structured YAML and
formatted Markdown files.

Usage:
    cd scripts/x-sci
    poetry run python physics-journals-feed.py

TODO:
- [x] Improve Markdown formatting
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

# Configuration
INPUT_FILE = Path("physics-journals-input.yml")
OUTPUT_YAML_FILE = Path("aps_results.yml")
OUTPUT_MARKDOWN_FILE = Path("aps_results.md")

# Request timeout and retry settings
REQUEST_TIMEOUT = 30
MAX_ARXIV_RESULTS = 20

# Matching thresholds
TITLE_SIMILARITY_THRESHOLD = 0.9
MIN_WORD_LENGTH = 3

# Search configuration
MAX_SEARCH_WORDS = 6

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class FeedEntry:
    """Represents a single article entry from the RSS feed."""
    
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
| **Topic** | {topic_str} |
| **Authors** | {authors_str} |
| **APS Link** | {prl_link} |
| **DOI** | {self.doi} |
| **Published Date** | {self.published} |
| **arXiv Link** | {arxiv_link} |
| **Summary** | {summary} |

"""


class PRLFeedProcessor:
    """Processes Physical Review Letters RSS feed."""
    
    def __init__(self, feed_url: str, feed_description: str) -> None:
        self.feed_url = feed_url
        self.feed_description = feed_description
    
    def fetch_entries(self) -> List[Dict[str, Any]]:
        """Fetch and parse RSS feed entries."""
        try:
            logger.info(f"Fetching feed from {self.feed_url}")
            parsed_feed = feedparser.parse(self.feed_url)
            
            if parsed_feed.bozo:
                logger.warning("Feed parser detected malformed XML")
            
            return list(parsed_feed.get("entries", []))
        
        except requests.RequestException as e:
            logger.error(f"Network error while fetching feed: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error while fetching feed: {e}")
            raise
    
    def extract_authors(self, entry: Dict[str, Any]) -> List[str]:
        """Extract and clean author names from feed entry."""
        authors = []
        
        # Try structured authors field first
        if entry.get("authors"):
            for author in entry.get("authors", []):
                name = author.get("name", "").strip()
                if name:
                    authors.extend(self._split_author_string(name))
        
        # Fallback to single author field
        elif entry.get("author"):
            author_str = entry.get("author", "").strip()
            authors.extend(self._split_author_string(author_str))
        
        return [author for author in authors if author]
    
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
    
    def process_entry(self, entry: Dict[str, Any], filter_keywords: List[str]) -> Optional[FeedEntry]:
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
            keywords = check_keywords(title, summary, filter_keywords)
            
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
    
    def process_feed(self, filter_keywords: List[str]) -> List[FeedEntry]:
        """Process entire feed and return list of FeedEntry objects."""
        raw_entries = self.fetch_entries()
        processed_entries = []
        
        logger.info(f"📌 📌 Processing {len(raw_entries)} entries")
        
        for raw_entry in raw_entries:
            processed_entry = self.process_entry(raw_entry, filter_keywords)
            if processed_entry:
                processed_entries.append(processed_entry)
        
        logger.info(f"Successfully processed {len(processed_entries)} entries")
        return processed_entries


class ArxivMatcher:
    """
    Handles arXiv article matching and enrichment.
    
    This class provides functionality to search arXiv for articles that match
    PRL entries based on title similarity and author matching.
    """
    
    def __init__(self) -> None:
        self.base_url = "http://export.arxiv.org/api/query"
    
    def extract_title_words(self, title: str) -> List[str]:
        """Extract meaningful alphabetic words from title for matching."""
        # Remove all mathematical notation, special characters, and numbers
        # Keep only alphabetic characters and spaces
        clean_title = re.sub(r'[\$\{\}\[\]\\^_{}]', '', title)  # Remove math symbols
        clean_title = re.sub(r'\b\d+\b', '', clean_title)  # Remove standalone numbers
        clean_title = re.sub(r'[^a-zA-Z\s\-]', ' ', clean_title)  # Keep only letters, spaces, hyphens
        clean_title = re.sub(r'\s+', ' ', clean_title).strip().lower()
        
        # Handle common scientific abbreviations - expand them for better matching
        # CDW = Charge Density Wave
        clean_title = re.sub(r'\bcdw\b', 'charge density wave', clean_title)
        # QHE = Quantum Hall Effect
        clean_title = re.sub(r'\bqhe\b', 'quantum hall effect', clean_title)
        # AFM = Antiferromagnetic, FM = Ferromagnetic
        clean_title = re.sub(r'\bafm\b', 'antiferromagnetic', clean_title)
        clean_title = re.sub(r'\bfm\b', 'ferromagnetic', clean_title)
        
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


def write_yaml_file(entries: List[FeedEntry], output_path: Path) -> None:
    """Write entries to YAML file."""
    try:
        data = [entry.to_dict() for entry in entries]
        
        with output_path.open('w', encoding='utf-8') as f:
            yaml.safe_dump(
                data, 
                f, 
                sort_keys=False, 
                allow_unicode=True, 
                width=88,
                default_flow_style=False
            )
        
        logger.info(f"Wrote {len(entries)} entries to {output_path}")
    
    except OSError as e:
        logger.error(f"File system error writing YAML file: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error writing YAML file: {e}")
        raise


def write_markdown_file(entries: List[FeedEntry], output_path: Path, filter_keywords: List[str], feeds: List[Dict[str, str]], feed_order: Dict[str, int]) -> None:
    """Write entries to Markdown file with table format."""
    try:
        with output_path.open('w', encoding='utf-8') as f:
            # Write header
            f.write("# Physics Journals - Recent Articles\n\n")
            f.write(f"Generated on: {datetime.now().strftime('%Y-%m-%d')}\n\n")
            f.write(f"Total articles: {len(entries)} (from {len(feeds)} feeds)\n\n")
            
            # Write feed sources
            feed_info = get_feed_descriptions(feeds)
            for feed in feed_info:
                f.write(f"-   [{feed['description']}]({feed['url']})\n")
            
            f.write(f"\n\n\n>   Note: Each article with  🔖 means that it can be found on Arxiv.\n")
            f.write(f">   Matched keywords are ==highlighted== in titles and summaries.\n")
            f.write(f">   Only articles matching keywords: [=={', '.join(filter_keywords)}==] are included.\n\n")
            
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
        
        logger.info(f"Wrote {len(entries)} entries to {output_path}")
    
    except OSError as e:
        logger.error(f"File system error writing Markdown file: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error writing Markdown file: {e}")
        raise


def load_config() -> Dict[str, Any]:
    """Load filter keywords and feed configurations from YAML file."""
    try:
        if not INPUT_FILE.exists():
            logger.warning(f"Input file {INPUT_FILE} not found. Creating default file.")
            # Create default config file
            default_config = {
                "Keywords": [
                    "Symmetry",
                    "Hermitian", 
                    "Bose-Einstein",
                    "BEC"
                ],
                "Feed-URL": {
                    "Recently published in Reviews of Modern Physics": [
                        "https://feeds.aps.org/rss/recent/rmp.xml"
                    ],
                    "PRL - Recently published": [
                        "https://feeds.aps.org/rss/recent/prl.xml"
                    ]
                }
            }
            with INPUT_FILE.open('w', encoding='utf-8') as f:
                yaml.safe_dump(default_config, f, sort_keys=False, allow_unicode=True)
            
            # Convert to feed info format
            feed_info = []
            for description, urls in default_config["Feed-URL"].items():
                for url in urls:
                    feed_info.append({"description": description, "url": url})
            
            return {
                "keywords": default_config["Keywords"],
                "feeds": feed_info
            }
        
        with INPUT_FILE.open('r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
            keywords = data.get("Keywords", [])
            feed_url_data = data.get("Feed-URL", {})
            
            # Convert Feed-URL structure to list of feed info
            feed_info = []
            for description, urls in feed_url_data.items():
                if isinstance(urls, list):
                    for url in urls:
                        feed_info.append({"description": description, "url": url})
                else:
                    # Handle single URL case
                    feed_info.append({"description": description, "url": urls})
            
            logger.info(f"Loaded {len(keywords)} keywords from {INPUT_FILE}: {', '.join(keywords)}")
            logger.info(f"Loaded {len(feed_info)} feeds from {INPUT_FILE}")
            
            return {
                "keywords": keywords,
                "feeds": feed_info
            }
    
    except Exception as e:
        logger.error(f"Error loading config from {INPUT_FILE}: {e}")
        # Return default config on error
        default_keywords = ["Symmetry", "Hermitian", "Bose-Einstein", "BEC"]
        default_feeds = [
            {"description": "Recently published in Reviews of Modern Physics", "url": "https://feeds.aps.org/rss/recent/rmp.xml"},
            {"description": "PRL - Recently published", "url": "https://feeds.aps.org/rss/recent/prl.xml"}
        ]
        logger.info(f"Using default keywords: {', '.join(default_keywords)}")
        logger.info(f"Using default feeds: {len(default_feeds)} feeds")
        return {
            "keywords": default_keywords,
            "feeds": default_feeds
        }


def get_feed_descriptions(feeds: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Extract feed URLs and their descriptions for markdown display."""
    # Since feeds already contains descriptions and URLs from YAML, just return it
    return feeds


def highlight_keywords(text: str, keywords: List[str]) -> str:
    """Highlight keywords in text by wrapping them with ==keyword==."""
    if not text or not keywords:
        return text
    
    highlighted_text = text
    
    # Sort keywords by length (longest first) to avoid partial replacements
    sorted_keywords = sorted(keywords, key=len, reverse=True)
    
    for keyword in sorted_keywords:
        # Use flexible pattern to match word variations (plural, etc.)
        # Create pattern that matches the keyword as part of a word
        escaped_keyword = re.escape(keyword.lower())
        # Match the keyword at word boundary, allowing for common suffixes
        pattern = re.compile(rf'\b{escaped_keyword}(s|ies|y)?\b', re.IGNORECASE)
        
        def replacement(match):
            # Preserve the original case and form found in text
            original_match = match.group(0)
            return f"=={original_match}=="
        
        highlighted_text = pattern.sub(replacement, highlighted_text)
    
    return highlighted_text


def check_keywords(title: str, summary: str, filter_keywords: List[str]) -> List[str]:
    """Check if title or summary contains any of the filter keywords (case-insensitive)."""
    matched_keywords = []
    text_to_search = f"{title} {summary}".lower()
    
    for keyword in filter_keywords:
        if keyword.lower() in text_to_search:
            matched_keywords.append(keyword)
    
    return matched_keywords


def remove_duplicates_by_title(entries: List[FeedEntry]) -> List[FeedEntry]:
    """
    Remove duplicate entries based on title similarity.
    
    For each group of entries with the same title (case-insensitive), 
    keep the "best" entry prioritizing:
    1. Entries with arXiv links
    2. Entries with more keywords
    3. Entries with longer summaries
    """
    if not entries:
        return []
    
    # Group entries by normalized title
    title_groups = {}
    for entry in entries:
        normalized_title = entry.title.lower().strip()
        if normalized_title not in title_groups:
            title_groups[normalized_title] = []
        title_groups[normalized_title].append(entry)
    
    # For each group, select the best entry
    deduplicated_entries = []
    duplicates_removed = 0
    
    for title, group_entries in title_groups.items():
        if len(group_entries) == 1:
            # No duplicates for this title
            deduplicated_entries.append(group_entries[0])
        else:
            # Multiple entries with same title - pick the best one
            duplicates_removed += len(group_entries) - 1
            
            # Sort by priority: arXiv link first, then keyword count, then summary length
            best_entry = max(group_entries, key=lambda e: (
                1 if e.arxiv else 0,           # Has arXiv link
                len(e.keywords),               # Number of keywords matched
                len(e.summary)                 # Summary length
            ))
            
            deduplicated_entries.append(best_entry)
            logger.info(f"Removed {len(group_entries)-1} duplicate(s) for title: '{title[:50]}...'")
    
    if duplicates_removed > 0:
        logger.info(f"Removed {duplicates_removed} duplicate entries, kept {len(deduplicated_entries)} unique articles")
    else:
        logger.info("No duplicate titles found")
    
    return deduplicated_entries


def process_single_feed(feed_info: Dict[str, str], filter_keywords: List[str]) -> List[FeedEntry]:
    """Process a single feed and return entries."""
    feed_url = feed_info["url"]
    feed_description = feed_info["description"]
    
    try:
        logger.info(f"Processing feed: {feed_description} ({feed_url})")
        processor = PRLFeedProcessor(feed_url, feed_description)
        entries = processor.process_feed(filter_keywords)
        logger.info(f"Added {len(entries)} entries from {feed_description}")
        return entries
    except Exception as e:
        logger.error(f"Failed to process feed {feed_description}: {e}")
        return []


def enrich_single_entry_with_arxiv(entry: FeedEntry) -> FeedEntry:
    """Enrich a single entry with arXiv data."""
    arxiv_matcher = ArxivMatcher()
    
    try:
        logger.info(f"📌 📌 Processing: {entry.title}")
        
        # Try to find matching arXiv article
        arxiv_match = arxiv_matcher.find_matching_article(entry.title, entry.authors)
        
        if arxiv_match:
            # Update entry with arXiv data
            enriched_entry = FeedEntry(
                title=entry.title,
                authors=entry.authors,
                link=entry.link,
                doi=entry.doi,
                published=entry.published,
                summary=arxiv_match["summary"],  # Use arXiv summary
                arxiv=arxiv_match["link"],       # Add arXiv link
                keywords=entry.keywords,         # Preserve keywords
                source_feed=entry.source_feed    # Preserve source feed
            )
            logger.info(f"Enriched with arXiv data: {entry.title}")
            return enriched_entry
        else:
            # Keep original entry
            logger.info(f"No arXiv match found: {entry.title}")
            return entry
    
    except Exception as e:
        logger.error(f"Failed to enrich entry '{entry.title}': {e}")
        return entry


def enrich_with_arxiv(entries: List[FeedEntry]) -> List[FeedEntry]:
    """Enrich entries with arXiv data using parallel processing."""
    if not entries:
        return []
    
    logger.info(f"Starting arXiv enrichment for {len(entries)} entries...")
    enriched_entries = []
    
    # Use ThreadPoolExecutor for parallel arXiv processing
    max_workers = min(len(entries), 5)  # Limit to avoid overwhelming arXiv API
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


def main() -> None:
    """Main function."""
    try:
        start_time = time.time()
        
        # Load config from YAML file
        config = load_config()
        filter_keywords = config["keywords"]
        feeds = config["feeds"]
        
        # Process all feeds in parallel
        logger.info(f"Processing {len(feeds)} feeds in parallel...")
        feed_start_time = time.time()
        
        # Create a mapping to maintain original feed order
        feed_order = {feed['description']: i for i, feed in enumerate(feeds)}
        
        # Use ThreadPoolExecutor for parallel processing
        max_workers = min(len(feeds), 10)  # Limit concurrent connections
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all feed processing tasks
            future_to_feed = {
                executor.submit(process_single_feed, feed, filter_keywords): feed
                for feed in feeds
            }
            
            # Collect results as they complete, maintaining feed association
            feed_results = {}
            for future in as_completed(future_to_feed):
                feed = future_to_feed[future]
                try:
                    entries = future.result()
                    feed_results[feed['description']] = entries
                except Exception as e:
                    logger.error(f"Feed {feed['description']} generated an exception: {e}")
                    feed_results[feed['description']] = []
        
        # Combine all entries in the original feed order
        all_entries = []
        for feed in feeds:
            feed_desc = feed['description']
            if feed_desc in feed_results:
                all_entries.extend(feed_results[feed_desc])
        
        if not all_entries:
            print("No entries to process")
            return
        
        feed_time = time.time() - feed_start_time
        logger.info(f"Total entries from {len(feeds)} feeds: {len(all_entries)} (completed in {feed_time:.2f}s)")
        entries = all_entries
        
        # Enrich with arXiv data (parallel processing)
        arxiv_start_time = time.time()
        enriched_entries = enrich_with_arxiv(entries)
        arxiv_time = time.time() - arxiv_start_time
        logger.info(f"arXiv enrichment completed in {arxiv_time:.2f}s")
        
        # Filter entries with matching keywords for markdown output
        filtered_entries = [entry for entry in enriched_entries if entry.keywords]
        
        # Remove duplicates from filtered entries before markdown output
        logger.info("Removing duplicates from filtered entries...")
        deduplicated_entries = remove_duplicates_by_title(filtered_entries)
        
        # Write to both YAML and Markdown files
        write_yaml_file(enriched_entries, OUTPUT_YAML_FILE)  # All entries
        write_markdown_file(deduplicated_entries, OUTPUT_MARKDOWN_FILE, filter_keywords, feeds, feed_order)  # Keyword matches, deduplicated
        
        # Report results
        arxiv_matches = sum(1 for entry in enriched_entries if entry.arxiv)
        keyword_matches = len(filtered_entries)
        final_articles = len(deduplicated_entries)
        duplicates_removed = keyword_matches - final_articles
        
        print(f"Successfully processed {len(enriched_entries)} articles from {len(feeds)} feeds")
        print(f"  - YAML output: {OUTPUT_YAML_FILE} ({len(enriched_entries)} articles)")
        print(f"  - Markdown output: {OUTPUT_MARKDOWN_FILE} ({final_articles} articles after deduplication)")
        print(f"Found {arxiv_matches} arXiv matches out of {len(enriched_entries)} articles")
        print(f"Found {keyword_matches} articles matching keywords: {', '.join(filter_keywords)}")
        if duplicates_removed > 0:
            print(f"Removed {duplicates_removed} duplicate articles from markdown output")
        
        total_time = time.time() - start_time
        print(f"\nTotal execution time: {total_time:.2f}s")
        print(f"  - Feed processing: {feed_time:.2f}s ({len(feeds)} feeds in parallel)")
        print(f"  - arXiv enrichment: {arxiv_time:.2f}s ({len(entries)} articles in parallel)")
    
    except Exception as e:
        logger.error(f"Application failed: {e}")
        raise


if __name__ == "__main__":
    main()