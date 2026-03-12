#!/usr/bin/env python3
"""Track one CSGOPositive match from Selenium to seed metadata and WS updates.

Workflow:
1) Start selenium and parse initial match page metadata (teams, BO, map, scores,
   and any initial odds data).
2) Close selenium and keep a websocket session for live updates.
3) On map change, reopen selenium briefly to refresh map context, then close it.
4) Stop when BO is finished.
"""

import argparse
import json
import os
import random
import re
import signal
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse


try:
    from bs4 import BeautifulSoup  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    BeautifulSoup = None


SOCKET_URL = "wss://ws.csgopositive.com/odds/socket.io"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

BASE_REQUEST_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://csgopositive.com",
    "Referer": "https://csgopositive.com/",
}

SHUTDOWN_REQUESTED = False


def _request_shutdown(signum, frame):
    global SHUTDOWN_REQUESTED
    SHUTDOWN_REQUESTED = True
    print(f"[{signum}] shutdown requested, closing websocket and exiting")


def normalize_headers(user_agent: Optional[str]) -> Dict[str, str]:
    headers = dict(BASE_REQUEST_HEADERS)
    headers["User-Agent"] = user_agent or random.choice(USER_AGENTS)
    return headers


def set_proxy_env(proxy_url: Optional[str], should_override: bool) -> Optional[Dict[str, Optional[str]]]:
    if not should_override:
        return None

    previous = {
        "HTTP_PROXY": os.environ.get("HTTP_PROXY"),
        "HTTPS_PROXY": os.environ.get("HTTPS_PROXY"),
        "http_proxy": os.environ.get("http_proxy"),
        "https_proxy": os.environ.get("https_proxy"),
    }

    if proxy_url is None:
        for key in previous:
            os.environ.pop(key, None)
    else:
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        os.environ["http_proxy"] = proxy_url
        os.environ["https_proxy"] = proxy_url

    return previous


def restore_proxy_env(previous: Optional[Dict[str, Optional[str]]]) -> None:
    if previous is None:
        return

    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def to_int(value: Any) -> Optional[int]:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def to_float(value: Any) -> Optional[float]:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def normalize_socket_target(raw_url: str) -> Tuple[str, Optional[str]]:
    parsed = urlparse(raw_url)
    if not parsed.scheme or not parsed.netloc:
        return raw_url if "://" in raw_url else f"wss://{raw_url}", None

    base = f"{parsed.scheme}://{parsed.netloc}"
    socketio_path = None
    if parsed.path and "/socket.io" in parsed.path:
        path_base = parsed.path.split("/socket.io", 1)[0].strip("/")
        socketio_path = "socket.io" if not path_base else f"{path_base}/socket.io"

    return base, socketio_path


def normalize_payload(payload: Any) -> Any:
    if isinstance(payload, bytes):
        try:
            return payload.decode("utf-8", errors="replace")
        except Exception:
            return payload

    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return {"raw": payload}

    return payload


def extract_market_pair(value: Any) -> Optional[Tuple[float, float]]:
    if isinstance(value, (list, tuple)):
        if len(value) < 2:
            return None
        a = to_float(value[0])
        b = to_float(value[1])
        if a is None or b is None:
            return None
        return a, b

    if isinstance(value, str):
        nums = re.findall(r"-?\d+\.?\d*", value)
        if len(nums) < 2:
            return None
        try:
            return float(nums[0]), float(nums[1])
        except ValueError:
            return None

    if isinstance(value, dict):
        candidates = []
        for key, val in value.items():
            numeric = to_float(val)
            if numeric is None:
                continue
            candidates.append((str(key).lower(), numeric))

        home = None
        away = None
        for key, numeric in candidates:
            if key in {"home", "a", "team_a", "teama", "left", "1", "first", "t1"}:
                home = numeric
            elif key in {"away", "b", "team_b", "teamb", "right", "2", "second", "t2"}:
                away = numeric

        if home is not None and away is not None:
            return home, away

        # Fallback to first two numeric values only
        if len(candidates) >= 2:
            return candidates[0][1], candidates[1][1]

    return None


