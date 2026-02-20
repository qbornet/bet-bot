"""JSON storage utilities for the betting bot."""

import json
import os
from datetime import datetime
from typing import Dict, Any, Optional


DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

USERS_FILE = os.path.join(DATA_DIR, "users.json")
BETS_FILE = os.path.join(DATA_DIR, "bets.json")
MATCHES_FILE = os.path.join(DATA_DIR, "matches.json")


def ensure_data_dir():
    """Ensure data directory exists."""
    os.makedirs(DATA_DIR, exist_ok=True)


def load_json(filepath: str, default: Any = None) -> Any:
    """Load JSON file, return default if not exists."""
    ensure_data_dir()
    if not os.path.exists(filepath):
        return default if default is not None else {}
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return default if default is not None else {}


def save_json(filepath: str, data: Any):
    """Save data to JSON file."""
    ensure_data_dir()
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2, default=str)


# Users
def get_user(user_id: str) -> Optional[Dict]:
    """Get user data or None if not exists."""
    users = load_json(USERS_FILE, {})
    return users.get(str(user_id))


def create_user(user_id: str, starting_balance: int = 1000) -> Dict:
    """Create new user with starting balance."""
    users = load_json(USERS_FILE, {})
    users[str(user_id)] = {
        "balance": starting_balance,
        "total_won": 0,
        "total_lost": 0,
        "bets_placed": 0
    }
    save_json(USERS_FILE, users)
    return users[str(user_id)]


def update_user(user_id: str, updates: Dict) -> Dict:
    """Update user data."""
    users = load_json(USERS_FILE, {})
    user_id = str(user_id)
    if user_id not in users:
        users[user_id] = create_user(user_id)
    users[user_id].update(updates)
    save_json(USERS_FILE, users)
    return users[user_id]


def get_or_create_user(user_id: str) -> Dict:
    """Get user or create if not exists."""
    user = get_user(user_id)
    if user is None:
        user = create_user(user_id)
    return user


def get_leaderboard(limit: int = 10) -> list:
    """Get top users by balance."""
    users = load_json(USERS_FILE, {})
    sorted_users = sorted(
        users.items(),
        key=lambda x: x[1]["balance"],
        reverse=True
    )
    return [(uid, data) for uid, data in sorted_users[:limit]]


# Matches
def get_match(match_id: str) -> Optional[Dict]:
    """Get match data or None if not exists."""
    matches = load_json(MATCHES_FILE, {})
    return matches.get(str(match_id))


def get_all_matches() -> Dict[str, Dict]:
    """Get all active matches."""
    return load_json(MATCHES_FILE, {})


def save_match(match_id: str, match_data: Dict):
    """Save or update match data."""
    matches = load_json(MATCHES_FILE, {})
    matches[str(match_id)] = match_data
    save_json(MATCHES_FILE, matches)


def remove_match(match_id: str):
    """Remove match from storage."""
    matches = load_json(MATCHES_FILE, {})
    if str(match_id) in matches:
        del matches[str(match_id)]
        save_json(MATCHES_FILE, matches)


# Bets
def get_bet(bet_id: str) -> Optional[Dict]:
    """Get bet data or None if not exists."""
    bets = load_json(BETS_FILE, {})
    return bets.get(str(bet_id))


def get_all_bets() -> Dict[str, Dict]:
    """Get all active bets."""
    return load_json(BETS_FILE, {})


def get_user_bets(user_id: str) -> Dict[str, Dict]:
    """Get all bets for a user."""
    bets = load_json(BETS_FILE, {})
    return {bid: bdata for bid, bdata in bets.items() if bdata["user_id"] == str(user_id)}


def get_user_bet_for_map(user_id: str, match_id: str, map_number: int) -> Optional[Dict]:
    """Check if user already bet on this map."""
    bets = load_json(BETS_FILE, {})
    for bet_id, bet_data in bets.items():
        if (bet_data["user_id"] == str(user_id) and
            bet_data["match_id"] == str(match_id) and
            bet_data["map_number"] == map_number):
            return bet_data
    return None


def get_match_bets(match_id: str) -> Dict[str, Dict]:
    """Get all bets for a match."""
    bets = load_json(BETS_FILE, {})
    return {bid: bdata for bid, bdata in bets.items() if bdata["match_id"] == str(match_id)}


def save_bet(bet_id: str, bet_data: Dict):
    """Save or update bet data."""
    bets = load_json(BETS_FILE, {})
    bets[str(bet_id)] = bet_data
    save_json(BETS_FILE, bets)


def remove_bet(bet_id: str):
    """Remove bet from storage."""
    bets = load_json(BETS_FILE, {})
    if str(bet_id) in bets:
        del bets[str(bet_id)]
        save_json(BETS_FILE, bets)


def generate_bet_id() -> str:
    """Generate unique bet ID."""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    import random
    rand = random.randint(1000, 9999)
    return f"bet_{timestamp}_{rand}"
