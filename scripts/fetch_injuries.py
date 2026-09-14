"""
Weekly CFB injury report pipeline
===================================
Run by .github/workflows/injury-report.yml on a Thu/Fri/Sat schedule.

Pipeline:
  1. Ask CFBD which week is "current" and pull this week's FBS matchups.
  2. For each team playing this week, pull recent headlines from ESPN's
     unofficial news API (no official CFB injury feed exists — this is
     the same "sparse but real" source the app already spot-checks live).
  3. Run each headline through a structured-output LLM parse (the
     approach you found) to extract player/status/confidence.
  4. Write data/injuries.json — the web app fetches this file directly
     instead of calling ESPN or OpenAI itself (keeps your OpenAI key off
     the client, and means the app doesn't re-run this expensive pass on
     every single page load).

HONEST LIMITATION, carried over from the live spot-check we already
shipped: ESPN's college football news/injury coverage is thin. This
pipeline will often find few or zero real injury mentions for most
teams in a given week — that's the underlying data, not a bug in this
script. Treat the output as "whatever surfaced," not a comprehensive
report.

Required environment variables (set as GitHub repo secrets):
  OPENAI_API_KEY   — for the structured-output parse
  CFBD_API_KEY     — for determining the current week + matchups
"""

import json
import os
import time
from datetime import datetime, timezone
from typing import Optional, Literal

import requests
from pydantic import BaseModel, Field
from openai import OpenAI

CFBD_BASE = "https://api.collegefootballdata.com"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"

CFBD_KEY = os.environ["CFBD_API_KEY"]
CFBD_HEADERS = {"Authorization": f"Bearer {CFBD_KEY}"}

client = OpenAI()  # reads OPENAI_API_KEY from env automatically


# ============================================================
# 1. Structured output schema (your original design, unchanged)
# ============================================================

class InjuryReport(BaseModel):
    is_injury_news: bool = Field(description="True if the text contains CFB injury/availability updates, False otherwise.")
    player_name: Optional[str] = Field(None, description="Full name of the player.")
    team_name: Optional[str] = Field(None, description="Full team name (e.g., Georgia Bulldogs, Texas A&M).")
    position: Optional[str] = Field(None, description="Player position abbreviation (e.g., QB, LT, CB).")
    status: Optional[Literal["OUT", "QUESTIONABLE", "PROBABLE", "DOUBTFUL", "UNKNOWN"]] = Field(
        None, description="Standardized status based on the report."
    )
    body_part: Optional[str] = Field(None, description="Specific injury if mentioned (e.g., Hamstring, ACL, Ankle).")
    confidence_score: float = Field(
        description="Confidence from 0.0 to 1.0 that this report is verified news and not speculation."
    )


def parse_injury_text(headline_text: str) -> InjuryReport:
    system_prompt = (
        "You are an expert college football sports data parser. Your task is to extract "
        "player injury status updates from news headlines and short article blurbs. "
        "Only extract definitive updates or high-confidence practice observations. "
        "If the text isn't about an injury/availability status at all, set is_injury_news to False."
    )
    completion = client.beta.chat.completions.parse(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Extract injury data from this text:\n\n\"{headline_text}\""},
        ],
        response_format=InjuryReport,
    )
    return completion.choices[0].message.parsed


# ============================================================
# 2. Figure out the current week + this week's matchups (CFBD)
# ============================================================