def extract_markets(payload: Any) -> Dict[str, Tuple[float, float]]:
    """Extract all keys that look like live:win_* odds from payload."""
    markets: Dict[str, Tuple[float, float]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key.startswith("live:win_"):
                    odds = extract_market_pair(value)
                    if odds is not None:
                        markets[key] = odds
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(payload)
    return markets


def extract_markets_from_text(text: str) -> Dict[str, Tuple[float, float]]:
    markets: Dict[str, Tuple[float, float]] = {}
    patterns = [
        r'"(live:win_[^"]+)"\s*:\s*(\[[^\]]+\])',
        r'"(live:win_[^"]+)"\s*:\s*(\{[^\{\}]+\})',
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE | re.DOTALL):
            key = match.group(1)
            raw = match.group(2)
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = raw
            odds = extract_market_pair(parsed)
            if odds is not None:
                markets[key] = odds

    return markets


def market_kind(name: str) -> str:
    if not name.startswith("live:win_"):
        return "other"

    # live:win_3 -> BO odds (BO1,BO3,BO5)
    if re.fullmatch(r"live:win_\d+$", name):
        return "bo"

    # live:win_3_2 -> current map odds
    if re.fullmatch(r"live:win_\d+_\d+$", name):
        return "map"

    # fallback for unusual variants that include only one segment after prefix
    if name.count("_") == 1:
        return "bo"
    return "map"


def ensure_state(match_id: str) -> Dict[str, Any]:
    return {
        "match_id": str(match_id),
        "team_a": "Team A",
        "team_b": "Team B",
        "bo": 3,
        "map": "Unknown",
        "map_number": None,
        "round_score_a": None,
        "round_score_b": None,
        "series_score_a": 0,
        "series_score_b": 0,
        "bo_odds": (None, None),
        "map_odds": (None, None),
        "bo_odds_source": None,
        "map_odds_source": None,
        "updated_once": False,
        "last_map_refresh_key": None,
    }


def pretty_value(v: Optional[float]) -> str:
    if v is None:
        return "--"
    return f"{v:.2f}"


def print_state(state: Dict[str, Any], source: str) -> None:
    map_name = state.get("map") or "Unknown"
    map_number = state.get("map_number")
    map_label = f"Map {map_number} - {map_name}" if map_number is not None else map_name
    bo = state.get("bo")
    bo_label = f"BO{bo}" if bo else "Unknown"

    round_score = (
        f"{state.get('round_score_a') if state.get('round_score_a') is not None else '--'} "
        f"- {state.get('round_score_b') if state.get('round_score_b') is not None else '--'}"
    )
    series_score = (
        f"{state.get('series_score_a', 0)} - {state.get('series_score_b', 0)}"
    )

    bo_odds = f"{pretty_value(state['bo_odds'][0])} / {pretty_value(state['bo_odds'][1])}"
    map_odds = f"{pretty_value(state['map_odds'][0])} / {pretty_value(state['map_odds'][1])}"

    print(
        f"[{source}] {state['team_a']} vs {state['team_b']} | {map_label} | {bo_label} "
        f"| Live score: {round_score} | Series: {series_score} "
        f"| BO odds: {bo_odds} ({state['bo_odds_source']}) "
        f"| Map odds: {map_odds} ({state['map_odds_source']})"
    )


def update_markets_from_dict(
    state: Dict[str, Any],
    markets: Dict[str, Tuple[float, float]],
    source: str,
) -> None:
    for name, odds in markets.items():
        kind = market_kind(name)
        if kind == "bo":
            if state.get("bo_odds") != odds:
                state["bo_odds"] = odds
                state["bo_odds_source"] = f"{source}:{name}"
        elif kind == "map":
            if state.get("map_odds") != odds:
                state["map_odds"] = odds
                state["map_odds_source"] = f"{source}:{name}"


def parse_bo(value: Any) -> Optional[int]:
    text = str(value)
    if text:
        # allow bo=3 and "BO3"
        match = re.search(r"(\d+)", text)
        if match:
            return to_int(match.group(1))
    return None


def extract_text_patterns(html: str, patterns: List[str]) -> Optional[str]:
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            value = match.group(1)
            if value is None:
                continue
            value = value.strip()
            if value:
                return value
    return None


def update_state_from_mapping(state: Dict[str, Any], payload: Any) -> bool:
    """Update state from nested dict/list payload, return True if anything changed."""
    changed = False

    def _walk(node: Any) -> None:
        nonlocal changed

        if isinstance(node, dict):
            for key, value in node.items():
                if not isinstance(key, str):
                    _walk(value)
                    continue

                lk = key.lower().replace("-", "_")

                if lk in {"team_a", "teama", "teamone", "team1", "home", "team_home", "home_team"}:
                    if isinstance(value, str) and value.strip():
                        state["team_a"] = value.strip()
                        changed = True

                elif lk in {"team_b", "teamb", "teamtwo", "team2", "away", "team_away", "away_team"}:
                    if isinstance(value, str) and value.strip():
                        state["team_b"] = value.strip()
                        changed = True

                elif lk == "teams" and isinstance(value, (list, tuple)) and len(value) >= 2:
                    team_zero = value[0]
                    team_one = value[1]

                    if isinstance(team_zero, dict):
                        for name_key in ("name", "team_name", "short_name", "team"):
                            team_name = team_zero.get(name_key)
                            if isinstance(team_name, str) and team_name.strip():
                                state["team_a"] = team_name.strip()
                                changed = True
                                break

                    if isinstance(team_one, dict):
                        for name_key in ("name", "team_name", "short_name", "team"):
                            team_name = team_one.get(name_key)
                            if isinstance(team_name, str) and team_name.strip():
                                state["team_b"] = team_name.strip()
                                changed = True
                                break

                elif lk in {"bestof", "best_of", "best-of"}:
                    bo = parse_bo(value)
                    if bo:
                        state["bo"] = bo
                        changed = True

                elif lk in {"bo", "bo_type", "best_of_match"}:
                    bo = parse_bo(value)
                    if bo:
                        state["bo"] = bo
                        changed = True

                elif lk in {"map", "current_map", "map_name", "currentmap", "mapname"}:
                    if isinstance(value, str) and value.strip():
                        if state.get("map") != value.strip():
                            state["map"] = value.strip()
                            changed = True

                elif lk in {"map_num", "mapnum", "map_number", "mapnumber"}:
                    number = to_int(value)
                    if number is not None and state.get("map_number") != number:
                        state["map_number"] = number
                        changed = True

                elif lk in {"roundscore_home", "round_score_home", "round_home", "rounds_home", "home_round_score"}:
                    number = to_int(value)
                    if number is not None and state.get("round_score_a") != number:
                        state["round_score_a"] = number
                        changed = True

                elif lk in {"roundscore_away", "round_score_away", "round_away", "rounds_away", "away_round_score"}:
                    number = to_int(value)
                    if number is not None and state.get("round_score_b") != number:
                        state["round_score_b"] = number
                        changed = True

                elif lk in {"mapscore_home", "map_score_home", "home_map_score", "home_maps", "score_a"}:
                    number = to_int(value)
                    if number is not None and state.get("series_score_a") != number:
                        state["series_score_a"] = number
                        changed = True

                elif lk in {"mapscore_away", "map_score_away", "away_map_score", "away_maps", "score_b"}:
                    number = to_int(value)
                    if number is not None and state.get("series_score_b") != number:
                        state["series_score_b"] = number
                        changed = True

                _walk(value)

        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)

    _walk(payload)
    return changed


