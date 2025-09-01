"""
Main entry point for the Physics Journals Feed Processor.

This module contains the main orchestration logic that coordinates all the
processing steps from configuration loading to final output generation.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from .config import (MAX_FEED_WORKERS, OUTPUT_YAML_FILE, get_rate_limit_count,
                     load_config)
from .output import write_markdown_file, write_yaml_file
from .processors import enrich_with_arxiv, process_single_feed
from .utils import apply_keyword_filtering, remove_duplicates_by_title

# Setup logger
logger = logging.getLogger(__name__)


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
        OUTPUT_MARKDOWN_FILE = Path(f"results/Aps_{timestamp}.md")
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
        max_workers = min(len(feeds), MAX_FEED_WORKERS)  # Optimize concurrent connections
        logger.info(f"⚡ Using {max_workers} parallel workers for feed processing")
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all feed processing tasks simultaneously (WITHOUT keyword filtering)
            future_to_feed = {
                executor.submit(process_single_feed, feed, {}): feed  # Empty keyword_groups to skip filtering
                for feed in feeds
            }
            
            # Collect results as they complete, maintaining feed association
            feed_results = {}
            completed_feeds = 0
            collected_entries = []  # Start collecting entries early for pipeline processing
            
            for future in as_completed(future_to_feed):
                feed = future_to_feed[future]
                completed_feeds += 1
                try:
                    entries = future.result()
                    feed_results[feed['description']] = entries
                    collected_entries.extend(entries)  # Add to early collection
                    logger.info(f"📦 Feed {completed_feeds}/{len(feeds)} completed: {feed['description']} ({len(entries)} entries)")
                except Exception as e:
                    logger.error(f"❌ Feed {feed['description']} failed: {e}")
                    feed_results[feed['description']] = []
        
        # Use collected entries (already in completion order) or fallback to ordered collection
        if collected_entries:
            all_entries = collected_entries
        else:
            # Fallback: combine entries in original feed order (if needed)
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
        
        # Post-processing: Check for missed enrichments and retry
        missed_enrichments = [entry for entry in enriched_entries 
                            if not entry.arxiv and len(entry.title.split()) >= 5]  # Skip very short titles
        
        if missed_enrichments:
            logger.info(f"🔄 Found {len(missed_enrichments)} articles that may have missed enrichment, retrying...")
            retry_start_time = time.time()
            
            # Retry with smaller batches and more conservative settings
            from .processors import enrich_single_entry_with_arxiv

            # Use smaller batch size and fewer workers for retry
            retry_workers = min(10, len(missed_enrichments))  # Conservative retry parallelism
            
            retried_entries = []
            with ThreadPoolExecutor(max_workers=retry_workers) as executor:
                future_to_entry = {
                    executor.submit(enrich_single_entry_with_arxiv, entry, 1): entry  # 1 retry max
                    for entry in missed_enrichments
                }
                
                for future in as_completed(future_to_entry):
                    original_entry = future_to_entry[future]
                    try:
                        retried_entry = future.result()
                        retried_entries.append(retried_entry)
                    except Exception as e:
                        logger.warning(f"Retry failed for '{original_entry.title[:50]}...': {e}")
                        retried_entries.append(original_entry)
            
            # Replace original entries with retried entries
            entry_map = {entry.title: entry for entry in retried_entries}
            enriched_entries = [
                entry_map.get(entry.title, entry) for entry in enriched_entries
            ]
            
            retry_time = time.time() - retry_start_time
            successful_retries = sum(1 for entry in retried_entries if entry.arxiv)
            logger.info(f"🔄 Retry completed in {retry_time:.2f}s: {successful_retries}/{len(missed_enrichments)} successful")
        
        arxiv_time = time.time() - arxiv_start_time
        logger.info(f"✅ arXiv enrichment completed in {arxiv_time:.2f}s")
        
        # Apply keyword filtering to enriched articles (with richer arXiv summaries)
        logger.info(f"🎯 Applying keyword filtering to {len(enriched_entries)} enriched articles...")
        filtering_start_time = time.time()
        
        if not keyword_groups:
            filtered_entries = enriched_entries
            logger.info(f"📋 No keyword filtering - including all {len(filtered_entries)} articles")
        else:
            filtered_entries = apply_keyword_filtering(enriched_entries, keyword_groups)
            filtering_time = time.time() - filtering_start_time
            logger.info(f"✅ Keyword filtering completed in {filtering_time:.2f}s")
            logger.info(f"🎯 Found {len(filtered_entries)} articles matching keyword groups (vs {len(entries)} before enrichment)")
        
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
        
        # Get total rate limiting events from global counter
        total_rate_limits = get_rate_limit_count()
        
        # Log completion and display comprehensive results summary
        logger.info("🎉 Processing completed successfully!")
        
        print(f"\n{'='*60}")
        print(f"🏁 PROCESSING COMPLETE - RESULTS SUMMARY")
        print(f"{'='*60}")
        print(f"🎉  Successfully processed {len(enriched_entries)} articles from {len(feeds)} RSS feeds")
        print(f"📄 Output files:")
        print(f"  • YAML: {OUTPUT_YAML_FILE} ({len(enriched_entries)} articles)")
        print(f"  • Markdown: {OUTPUT_MARKDOWN_FILE} ({final_articles} articles after deduplication)")
        
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
        
        # Display rate limiting statistics
        if total_rate_limits > 0:
            print(f"🚨 arXiv API Rate Limiting: {total_rate_limits} events encountered")
            logger.warning(f"🚨 Total arXiv rate limiting events: {total_rate_limits}")
        else:
            print(f"✅ No arXiv rate limiting encountered")
        
        # Display performance metrics
        total_time = time.time() - start_time
        print(f"\n⏱️  Performance Summary:")
        print(f"  • Total execution time: {total_time:.2f}s")
        print(f"  • Feed processing: {feed_time:.2f}s ({len(feeds)} feeds in parallel)")
        print(f"  • arXiv enrichment: {arxiv_time:.2f}s ({len(entries)} articles in parallel)")
        if keyword_groups and 'filtering_time' in locals():
            print(f"  • Keyword filtering: {filtering_time:.2f}s (applied to enriched content)")
        if total_rate_limits > 0:
            print(f"  • Rate limiting events: {total_rate_limits} (check logs for details)")
        print(f"{'='*60}")
    
    except Exception as e:
        logger.error(f"💥 Application failed with unexpected error: {e}")
        logger.error("Check logs above for detailed error information")
        raise


if __name__ == "__main__":
    main() 