def get_current_season_and_week() -> tuple[int, int]:
    """Uses CFBD's /calendar endpoint to find which week 'now' falls in."""
    now = datetime.now(timezone.utc)
    season = now.year if now.month >= 7 else now.year - 1  # CFB season named by its fall year

    resp = requests.get(f"{CFBD_BASE}/calendar", params={"year": season}, headers=CFBD_HEADERS, timeout=20)
    resp.raise_for_status()
    weeks = resp.json()

    for w in weeks:
        start = datetime.fromisoformat(w["firstGameStart"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(w["lastGameStart"].replace("Z", "+00:00"))
        if start <= now <= end:
            return season, w["week"]

    # Fallback: nearest upcoming week rather than crashing the whole run
    future_weeks = [w for w in weeks if datetime.fromisoformat(w["firstGameStart"].replace("Z", "+00:00")) > now]
    if future_weeks:
        return season, future_weeks[0]["week"]
    return season, weeks[-1]["week"] if weeks else 1


def get_this_week_teams(season: int, week: int) -> set[str]:
    resp = requests.get(
        f"{CFBD_BASE}/games", params={"year": season, "week": week}, headers=CFBD_HEADERS, timeout=20
    )
    resp.raise_for_status()
    games = resp.json()
    teams = set()
    for g in games:
        if g.get("homeTeam"):
            teams.add(g["homeTeam"])
        if g.get("awayTeam"):
            teams.add(g["awayTeam"])
    return teams


# ============================================================
# 3. ESPN team-ID lookup + news pull
# ============================================================
# NOTE: ESPN's API is unofficial and undocumented. The exact shape below
# is based on publicly documented reverse-engineering of their endpoints,
# not an official spec — if this starts failing, check
# https://github.com/pseudo-r/Public-ESPN-API for the current shape
# before assuming the pipeline itself is broken.

_espn_team_id_cache: Optional[dict[str, int]] = None


def get_espn_team_id_map() -> dict[str, int]:
    global _espn_team_id_cache
    if _espn_team_id_cache is not None:
        return _espn_team_id_cache

    resp = requests.get(f"{ESPN_BASE}/teams", params={"limit": 400}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    teams = data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])

    id_map = {}
    for t in teams:
        team = t.get("team", {})
        for candidate in [team.get("location"), team.get("name"), team.get("displayName"), team.get("shortDisplayName")]:
            if candidate:
                id_map[candidate.lower().strip()] = team.get("id")

    _espn_team_id_cache = id_map
    return id_map


def find_espn_team_id(team_name: str, id_map: dict[str, int]) -> Optional[int]:
    norm = team_name.lower().strip()
    if norm in id_map:
        return id_map[norm]
    for key, tid in id_map.items():
        if key.startswith(norm) or norm.startswith(key):
            return tid
    return None


def get_team_headlines(espn_team_id: int, limit: int = 5) -> list[str]:
    """Pull recent news headlines/descriptions for a team from ESPN's news feed."""
    try:
        resp = requests.get(
            f"{ESPN_BASE}/news",
            params={"team": espn_team_id, "limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
        headlines = []
        for a in articles:
            text = a.get("headline", "")
            if a.get("description"):
                text += ". " + a["description"]
            if text.strip():
                headlines.append(text.strip())
        return headlines
    except requests.RequestException:
        return []  # one team's feed failing shouldn't kill the whole run


# ============================================================
# 4. Main pipeline
# ============================================================

CONFIDENCE_THRESHOLD = 0.6  # drop low-confidence/speculative parses


def main():
    season, week = get_current_season_and_week()
    print(f"Season {season}, Week {week}")

    teams = sorted(get_this_week_teams(season, week))
    print(f"{len(teams)} teams playing this week")

    espn_id_map = get_espn_team_id_map()

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "season": season,
        "week": week,
        "teams": {},
    }

    for team_name in teams:
        espn_id = find_espn_team_id(team_name, espn_id_map)
        if espn_id is None:
            report["teams"][team_name] = {"matched": False, "injuries": []}
            continue

        headlines = get_team_headlines(espn_id)
        found = []

        for headline in headlines:
            try:
                parsed = parse_injury_text(headline)
            except Exception as e:
                print(f"  parse failed for {team_name}: {e}")
                continue

            if parsed.is_injury_news and parsed.confidence_score >= CONFIDENCE_THRESHOLD:
                found.append({
                    "player_name": parsed.player_name,
                    "position": parsed.position,
                    "status": parsed.status,
                    "body_part": parsed.body_part,
                    "confidence_score": parsed.confidence_score,
                    "source_headline": headline,
                })

            time.sleep(0.2)  # gentle pacing against both ESPN and OpenAI

        report["teams"][team_name] = {"matched": True, "injuries": found}
        if found:
            print(f"  {team_name}: {len(found)} injury mention(s) found")

    os.makedirs("data", exist_ok=True)
    with open("data/injuries.json", "w") as f:
        json.dump(report, f, indent=2)

    total_found = sum(len(t["injuries"]) for t in report["teams"].values())
    print(f"\nDone. {total_found} total injury mentions across {len(teams)} teams.")
    print("Expect this number to be small — see the module docstring for why.")


if __name__ == "__main__":
    main()
