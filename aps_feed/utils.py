"""
Utility functions for the Physics Journals Feed Processor.

This module contains helper functions for keyword processing, text highlighting,
deduplication, and other utility operations.
"""

import logging
import re
from typing import Dict, List

# Setup logger
logger = logging.getLogger(__name__)


def check_keywords(title: str, summary: str, keyword_groups: Dict[str, List[str]]) -> List[str]:
    """
    Check if article content matches any complete keyword group.
    
    This function implements group-based keyword matching where an article must contain
    ALL keywords from at least one group to be considered a match. This provides more
    precise filtering than individual keyword matching.
    
    Special case: If keyword_groups is empty, returns ["​-​"] to indicate
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


def remove_duplicates_by_title(entries: List['FeedEntry']) -> List['FeedEntry']:
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


def get_feed_descriptions(feeds: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Extract feed URLs and their descriptions for markdown display.
    
    Args:
        feeds: List of feed information dictionaries
        
    Returns:
        List of feed dictionaries with description and URL keys
    """
    # Since feeds already contains descriptions and URLs from YAML, just return it
    return feeds 