def filter_match_payload(payload: Any, match_id: str) -> Optional[Dict[str, Any]]:
    """Handle payload shape differences and filter by match id when possible."""
    if not isinstance(payload, dict):
        if isinstance(payload, list) and len(payload) >= 2:
            for item in payload[1:]:
                found = filter_match_payload(item, match_id)
                if found is not None:
                    return found
        return None

    candidate = payload
    payload_match_id = payload.get("id") or payload.get("matchId") or payload.get("match_id")
    if payload_match_id is not None and str(payload_match_id) == str(match_id):
        return candidate

    # sometimes payload contains a 'data' list with per-match objects
    nested = payload.get("data") if isinstance(payload.get("data"), list) else None
    if nested:
        for item in nested:
            found = filter_match_payload(item, match_id)
            if found is not None:
                return found

    # accept if no id is present and event context looks like match payload
    if payload_match_id is None:
        return candidate

    return None


def _safe_parse_json_from_text(text: str) -> Optional[Any]:
    """Parse a JSON value from the start of the text using json.JSONDecoder."""
    decoder = json.JSONDecoder()
    try:
        value, _ = decoder.raw_decode(text)
    except Exception:
        return None
    return value


def _extract_json_candidates_from_script(script_text: str) -> List[Any]:
    """Extract standalone JSON objects/arrays from a script body."""
    candidates: List[Any] = []
    text = (script_text or "").strip()
    if not text:
        return candidates

    # Direct JSON script tag
    parsed = _safe_parse_json_from_text(text)
    if isinstance(parsed, (dict, list)):
        candidates.append(parsed)

    # Try known JS marker assignments.
    marker_patterns = [
        r"__NUXT__\s*=\s*(\{)",
        r"window\.__NUXT__\s*=\s*(\{)",
        r"__INITIAL_STATE__\s*=\s*(\{)",
        r"window\.__INITIAL_STATE__\s*=\s*(\{)",
        r"window\.__NEXT_DATA__\s*=\s*(\{)",
        r"matchData\s*=\s*(\{)",
        r"window\.matchData\s*=\s*(\{)",
        r"var\s+match\s*=\s*(\{)",
        r"match\s*=\s*(\{)",
    ]

    for pattern in marker_patterns:
        for marker in re.finditer(pattern, text, re.IGNORECASE):
            start = marker.end(1) - 1
            parsed_marker = _safe_parse_json_from_text(text[start:])
            if isinstance(parsed_marker, (dict, list)):
                candidates.append(parsed_marker)

    # Also recover object values from inline arrays that may represent state payload.
    for marker in ["live:win_"]:
        if marker in text:
            for found in re.finditer(r'\"(live:win_[^\"]+)\"\s*:\s*(\[[^\]]+\]|\{[^\{\}]+\})', text):
                raw = found.group(2)
                try:
                    parsed_marker = json.loads(raw)
                except Exception:
                    parsed_marker = _safe_parse_json_from_text(raw)
                if isinstance(parsed_marker, (list, tuple, dict)):
                    candidates.append({found.group(1): parsed_marker})

    return candidates


