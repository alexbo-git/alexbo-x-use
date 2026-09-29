#!/usr/bin/env python3
"""Autonomous Railway worker for Kalyvox X engagement.

Posts are intentionally out of scope: Buffer owns original publishing.
This worker only runs keyword replies and likes.
"""

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
COOKIE_DIR = DATA_DIR / "cookies"
METRICS_DIR = DATA_DIR / "metrics"
CONFIG_DIR = ROOT / "config"
STATE_FILE = DATA_DIR / "worker_state.json"
ACCOUNT_FILE = CONFIG_DIR / "accounts.json"

ACCOUNT_ID = os.getenv("X_ACCOUNT_ID", "kalyvox").strip() or "kalyvox"
TZ = ZoneInfo(os.getenv("WORKER_TIMEZONE", "Europe/Paris"))
START_HOUR = int(os.getenv("WORKER_START_HOUR", "8"))
END_HOUR = int(os.getenv("WORKER_END_HOUR", "22"))
MIN_INTERVAL = int(os.getenv("WORKER_MIN_INTERVAL_MINUTES", "20"))
MAX_INTERVAL = int(os.getenv("WORKER_MAX_INTERVAL_MINUTES", "40"))
REPLY_TARGET = int(os.getenv("WORKER_REPLY_DAILY_TARGET", "20"))
LIKE_TARGET = int(os.getenv("WORKER_LIKE_DAILY_TARGET", "30"))
KEYWORDS_PER_CYCLE = max(1, int(os.getenv("WORKER_KEYWORDS_PER_CYCLE", "2")))

DEFAULT_KEYWORDS = [
    "AI agents",
    "AI automation",
    "SaaS founders",
    "indie hackers",
    "building SaaS",
    "small business automation",
    "small business growth",
    "customer service automation",
    "customer experience",
    "sales automation",
    "inbound leads",
    "lead response time",
    "appointment booking",
    "AI receptionist",
    "virtual receptionist",
    "AI answering service",
    "voice AI",
    "AI phone agent",
    "home service business",
    "HVAC business",
    "plumbing business",
    "dental practice growth",
    "law firm automation",
    "property management automation",
    "SEO SaaS",
    "GEO SEO"
]

DEFAULT_PERSONA = """You are Alex, founder of Kalyvox, an AI receptionist / phone-answering SaaS for small businesses.
You are a SaaS builder who works hands-on on acquisition, SEO/GEO, AI agents, customer experience, inbound lead handling and automation.

Engagement goals:
- Build a relevant network around SaaS builders, AI operators, SMB operators, growth/SEO people and voice-AI practitioners.
- Add something useful to the conversation: concrete experience, nuance, a practical observation, informed disagreement, or a sharp question.
- Sound like a real founder, not a corporate brand account or engagement bot.
- Keep replies concise and conversational.
- Never use generic praise such as "Great post", "Love this", or "Thanks for sharing".
- Do not mention Kalyvox unless it is genuinely relevant to the discussion.
- Do not insert promotional links.
- Do not manufacture facts, customer results, metrics or personal experiences.
- Avoid hashtags and emojis unless they are genuinely natural.
- It is fine to engage with adjacent SaaS, SEO, AI and small-business topics; do not restrict yourself to phone-call discussions.
"""

