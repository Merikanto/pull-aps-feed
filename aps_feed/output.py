"""
Output generation for the Physics Journals Feed Processor.

This module handles writing processed article data to YAML and Markdown files
with proper formatting and organization.
"""

import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .models import FeedEntry
from .utils import get_feed_descriptions

# Setup logger
logger = logging.getLogger(__name__)


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


def write_markdown_file(
    entries: List[FeedEntry], 
    output_path: Path, 
    keyword_groups: Dict[str, List[str]], 
    feeds: List[Dict[str, str]], 
    feed_order: Dict[str, int]
) -> None:
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