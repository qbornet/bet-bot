"""Web scraper for cross.bet with rate limiting."""

import re
import time
import random
from datetime import datetime
from typing import Optional, Dict
from bs4 import BeautifulSoup
from curl_cffi import requests

class CrossBetScraper:
    """Scraper for cross.bet match data with intelligent rate limiting."""
    
    BASE_URL = "https://www.cross.bet/match/"
    
    # User agents for rotation
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    ]
    
    def __init__(self):
        self.session = requests.Session(impersonate="chrome120")
        proxies = {
            "http": "http://yzhomyyf:ywbo1ca7ngr0@31.59.20.176:6754/",
            "https": "http://yzhomyyf:ywbo1ca7ngr0@31.59.20.176:6754/"
        }
        self.session.proxies.update(proxies)

        self.last_request_time = 0
        self.min_interval = 25  # Minimum 25 seconds
        self.max_interval = 35  # Maximum 35 seconds
        self.consecutive_errors = 0
        self.max_retries = 3
    
    def _get_headers(self) -> Dict:
        """Get random headers."""
        return {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Cache-Control": "max-age=0",
        }
    
    def _rate_limit(self):
        """Apply rate limiting with jitter."""
        now = time.time()
        time_since_last = now - self.last_request_time
        
        # Calculate interval with random jitter
        interval = random.uniform(self.min_interval, self.max_interval)
        
        if time_since_last < interval:
            sleep_time = interval - time_since_last
            time.sleep(sleep_time)
        
        self.last_request_time = time.time()
    
    def _parse_match_data(self, html: str) -> Optional[Dict]:
        """Parse match data from HTML."""
        # Look for the match variable in JavaScript
        # Match "var match = {" up to the closing "};"
        match_pattern = r'var match = (\{.*?\});'
        match_match = re.search(match_pattern, html, re.DOTALL)
        
        if not match_match:
            # Try alternative: find "var match =" and capture until end of object
            match_pattern = r'var match = (\{[\s\S]*?\})[;\s]*(?:var|$)'
            match_match = re.search(match_pattern, html, re.DOTALL)
            
        if not match_match:
            print(f"DEBUG: Could not find 'var match' in HTML. HTML length: {len(html)}")
            # Debug: show what we're looking for
            if 'var match' in html:
                print("DEBUG: 'var match' found but regex failed")
                # Try to find the actual match data
                start = html.find('var match = ')
                if start != -1:
                    print(f"DEBUG: Found 'var match = ' at position {start}")
                    print(f"DEBUG: Context: {html[start:start+200]}")
            return None
        
        import json
        try:
            match_str = match_match.group(1)
            match_data = json.loads(match_str)
        except json.JSONDecodeError as e:
            print(f"DEBUG: JSON decode error: {e}")
            return None
        
        # Extract teams
        teams = match_data.get("teams", [])
        if len(teams) < 2:
            return None
        
        team_a = teams[0]["name"]
        team_b = teams[1]["name"]
        
        # Get current map info
        map_name = match_data.get("map", "Unknown")
        map_number = match_data.get("mapNum", 1)
        
        # Get scores
        score_a = match_data.get("mapScore_home", 0)
        score_b = match_data.get("mapScore_away", 0)
        
        # Get odds from bookmakers
        cross_odds = match_data.get("cross", {})
        
        # Calculate odds (pass round scores to determine if live)
        round_score_a = match_data.get("roundScore_home", 0)
        round_score_b = match_data.get("roundScore_away", 0)
        odds_a, odds_b = self._calculate_odds(cross_odds, round_score_a, round_score_b)
        
        # Determine match status
        # Match is live if we have round scores
        
        if round_score_a > 0 or round_score_b > 0:
            status = "live"
        elif score_a == 0 and score_b == 0:
            status = "upcoming"
        else:
            # Map completed if score is 16+ or best of 3 logic
            status = "map_live" if round_score_a < 24 and round_score_b < 24 else "map_ended"
        
        return {
            "match_id": match_data.get("matchId"),
            "event": match_data.get("event", "Unknown Event"),
            "team_a": team_a,
            "team_b": team_b,
            "current_map": map_name,
            "map_number": map_number,
            "odds_a": odds_a,
            "odds_b": odds_b,
            "score_a": score_a,  # Maps won
            "score_b": score_b,  # Maps won
            "round_score_a": round_score_a,
            "round_score_b": round_score_b,
            "status": status,
            "best_of": match_data.get("bestof", "Best of 3"),
            "last_updated": datetime.now().isoformat(),
        }
    
    def _calculate_odds(self, cross_odds: Dict, round_score_a: int = 0, round_score_b: int = 0) -> tuple:
        """Calculate odds based on available bookmakers.
        
        Pre-match (no round scores yet): average of 1xbet + csgopositive
        Live (round scores exist): use csgopositive only
        """
        
        # Get odds from bookmakers
        onexbet = cross_odds.get("onexbet", {})
        csgopositive = cross_odds.get("csgopositive", {})
        
        odds_a_1xbet = onexbet.get("odds_home")
        odds_b_1xbet = onexbet.get("odds_away")
        odds_a_csgopositive = csgopositive.get("odds_home")
        odds_b_csgopositive = csgopositive.get("odds_away")
        
        # Convert to float
        try:
            odds_a_1xbet = float(odds_a_1xbet) if odds_a_1xbet else None
            odds_b_1xbet = float(odds_b_1xbet) if odds_b_1xbet else None
            odds_a_csgopositive = float(odds_a_csgopositive) if odds_a_csgopositive else None
            odds_b_csgopositive = float(odds_b_csgopositive) if odds_b_csgopositive else None
        except (ValueError, TypeError):
            odds_a_1xbet = odds_b_1xbet = odds_a_csgopositive = odds_b_csgopositive = None
        
        # Determine which calculation to use
        # Pre-match: no round scores yet (both 0)
        # Live: round scores exist (at least one > 0)
        
        is_live = round_score_a > 0 or round_score_b > 0
        
        if not is_live:
            # Pre-match: average of 1xbet + csgopositive
            odds_a_values = [o for o in [odds_a_1xbet, odds_a_csgopositive] if o is not None]
            odds_b_values = [o for o in [odds_b_1xbet, odds_b_csgopositive] if o is not None]
            
            odds_a = sum(odds_a_values) / len(odds_a_values) if odds_a_values else 1.9
            odds_b = sum(odds_b_values) / len(odds_b_values) if odds_b_values else 1.9
        else:
            # Live: use only csgopositive
            odds_a = odds_a_csgopositive if odds_a_csgopositive else 1.9
            odds_b = odds_b_csgopositive if odds_b_csgopositive else 1.9
        
        return round(odds_a, 2), round(odds_b, 2)
    
    def scrape_match(self, match_input: str) -> Optional[Dict]:
        """Scrape match data from ID or full URL with retry logic."""
        # Extract ID if a full URL is provided
        match_id = match_input
        if "cross.bet/match/" in match_input:
            match_id = match_input.split("cross.bet/match/")[1].split("?")[0].split("/")[0]
        
        url = f"{self.BASE_URL}{match_id}"
        
        for attempt in range(self.max_retries):
            try:
                self._rate_limit()
                
                response = self.session.get(
                    url,
                    headers=self._get_headers(),
                    timeout=30
                )
                response.raise_for_status()
                
                match_data = self._parse_match_data(response.text)
                
                if match_data:
                    self.consecutive_errors = 0
                    return match_data
                else:
                    print("❌ Error couldn't parse match data\n");
                    # No match data found, might be invalid match ID
                    return None
                    
            except requests.exceptions.RequestException as e:
                self.consecutive_errors += 1
                
                if attempt < self.max_retries - 1:
                    # Exponential backoff
                    backoff = min(2 ** self.consecutive_errors, 60)
                    time.sleep(backoff)
                else:
                    # Max retries reached
                    return None
        
        return None
    
    def close(self):
        """Close the HTTP session."""
        self.session.close()


# Global scraper instance
scraper = CrossBetScraper()