def _is_match_payload(node: Any) -> bool:
    if not isinstance(node, dict):
        return False

    keys = {str(k).lower() for k in node.keys() if isinstance(k, str)}
    has_team_fields = bool({
        "team_a",
        "team_b",
        "team1",
        "team2",
        "teama",
        "teamb",
        "home",
        "away",
        "home_team",
        "away_team",
        "h_team",
        "a_team",
        "teams",
        "teamname_a",
        "teamname_b",
        "hometeam",
        "awayteam",
    } & keys)

    has_score_fields = bool({
        "mapscore_home",
        "mapscore_away",
        "score_a",
        "score_b",
        "round_score_home",
        "round_score_away",
        "roundscore_home",
        "roundscore_away",
        "currentmap",
        "current_map",
        "bestof",
        "best_of",
        "match_id",
        "matchid",
        "match_id",
    } & keys)

    has_match_indicator = bool({"match_id", "matchid", "match_id", "id", "event_id"} & keys)

    return has_team_fields or has_match_indicator or has_score_fields


def _extract_match_payloads(node: Any) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if _is_match_payload(value):
                results.append(value)

            for nested in value.values():
                walk(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                walk(nested)

    walk(node)
    return results


def _extract_script_texts(html: str) -> List[str]:
    if BeautifulSoup is not None:
        soup = BeautifulSoup(html, "html.parser")
        script_texts = [
            tag.get_text(" ", strip=True)
            for tag in soup.find_all("script")
            if tag.get_text(strip=True)
        ]
    else:
        script_texts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL | re.IGNORECASE)

    return [text for text in script_texts if text]


def _extract_match_json_blobs(html: str) -> List[Any]:
    blobs: List[Any] = []
    for raw in _extract_script_texts(html):
        blobs.extend(_extract_json_candidates_from_script(raw))

    return blobs


def _has_real_data(state: Dict[str, Any]) -> bool:
    return (
        state.get("team_a") not in (None, "Team A")
        or state.get("team_b") not in (None, "Team B")
    )


def parse_match_from_browser_object(driver: Any, state: Dict[str, Any], debug: bool) -> bool:
    """Read SPA state objects from the browser runtime and update state."""
    changed = False

    script = """
    return {
        'nuxt': window.__NUXT__,
        'initialState': window.__INITIAL_STATE__,
        'nextData': window.__NEXT_DATA__,
        'matchData': window.matchData,
    };
    """

    try:
        data = driver.execute_script(script)
    except Exception:
        return False

    if not isinstance(data, dict):
        return False

    for key in ("nuxt", "initialState", "matchData"):
        payload = data.get(key)
        if isinstance(payload, dict):
            if update_state_from_mapping(state, payload):
                changed = True

            markets = extract_markets(payload)
            if markets:
                update_markets_from_dict(state, markets, "selenium")

    next_payload = data.get("nextData")
    if isinstance(next_payload, dict):
        payload = next_payload
        if "props" in payload or "pageProps" in payload or "state" in payload:
            if update_state_from_mapping(state, payload):
                changed = True
            markets = extract_markets(payload)
            if markets:
                update_markets_from_dict(state, markets, "selenium")
        else:
            if update_state_from_mapping(state, payload):
                changed = True
            markets = extract_markets(payload)
            if markets:
                update_markets_from_dict(state, markets, "selenium")

    if changed and debug:
        print_state(state, "selenium:runtime")

    return changed


def _extract_tag_text(node: Any) -> Optional[str]:
    if node is None:
        return None
    if not hasattr(node, "get_text"):
        return None
    text = node.get_text(" ", strip=True)
    if not text:
        return None
    return re.sub(r"\s+", " ", text).strip()