logging.basicConfig(
    level=os.getenv("WORKER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("railway-worker")


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def keywords() -> list[str]:
    raw = os.getenv("X_TARGET_KEYWORDS", "").strip()
    if raw:
        parsed = [item.strip() for item in raw.split(";") if item.strip()]
        if parsed:
            return parsed
    return DEFAULT_KEYWORDS


def persona() -> str:
    return os.getenv("X_PERSONA", "").strip() or DEFAULT_PERSONA


def write_cookie_file() -> Path:
    auth_token = env_required("X_AUTH_TOKEN")
    ct0 = env_required("X_CT0")
    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    cookie_path = COOKIE_DIR / f"{ACCOUNT_ID}.json"
    payload = [
        {
            "name": "auth_token",
            "value": auth_token,
            "domain": ".x.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        },
        {
            "name": "ct0",
            "value": ct0,
            "domain": ".x.com",
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        },
    ]
    cookie_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.chmod(cookie_path, 0o600)
    return cookie_path


def write_account_config(selected_keywords: list[str], cookie_path: Path) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    account = {
        "account_id": ACCOUNT_ID,
        "is_active": True,
        "cookie_file_path": str(cookie_path.relative_to(ROOT)),
        "proxy": os.getenv("X_PROXY") or None,
        "post_to_community": False,
        "target_keywords_override": selected_keywords,
        "competitor_profiles_override": [],
        "persona": persona(),
        "action_config_override": {
            "min_delay_between_actions_seconds": 90,
            "max_delay_between_actions_seconds": 240,
            "enable_competitor_reposts": False,
            "max_posts_per_competitor_run": 0,
            "enable_keyword_replies": True,
            "max_replies_per_keyword_run": 1,
            "reply_only_to_recent_tweets_hours": 8,
            "avoid_replying_to_own_tweets": True,
            "enable_content_curation_posts": False,
            "max_curated_posts_per_run": 0,
            "enable_liking_tweets": True,
            "max_likes_per_run": 3,
            "like_tweets_from_feed": False,
            "enable_thread_analysis": True,
            "enable_relevance_filter_keyword_replies": True,
            "relevance_threshold_keyword_replies": 0.58,
            "enable_relevance_filter_likes": True,
            "relevance_threshold_likes": 0.45,
            "enable_keyword_retweets": False,
            "max_retweets_per_keyword_run": 0,
            "enable_community_engagement": False,
            "enable_community_likes": False,
            "enable_community_retweets": False,
            "max_community_engagements_per_run": 0,
            "enable_community_replies": False,
            "max_community_replies_per_run": 0,
        },
    }
    ACCOUNT_FILE.write_text(json.dumps([account], indent=2), encoding="utf-8")


def read_metrics() -> dict[str, int]:
    path = METRICS_DIR / f"{ACCOUNT_ID}.json"
    if not path.exists():
        return {"replies": 0, "likes": 0, "errors": 0}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        counters = payload.get("counters", {})
        return {
            "replies": int(counters.get("replies", 0)),
            "likes": int(counters.get("likes", 0)),
            "errors": int(counters.get("errors", 0)),
        }
    except Exception:
        log.exception("Could not read metrics file")
        return {"replies": 0, "likes": 0, "errors": 0}


def load_state() -> dict:
    today = datetime.now(TZ).date().isoformat()
    default = {
        "date": today,
        "replies": 0,
        "likes": 0,
        "errors": 0,
        "cycles": 0,
        "pause_until": None,
    }
    if not STATE_FILE.exists():
        return default
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if state.get("date") != today:
            return default
        return {**default, **state}
    except Exception:
        return default


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def run_pipeline(name: str) -> int:
    log.info("Starting pipeline=%s account=%s", name, ACCOUNT_ID)
    result = subprocess.run(
        ["x-use", "run", "--account", ACCOUNT_ID, "--pipeline", name],
        cwd=ROOT,
        check=False,
    )
    log.info("Finished pipeline=%s exit=%s", name, result.returncode)
    return result.returncode


def update_state_from_metrics(state: dict, before: dict[str, int], after: dict[str, int]) -> None:
    state["replies"] += max(0, after["replies"] - before["replies"])
    state["likes"] += max(0, after["likes"] - before["likes"])
    state["errors"] += max(0, after["errors"] - before["errors"])


def in_active_window(now: datetime) -> bool:
    return START_HOUR <= now.hour < END_HOUR


def seconds_until_next_window(now: datetime) -> int:
    if now.hour < START_HOUR:
        target = now.replace(hour=START_HOUR, minute=0, second=0, microsecond=0)
    else:
        target = (now + timedelta(days=1)).replace(
            hour=START_HOUR, minute=0, second=0, microsecond=0
        )
    return max(60, int((target - now).total_seconds()))


def main() -> None:
    env_required("OPENAI_API_KEY")
    cookie_path = write_cookie_file()
    pool = keywords()
    if not pool:
        raise RuntimeError("No target keywords configured")

    log.info(
        "Kalyvox X worker started: replies/day=%s likes/day=%s active=%02d:00-%02d:00 %s",
        REPLY_TARGET,
        LIKE_TARGET,
        START_HOUR,
        END_HOUR,
        TZ.key,
    )

    while True:
        now = datetime.now(TZ)
        state = load_state()

        pause_until = state.get("pause_until")
        if pause_until:
            try:
                pause_dt = datetime.fromisoformat(pause_until)
                if now < pause_dt:
                    sleep_s = min(1800, max(60, int((pause_dt - now).total_seconds())))
                    log.warning("Worker paused after errors until %s", pause_dt.isoformat())
                    time.sleep(sleep_s)
                    continue
                state["pause_until"] = None
                save_state(state)
            except ValueError:
                state["pause_until"] = None

        if not in_active_window(now):
            sleep_s = seconds_until_next_window(now)
            log.info("Outside active window; sleeping %.1f minutes", sleep_s / 60)
            time.sleep(min(sleep_s, 1800))
            continue

        if state["replies"] >= REPLY_TARGET and state["likes"] >= LIKE_TARGET:
            log.info(
                "Daily targets reached: replies=%s/%s likes=%s/%s",
                state["replies"], REPLY_TARGET, state["likes"], LIKE_TARGET,
            )
            time.sleep(1800)
            continue

        selected = random.sample(pool, k=min(KEYWORDS_PER_CYCLE, len(pool)))
        write_account_config(selected, cookie_path)
        log.info("Cycle keywords: %s", " | ".join(selected))

        before = read_metrics()
        return_codes: list[int] = []

        if state["replies"] < REPLY_TARGET:
            return_codes.append(run_pipeline("keyword_replies"))

        middle = read_metrics()
        update_state_from_metrics(state, before, middle)

        if state["likes"] < LIKE_TARGET:
            return_codes.append(run_pipeline("likes"))

        after = read_metrics()
        update_state_from_metrics(state, middle, after)
        state["cycles"] += 1
        save_state(state)

        error_delta = max(0, after["errors"] - before["errors"])
        log.info(
            "Daily progress: replies=%s/%s likes=%s/%s errors=%s cycles=%s",
            state["replies"], REPLY_TARGET,
            state["likes"], LIKE_TARGET,
            state["errors"], state["cycles"],
        )

        if any(code != 0 for code in return_codes) or error_delta >= 3:
            pause_hours = int(os.getenv("WORKER_ERROR_PAUSE_HOURS", "2"))
            pause_dt = datetime.now(TZ) + timedelta(hours=pause_hours)
            state["pause_until"] = pause_dt.isoformat()
            save_state(state)
            log.warning("Error threshold reached; pausing until %s", pause_dt.isoformat())
            time.sleep(300)
            continue

        interval = random.randint(MIN_INTERVAL, MAX_INTERVAL)
        log.info("Next cycle in %s minutes", interval)
        time.sleep(interval * 60)


if __name__ == "__main__":
    main()
