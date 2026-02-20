"""Factory to get the right scraper based on the match input."""

from typing import Optional, Dict
from scraper.crossbet import scraper as crossbet_scraper
from scraper.bet_scraper import scraper as unified_scraper

def get_scraper_for_input(match_input: str):
    """Return the appropriate scraper instance for the given input."""
    # Use unified scraper for all inputs (handles both cross.bet and egamersworld)
    return unified_scraper

def scrape_any(match_input: str) -> Optional[Dict]:
    """Scrape match data from any supported site."""
    s = get_scraper_for_input(match_input)
    if s:
        return s.scrape_match(match_input)
    return None