def _extract_float_from_text(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return to_float(match.group(0))


def _parse_csgo_event_card(card: Any, state: Dict[str, Any], debug_match_id: str) -> bool:
    """Parse a single match card from the /en landing markup."""
    changed = False

    if card is None:
        return False

    # Keep only requested match / CS2 scope.
    card_match_id = card.get("data-id") if hasattr(card, "get") else None
    card_app_id = card.get("data-app_id") if hasattr(card, "get") else None
    if card_match_id != str(debug_match_id):
        return False
    if str(card_app_id or "") not in {"730", "cs2", "CS2"}:
        return False

    team_a = _extract_tag_text(card.select_one(".left .team_name"))
    team_b = _extract_tag_text(card.select_one(".right .team_name"))

    if team_a and state.get("team_a") != team_a:
        state["team_a"] = team_a
        changed = True
    if team_b and state.get("team_b") != team_b:
        state["team_b"] = team_b
        changed = True

    # In current /en layout, round scores are typically in .team_score_add.
    score_a_node = card.select_one(".left .team_score_add") or card.select_one(".left .team_score")
    score_b_node = card.select_one(".right .team_score_add") or card.select_one(".right .team_score")
    score_a = _extract_float_from_text(_extract_tag_text(score_a_node))
    score_b = _extract_float_from_text(_extract_tag_text(score_b_node))
    score_a_int = to_int(score_a)
    score_b_int = to_int(score_b)

    if score_a_int is not None and state.get("round_score_a") != score_a_int:
        state["round_score_a"] = to_int(score_a)
        changed = True
    if score_b_int is not None and state.get("round_score_b") != score_b_int:
        state["round_score_b"] = to_int(score_b)
        changed = True

    # BO type.
    bo = _extract_tag_text(card.select_one(".event_type"))
    if bo:
        parsed_bo = parse_bo(bo)
        if parsed_bo and state.get("bo") != parsed_bo:
            state["bo"] = parsed_bo
            changed = True

    # Match-level odds can appear on the card as .sum values.
    left_odds = _extract_float_from_text(_extract_tag_text(card.select_one(".left .sum")))
    right_odds = _extract_float_from_text(_extract_tag_text(card.select_one(".right .sum")))
    if left_odds is not None and right_odds is not None:
        pair = (left_odds, right_odds)
        if state.get("bo_odds") != pair:
            state["bo_odds"] = pair
            state["bo_odds_source"] = "selenium:event_card"
            changed = True

    return changed


def _parse_match_from_match_cards(html: str, state: Dict[str, Any]) -> bool:
    if BeautifulSoup is None:
        return False

    match_id = str(state.get("match_id", ""))
    if not match_id:
        return False

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return False

    selectors = [
        f'.event[data-id="{match_id}"][data-app_id="730"]',
        f'.match_banner[data-id="{match_id}"][class*="match_banner"]',
        f'.line_event[data-id="{match_id}"]',
        f'[data-id="{match_id}"]',
    ]

    candidates = []
    for selector in selectors:
        candidates.extend(soup.select(selector))
        if candidates:
            break

    if not candidates:
        return False

    changed = False
    for card in candidates:
        if _parse_csgo_event_card(card=card, state=state, debug_match_id=match_id):
            changed = True

    return changed


def parse_match_from_html(html: str, state: Dict[str, Any]) -> Dict[str, Any]:
    # Try resilient script-json extraction from dynamic HTML first.
    blobs = _extract_match_json_blobs(html)
    changed = False

    if _parse_match_from_match_cards(html, state):
        changed = True
        print_state(state, "selenium:event_card")

    for blob in blobs:
        for candidate in _extract_match_payloads(blob):
            if update_state_from_mapping(state, candidate):
                changed = True
            update_markets_from_dict(state, extract_markets(candidate), "selenium")

        # Sometimes the markets appear as standalone top-level entries in parsed script blobs.
        if isinstance(blob, dict):
            markets = extract_markets(blob)
            if markets:
                update_markets_from_dict(state, markets, "selenium")

    if changed:
        print_state(state, "selenium:json")

    team_a = extract_text_patterns(
        html,
        [
            r'"teamA"\s*:\s*"([^"]+)"',
            r'"homeTeam"\s*:\s*"([^"]+)"',
            r'"team_a"\s*:\s*"([^"]+)"',
            r'"team\w*1"\s*:\s*"([^"]+)"',
        ],
    )

    team_b = extract_text_patterns(
        html,
        [
            r'"teamB"\s*:\s*"([^"]+)"',
            r'"awayTeam"\s*:\s*"([^"]+)"',
            r'"team_b"\s*:\s*"([^"]+)"',
            r'"team\w*2"\s*:\s*"([^"]+)"',
        ],
    )

    current_map = extract_text_patterns(
        html,
        [
            r'"currentMap"\s*:\s*"([^"]+)"',
            r'"current_map"\s*:\s*"([^"]+)"',
            r'"map"\s*:\s*"([^"]+)"',
        ],
    )

    map_number = extract_text_patterns(
        html,
        [
            r'"mapNum"\s*:\s*(\d+)',
            r'"mapNumber"\s*:\s*(\d+)',
            r'"map_number"\s*:\s*(\d+)',
        ],
    )

    bo = extract_text_patterns(
        html,
        [
            r'"bestOf"\s*:\s*(\d+)',
            r'"bestof"\s*:\s*(\d+)',
            r'"best_of"\s*:\s*(\d+)',
        ],
    )

    score_a = extract_text_patterns(
        html,
        [
            r'"score_a"\s*:\s*(\d+)',
            r'"mapScore_home"\s*:\s*(\d+)',
            r'"score_home"\s*:\s*(\d+)',
        ],
    )

    score_b = extract_text_patterns(
        html,
        [
            r'"score_b"\s*:\s*(\d+)',
            r'"mapScore_away"\s*:\s*(\d+)',
            r'"score_away"\s*:\s*(\d+)',
        ],
    )

    if team_a:
        state["team_a"] = team_a
    if team_b:
        state["team_b"] = team_b
    if current_map:
        state["map"] = current_map
    if map_number:
        state["map_number"] = to_int(map_number)
    if bo:
        parsed_bo = parse_bo(bo)
        if parsed_bo:
            state["bo"] = parsed_bo
    if score_a:
        state["series_score_a"] = to_int(score_a)
    if score_b:
        state["series_score_b"] = to_int(score_b)

    # Parse odds embedded in HTML scripts as fallback
    script_texts = _extract_script_texts(html)

    markets: Dict[str, Tuple[float, float]] = {}
    for raw in script_texts:
        if not raw or ("live:win_" not in raw and "odds" not in raw.lower()):
            continue
        markets.update(extract_markets_from_text(raw))

    if markets:
        update_markets_from_dict(state, markets, "selenium")

    # Parse structured blobs again with broader patterns.
    for blob in blobs:
        if isinstance(blob, dict):
            if update_state_from_mapping(state, blob):
                changed = True
            markets = extract_markets(blob)
            if markets:
                update_markets_from_dict(state, markets, "selenium")

    # Fallback: direct regex extraction from HTML markup.
    if changed:
        print_state(state, "selenium:fallback")

    return state


def try_click_map_or_teams(driver: Any, state: Dict[str, Any]) -> None:
    """Attempt to click visible controls that look like map/team selectors."""
    from selenium.webdriver.common.by import By  # type: ignore
    from selenium.webdriver.support.ui import WebDriverWait  # type: ignore
    from selenium.webdriver.support import expected_conditions as EC  # type: ignore

    try:
        WebDriverWait(driver, 4).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    except Exception:
        pass

    # Primary target: CS2 event row/card for requested match and #bet links.
    match_id = str(state.get("match_id", "")).strip()
    if match_id:
        try:
            selectors = [
                f'.event[data-id="{match_id}"][data-app_id="730"]',
                f'.match_banner[data-id="{match_id}"]',
                f'.line_event[data-id="{match_id}"]',
                f'[data-id="{match_id}"]',
            ]

            for selector in selectors:
                cards = driver.find_elements(By.CSS_SELECTOR, selector)
                if cards:
                    for card in cards:
                        # Some cards have exact /bet anchors to open odds menu.
                        try:
                            bet_links = card.find_elements(By.CSS_SELECTOR, "a[href='#bet']")
                            if not bet_links:
                                bet_links = card.find_elements(By.CSS_SELECTOR, "a[href*='#bet']")
                            if not bet_links:
                                bet_links = card.find_elements(By.CSS_SELECTOR, "a[href='#register']")

                            for idx, link in enumerate(bet_links[:2]):
                                if SHUTDOWN_REQUESTED:
                                    return
                                if not link.is_displayed():
                                    continue
                                if not link.is_enabled():
                                    continue
                                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", link)
                                try:
                                    driver.execute_script("arguments[0].click();", link)
                                except Exception:
                                    link.click()
                                time.sleep(1.0 if idx == 0 else 0.6)
                            if bet_links:
                                return
                        except Exception:
                            continue
                    break
        except Exception:
            pass

    candidates = [
        state.get("team_a", ""),
        state.get("team_b", ""),
        "Map",
        str(state.get("map_number") or ""),
        "Live",
        "BO",
    ]

    lowered = [item.lower() for item in candidates if item]
    clickable = []
    try:
        clickable = driver.find_elements(By.CSS_SELECTOR, "button") + driver.find_elements(By.CSS_SELECTOR, "a")
    except Exception:
        try:
            clickable = driver.find_elements(By.CSS_SELECTOR, "*[onclick]")
        except Exception:
            clickable = []

    for idx, element in enumerate(clickable[:200]):
        if SHUTDOWN_REQUESTED:
            return
        try:
            label = (element.text or "").strip().lower()
            if not label:
                continue
            if not any(candidate in label for candidate in lowered):
                continue
            if element.is_enabled():
                element.click()
                time.sleep(1.2)
                if idx % 2 == 0:
                    time.sleep(0.6)
        except Exception:
            continue


def create_stealth_driver(user_agent: Optional[str], headless: bool = True, timeout: int = 18):
    import shutil
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options 
    from selenium.webdriver.chrome.service import Service

    options = Options()
    chrome_path = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or None
    if chrome_path is None:
        raise Exception("Chrome not available")
    options.binary_location=chrome_path

    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1400,1200")
    options.add_argument("--lang=en-US")
    options.add_argument(f"user-agent={user_agent or random.choice(USER_AGENTS)}")

    try:
        from selenium_stealth import stealth  # type: ignore
    except Exception:  # pragma: no cover
        stealth = None

    try:
        # This call attempts default Chrome binary and chromedriver setup.
        service = Service(executable_path=shutil.which("chromedriver"))
        driver = webdriver.Chrome(service=service, options=options) # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "Failed to start Selenium Chrome driver. Ensure Chrome and chromedriver are installed."
        ) from exc

    driver.set_page_load_timeout(timeout)
    driver.set_script_timeout(timeout)

    if stealth is not None:
        stealth(
            driver,
            languages=["en-US", "en"],
            vendor="Google Inc.",
            platform="Win32",
            webgl_vendor="Intel Inc.",
            renderer="Intel Iris OpenGL Engine",
            fix_hairline=True,
        )

    return driver


