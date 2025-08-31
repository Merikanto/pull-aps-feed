"""
Data models for the Physics Journals Feed Processor.

This module contains the core data structures used throughout the application.
"""

from typing import Any, Dict, List, Optional

from .utils import highlight_keywords


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