"""Unified scraper for multiple betting sites (cross.bet and egamersworld)."""

import re
import time
import random
import requests
import json
from datetime import datetime
from typing import Optional, Dict
from urllib.parse import urlparse


class BetScraper:
    """Unified scraper supporting multiple betting sites."""
    
    # Site configurations
    SITES = {
        "cross.bet": {
            "base_url": "https://www.cross.bet/match/",
            "id_pattern": r'cross\.bet/match/([a-zA-Z0-9]+)',
        },
        "egamersworld.com": {
            "base_url": None,  # Will be extracted from URL
            "id_pattern": r'egamersworld\.com/[\w/]+/match/([a-zA-Z0-9]+)',
        }
    }
    
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    ]
    
    def __init__(self):
        self.session = requests.Session()
        # Configure session to automatically handle compression
        self.session.headers.update({
            'Accept-Encoding': 'gzip, deflate, br'
        })
        self.last_request_time = 0
        self.min_interval = 25
        self.max_interval = 35
        self.consecutive_errors = 0
        self.max_retries = 3
    
    def _get_headers(self) -> Dict:
        return {
            "User-Agent": random.choice(self.USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
        }
    
    def _rate_limit(self, is_background: bool = False):
        """Apply rate limiting. Skip for manual requests to avoid blocking Discord."""
        # Only rate limit for background updates, not user-triggered requests
        if not is_background:
            return
            
        now = time.time()
        time_since_last = now - self.last_request_time
        interval = random.uniform(self.min_interval, self.max_interval)
        
        if time_since_last < interval:
            time.sleep(interval - time_since_last)
        
        self.last_request_time = time.time()
    
    def _detect_site(self, match_input: str) -> tuple:
        """Detect which site to use based on input."""
        if "cross.bet" in match_input.lower():
            match_id = match_input.split("cross.bet/match/")[-1].split("?")[0].split("/")[0]
            return "cross.bet", match_id
        elif "egamersworld.com" in match_input.lower():
            match_id = match_input.split("/match/")[-1].split("?")[0].split("/")[0]
            return "egamersworld.com", match_id
        else:
            # Default to cross.bet
            return "cross.bet", match_input
    
    def _parse_crossbet(self, html: str) -> Optional[Dict]:
        """Parse cross.bet match data."""
        # Find "var match = " and extract the JSON object
        # The JSON ends with a semicolon followed by whitespace and another var
        
        # First, find where var match = starts
        start_marker = 'var match = '
        start_idx = html.find(start_marker)
        
        if start_idx == -1:
            print(f"DEBUG: Could not find 'var match' in HTML")
            return None
        
        # Start after "var match = "
        search_start = start_idx + len(start_marker)
        
        # Find the JSON object - it ends with };
        # We need to balance braces
        brace_count = 0
        json_start = search_start
        in_string = False
        escape_next = False
        
        for i in range(search_start, len(html)):
            char = html[i]
            
            if escape_next:
                escape_next = False
                continue
                
            if char == '\\':
                escape_next = True
                continue
                
            if char == '"' and not escape_next:
                in_string = not in_string
                continue
            
            if in_string:
                continue
                
            if char == '{':
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0:
                    # Found the end of the JSON
                    json_str = html[json_start:i+1]
                    break
        else:
            print(f"DEBUG: Could not find end of JSON object")
            return None
        
        try:
            match_data = json.loads(json_str)
        except json.JSONDecodeError as e:
            print(f"DEBUG: JSON decode error: {e}")
            print(f"DEBUG: JSON string: {json_str[:200]}...")
            return None
        
        teams = match_data.get("teams", [])
        if len(teams) < 2:
            return None
        
        team_a = teams[0]["name"]
        team_b = teams[1]["name"]
        
        map_name = match_data.get("map", "Unknown")
        map_number = match_data.get("mapNum", 1)
        
        score_a = match_data.get("mapScore_home", 0)
        score_b = match_data.get("mapScore_away", 0)
        
        round_score_a = match_data.get("roundScore_home", 0)
        round_score_b = match_data.get("roundScore_away", 0)
        
        cross_odds = match_data.get("cross", {})
        odds_a, odds_b = self._calculate_odds(cross_odds, round_score_a, round_score_b)
        
        if round_score_a > 0 or round_score_b > 0:
            status = "live"
        elif score_a == 0 and score_b == 0:
            status = "upcoming"
        else:
            status = "map_live" if round_score_a < 24 and round_score_b < 24 else "map_ended"
        
        return {
            "match_id": match_data.get("matchId"),
            "source": "cross.bet",
            "event": match_data.get("event", "Unknown Event"),
            "team_a": team_a,
            "team_b": team_b,
            "current_map": map_name,
            "map_number": map_number,
            "odds_a": odds_a,
            "odds_b": odds_b,
            "score_a": score_a,
            "score_b": score_b,
            "round_score_a": round_score_a,
            "round_score_b": round_score_b,
            "status": status,
            "best_of": match_data.get("bestof", "Best of 3"),
            "last_updated": datetime.now().isoformat(),
        }
    
    def _parse_egamersworld(self, html: str, match_id: str) -> Optional[Dict]:
        """Parse egamersworld.com match data."""
        # The site uses Next.js, data is in __NEXT_DATA__ or React components
        # Try to find the match data in the HTML
        
        # Pattern 1: Look for JSON in script tags
        next_data_pattern = r'<script[^>]*id="__NEXT_DATA__"[^>]*>([^<]+)</script>'
        match = re.search(next_data_pattern, html)
        
        if match:
            try:
                data = json.loads(match.group(1))
                props = data.get("props", {}).get("pageProps", {})
                
                # Try to find match data in props
                match_data = props.get("match") or props.get("matchData") or {}
                
                if match_data:
                    return self._extract_egamersworld_data(match_data, match_id)
            except (json.JSONDecodeError, KeyError):
                pass
        
        # Pattern 2: Look for teams in the HTML directly
        team_pattern = r'<a[^>]+href="/counterstrike/teams/[^"]+[^>]*>([^<]+)</a>'
        teams = re.findall(team_pattern, html)
        
        # Pattern 3: Look for odds in the page
        odds_patterns = [
            r'"odds"\s*:\s*\{\s*"home"\s*:\s*([0-9.]+)\s*,\s*"away"\s*:\s*([0-9.]+)',
            r'data-home-odds="([0-9.]+)"[^>]+data-away-odds="([0-9.]+)"',
        ]
        
        for pattern in odds_patterns:
            odds_match = re.search(pattern, html)
            if odds_match:
                break
        
        # If we can't parse properly, return None
        print("DEBUG: Could not parse egamersworld.com data structure")
        return None
    
    def _extract_egamersworld_data(self, match_data: Dict, match_id: str) -> Optional[Dict]:
        """Extract structured data from egamersworld format."""
        try:
            teams = match_data.get("teams", [])
            if len(teams) < 2:
                return None
            
            team_a = teams[0].get("name") or teams[0].get("shortName") or "Team A"
            team_b = teams[1].get("name") or teams[1].get("shortName") or "Team B"
            
            # Get scores
            score_a = match_data.get("homeScore") or match_data.get("score", {}).get("home", 0)
            score_b = match_data.get("awayScore") or match_data.get("score", {}).get("away", 0)
            
            # Get round scores
            round_score_a = match_data.get("roundScore", {}).get("home", 0)
            round_score_b = match_data.get("roundScore", {}).get("away", 0)
            
            # Get map info
            current_map = match_data.get("currentMap") or match_data.get("map", {}).get("name", "Unknown")
            map_number = match_data.get("mapNumber", 1)
            
            # Get odds
            odds_data = match_data.get("odds") or match_data.get("bookmakers", {})
            odds_a = 1.9
            odds_b = 1.9
            
            if isinstance(odds_data, dict):
                # Try common bookmaker keys
                for bookmaker in ["1xbet", "csgopositive", "default"]:
                    if bookmaker in odds_data:
                        bm_odds = odds_data[bookmaker]
                        if isinstance(bm_odds, dict):
                            odds_a = float(bm_odds.get("home", 1.9))
                            odds_b = float(bm_odds.get("away", 1.9))
                            break
            
            status = "live" if (round_score_a > 0 or round_score_b > 0) else "upcoming"
            if score_a > 0 or score_b > 0:
                status = "map_live"
            
            return {
                "match_id": match_id,
                "source": "egamersworld.com",
                "event": match_data.get("event", {}).get("name", "Unknown Event") if isinstance(match_data.get("event"), dict) else match_data.get("event", "Unknown Event"),
                "team_a": team_a,
                "team_b": team_b,
                "current_map": current_map,
                "map_number": map_number,
                "odds_a": round(odds_a, 2),
                "odds_b": round(odds_b, 2),
                "score_a": score_a,
                "score_b": score_b,
                "round_score_a": round_score_a,
                "round_score_b": round_score_b,
                "status": status,
                "best_of": match_data.get("format", "Best of 3"),
                "last_updated": datetime.now().isoformat(),
            }
        except Exception as e:
            print(f"DEBUG: Error extracting egamersworld data: {e}")
            return None
    
    def _calculate_odds(self, cross_odds: Dict, round_score_a: int = 0, round_score_b: int = 0) -> tuple:
        """Calculate odds based on available bookmakers.
        
        Pre-match (no round scores yet): average of 1xbet + csgopositive
        Live (round scores exist): use csgopositive only
        """
        
        onexbet = cross_odds.get("onexbet", {})
        csgopositive = cross_odds.get("csgopositive", {})
        
        odds_a_1xbet = onexbet.get("odds_home")
        odds_b_1xbet = onexbet.get("odds_away")
        odds_a_csgopositive = csgopositive.get("odds_home")
        odds_b_csgopositive = csgopositive.get("odds_away")
        
        try:
            odds_a_1xbet = float(odds_a_1xbet) if odds_a_1xbet else None
            odds_b_1xbet = float(odds_b_1xbet) if odds_b_1xbet else None
            odds_a_csgopositive = float(odds_a_csgopositive) if odds_a_csgopositive else None
            odds_b_csgopositive = float(odds_b_csgopositive) if odds_b_csgopositive else None
        except (ValueError, TypeError):
            odds_a_1xbet = odds_b_1xbet = odds_a_csgopositive = odds_b_csgopositive = None
        
        is_live = round_score_a > 0 or round_score_b > 0
        
        if not is_live:
            odds_a_values = [o for o in [odds_a_1xbet, odds_a_csgopositive] if o is not None]
            odds_b_values = [o for o in [odds_b_1xbet, odds_b_csgopositive] if o is not None]
            
            odds_a = sum(odds_a_values) / len(odds_a_values) if odds_a_values else 1.9
            odds_b = sum(odds_b_values) / len(odds_b_values) if odds_b_values else 1.9
        else:
            odds_a = odds_a_csgopositive if odds_a_csgopositive else 1.9
            odds_b = odds_b_csgopositive if odds_b_csgopositive else 1.9
        
        return round(odds_a, 2), round(odds_b, 2)
    
    def scrape_match(self, match_input: str, is_background: bool = False) -> Optional[Dict]:
        """Scrape match data from ID or full URL.
        
        Args:
            match_input: The match ID or full URL
            is_background: If True, apply rate limiting. If False (default), skip rate limit for user requests.
        """
        site, match_id = self._detect_site(match_input)
        
        if site == "cross.bet":
            url = f"https://www.cross.bet/match/{match_id}"
        elif site == "egamersworld.com":
            # Extract the full URL path
            if "http" in match_input:
                url = match_input
            else:
                # Need to construct URL from the input
                # Egamersworld uses: https://egamersworld.com/counterstrike/match/{id}/...
                url = match_input if "http" in match_input else f"https://egamersworld.com{match_input}"
        else:
            url = f"https://www.cross.bet/match/{match_input}"
        
        for attempt in range(self.max_retries):
            try:
                self._rate_limit(is_background=is_background)
                
                response = self.session.get(url, headers=self._get_headers(), timeout=30)
                response.raise_for_status()
                
                if site == "cross.bet":
                    match_data = self._parse_crossbet(response.text)
                elif site == "egamersworld.com":
                    match_data = self._parse_egamersworld(response.text, match_id)
                else:
                    match_data = self._parse_crossbet(response.text)
                
                if match_data:
                    self.consecutive_errors = 0
                    return match_data
                else:
                    print(f"❌ Error: Couldn't parse match data from {site}")
                    return None
                    
            except requests.exceptions.RequestException as e:
                self.consecutive_errors += 1
                
                if attempt < self.max_retries - 1:
                    backoff = min(2 ** self.consecutive_errors, 60)
                    time.sleep(backoff)
                else:
                    print(f"❌ Error: {e}")
                    return None
        
        return None
    
    def close(self):
        self.session.close()


# Global scraper instance
scraper = BetScraper()