def gather_from_selenium(
    match_urls: List[str],
    state: Dict[str, Any],
    user_agent: Optional[str],
    headless: bool,
    debug: bool,
) -> Dict[str, Any]:
    """Gather all static/map-context data and return updated state."""
    try:
        driver = create_stealth_driver(user_agent=user_agent, headless=headless)
    except Exception as exc:
        if debug:
            print(f"[selenium] failed to start: {exc}")
        return state

    try:
        for match_url in match_urls:
            if SHUTDOWN_REQUESTED:
                break

            try:
                if debug:
                    print(f"[selenium] opening match page: {match_url}")
                driver.get(match_url)
                time.sleep(1)

                # Initial snapshot
                html = driver.page_source
                parse_match_from_html(html, state)
                parse_match_from_browser_object(driver, state, debug)
                print_state(state, "selenium:init")

                # Trigger map/team context controls and re-parse.
                try_click_map_or_teams(driver, state)
                html = driver.page_source
                parse_match_from_html(html, state)
                parse_match_from_browser_object(driver, state, debug)

                if _has_real_data(state):
                    break
            except Exception as exc:
                if debug:
                    print(f"[selenium] page parse failure: {exc}")
                continue

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return state


def match_winner_goal(state: Dict[str, Any]) -> int:
    bo = state.get("bo")
    if not bo or bo <= 0:
        bo = 3
    return int((bo + 1) // 2)


def is_match_finished(state: Dict[str, Any]) -> bool:
    needed = match_winner_goal(state)
    return state.get("series_score_a", 0) >= needed or state.get("series_score_b", 0) >= needed


def derive_match_urls(match_id: str, raw_url: Optional[str]) -> List[str]:
    if raw_url and "csgopositive.com/en" in raw_url:
        return [raw_url.rstrip("/")]

    return ["https://www.csgopositive.com/en"]


def compute_map_key(state: Dict[str, Any]) -> Tuple[Optional[int], Optional[str]]:
    return state.get("map_number"), state.get("map")


def listen_match_events(
    match_state: Dict[str, Any],
    socket_url: str,
    events: List[str],
    wait_seconds: int,
    user_agent: Optional[str],
    proxy_url: Optional[str],
    disable_proxy: bool,
    headless: bool,
    match_urls: List[str],
    debug: bool,
) -> bool:
    try:
        import socketio  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("python-socketio missing. Install with: pip install python-socketio<5.0.0") from exc

    target_id = match_state["match_id"]
    socket_target, socketio_path = normalize_socket_target(socket_url)
    received = False
    payload_count = 0

    while not SHUTDOWN_REQUESTED:
        sio = socketio.Client(
            reconnection=False,
            logger=False,
            engineio_logger=False,
            request_timeout=wait_seconds + 5,
        )

        loop_state = {
            "error": None,
            "last_relevant": time.monotonic(),
        }

        def on_connected() -> None:
            if debug:
                sid = getattr(sio.eio, "sid", "n/a")
                print(f"[match={target_id}] connected sid={sid}")
            loop_state["last_relevant"] = time.monotonic()
            for event_name in ("match_connect", "join"):
                try:
                    sio.emit(event_name, {"id": target_id})
                except Exception as exc:
                    if debug:
                        print(f"[match={target_id}] emit {event_name} failed: {exc}")

        def on_disconnect() -> None:
            if debug and not SHUTDOWN_REQUESTED:
                print(f"[match={target_id}] websocket disconnected")

        def on_connect_error(data: Any) -> None:
            loop_state["error"] = data
            if debug:
                print(f"[match={target_id}] connect_error={data}")

        def on_error(data: Any) -> None:
            if debug:
                print(f"[match={target_id}] error={normalize_payload(data)}")

        def on_event(expected_event: str):
            def _handler(data: Any) -> None:
                nonlocal received, payload_count
                if SHUTDOWN_REQUESTED:
                    return

                payload_count += 1
                decoded = normalize_payload(data)

                effective_event = expected_event
                payload = decoded
                if isinstance(decoded, list) and len(decoded) >= 2 and isinstance(decoded[0], str):
                    effective_event = decoded[0]
                    payload = decoded[1]

                match_payload = filter_match_payload(payload, target_id)
                if match_payload is None:
                    return

                if effective_event == "score_change":
                    score_type = str(match_payload.get("type", "")).strip()
                    if score_type == "3":
                        effective_event = "score_change.current_map"
                    elif score_type == "1":
                        effective_event = "score_change.bo"

                loop_state["last_relevant"] = time.monotonic()
                received = True

                old_map_key = compute_map_key(match_state)

                if update_state_from_mapping(match_state, match_payload):
                    pass

                update_markets_from_dict(match_state, extract_markets(match_payload), f"ws:{effective_event}")

                print_state(match_state, f"ws:{effective_event}")
                match_state["updated_once"] = True

                new_map_key = compute_map_key(match_state)
                if new_map_key != old_map_key and not SHUTDOWN_REQUESTED:
                        # Refresh context with Selenium only when map changes.
                        if new_map_key != match_state.get("last_map_refresh_key"):
                            match_state["last_map_refresh_key"] = new_map_key
                            print(f"[match={target_id}] map changed from {old_map_key} to {new_map_key}, refreshing via selenium")
                            gather_from_selenium(match_urls, match_state, user_agent=user_agent, headless=headless, debug=debug)

            return _handler

        sio.on("connect", on_connected)
        sio.on("disconnect", on_disconnect)
        sio.on("error_msg", on_error)
        sio.on("connect_error", on_connect_error)

        for event_name in events:
            sio.on(event_name, on_event(event_name))

        proxy_state = set_proxy_env(proxy_url, should_override=(disable_proxy or proxy_url is not None))
        connected = False
        try:
            connect_kwargs = {
                "transports": ["polling", "websocket"],
                "headers": normalize_headers(user_agent),
            }
            if socketio_path:
                connect_kwargs["socketio_path"] = socketio_path

            if debug:
                print(f"[match={target_id}] connecting target={socket_target} events={','.join(events)}")

            sio.connect(socket_target, **connect_kwargs)
            connected = True

            while connected and not SHUTDOWN_REQUESTED:
                sio.sleep(1)

                # Refresh map on timeout and reconnect.
                if time.monotonic() - loop_state["last_relevant"] >= wait_seconds:
                    print(f"[match={target_id}] no relevant payload in {wait_seconds}s, reconnecting")
                    break

                # End condition after BO is done.
                if is_match_finished(match_state):
                    print(
                        f"[match={target_id}] match finished -> "
                        f"{match_state['team_a']} {match_state['series_score_a']} : {match_state['series_score_b']} {match_state['team_b']}"
                    )
                    break

        except Exception as exc:
            if not SHUTDOWN_REQUESTED:
                print(f"[match={target_id}] connection_error={exc}")
        finally:
            try:
                sio.disconnect()
            except Exception:
                pass
            restore_proxy_env(proxy_state)

        if received and (is_match_finished(match_state) or SHUTDOWN_REQUESTED):
            break

        if is_match_finished(match_state) or SHUTDOWN_REQUESTED:
            break

        time.sleep(0.5)

    return received


def parse_cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Track one CSGOPositive match: selenium start then websocket updates.")
    parser.add_argument("--match-id", required=True, help="Match ID from csgopositive")
    parser.add_argument(
        "--match-url",
        default=None,
        help="Optional /en csgopositive URL (all other base routes are ignored)",
    )
    parser.add_argument(
        "--seconds",
        type=int,
        default=10,
        help="If no relevant websocket payload for this many seconds, reconnect",
    )
    parser.add_argument("--user-agent", default=None, help="Fixed User-Agent to use")
    parser.add_argument("--proxy", default=None, help="Proxy URL for websocket connection")
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Disable proxy env vars for websocket attempt",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run selenium headless",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run selenium with visible browser window",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print additional debug output",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_cli()
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)

    events = ["koef_change", "score_change"]
    headless = not args.no_headless
    if args.headless:
        headless = True

    match_id = args.match_id
    match_urls = derive_match_urls(match_id, args.match_url)

    print(f"inspecting match={match_id} with websocket={SOCKET_URL}")

    match_state = ensure_state(match_id)
    match_state = gather_from_selenium(
        match_urls=match_urls,
        state=match_state,
        user_agent=args.user_agent,
        headless=headless,
        debug=args.debug,
    )
    match_state["last_map_refresh_key"] = compute_map_key(match_state)

    # Start websocket updates and continue until match done or user exits.
    got = listen_match_events(
        match_state=match_state,
        socket_url=SOCKET_URL,
        events=events,
        wait_seconds=args.seconds,
        user_agent=args.user_agent,
        proxy_url=args.proxy,
        disable_proxy=args.no_proxy,
        headless=headless,
        match_urls=match_urls,
        debug=args.debug,
    )

    if not SHUTDOWN_REQUESTED and not is_match_finished(match_state):
        print(f"no matching payload received for match={match_id} in {args.seconds}s")
    elif is_match_finished(match_state):
        print(f"match {match_id} done")

    if not got and not SHUTDOWN_REQUESTED:
        print("no data was received from websocket")


if __name__ == "__main__":
    main()
