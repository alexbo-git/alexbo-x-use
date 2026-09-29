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
import base64
import hmac
import html
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from selenium.webdriver.common.by import By
from xuse.core.browser_manager import BrowserManager

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
COOKIE_DIR = DATA_DIR / "cookies"
METRICS_DIR = DATA_DIR / "metrics"
CONFIG_DIR = ROOT / "config"
STATE_FILE = DATA_DIR / "worker_state.json"
CONTROL_FILE = DATA_DIR / "worker_control.json"
ACCOUNT_FILE = CONFIG_DIR / "accounts.json"
GROWTH_DIR = DATA_DIR / "growth"

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
PORT = int(os.getenv("PORT", "8080"))

ACCOUNT_ID = os.getenv("X_ACCOUNT_ID", "kalyvox").strip() or "kalyvox"
GROWTH_FILE = GROWTH_DIR / f"{ACCOUNT_ID}.jsonl"
TZ = ZoneInfo(os.getenv("WORKER_TIMEZONE", "Europe/Paris"))
START_HOUR = int(os.getenv("WORKER_START_HOUR", "8"))
END_HOUR = int(os.getenv("WORKER_END_HOUR", "22"))
MIN_INTERVAL = int(os.getenv("WORKER_MIN_INTERVAL_MINUTES", "20"))
MAX_INTERVAL = int(os.getenv("WORKER_MAX_INTERVAL_MINUTES", "40"))
REPLY_TARGET = int(os.getenv("WORKER_REPLY_DAILY_TARGET", "20"))
LIKE_TARGET = int(os.getenv("WORKER_LIKE_DAILY_TARGET", "30"))
KEYWORDS_PER_CYCLE = max(1, int(os.getenv("WORKER_KEYWORDS_PER_CYCLE", "2")))

LANES = {
    "smb_verticals": {
        "weight": 35,
        "reply_cap": 8,
        "keywords": [
            "home service business",
            "HVAC business",
            "plumbing business",
            "dental practice growth",
            "law firm automation",
            "property management automation",
            "small business operations",
            "local service business growth"
        ],
    },
    "business_pain": {
        "weight": 25,
        "reply_cap": 6,
        "keywords": [
            "inbound leads",
            "lead response time",
            "customer service automation",
            "customer experience",
            "appointment booking",
            "after hours customer service",
            "missed leads",
            "sales automation"
        ],
    },
    "voice_ai": {
        "weight": 20,
        "reply_cap": 5,
        "keywords": [
            "AI receptionist",
            "virtual receptionist",
            "AI answering service",
            "voice AI",
            "AI phone agent",
            "AI call automation"
        ],
    },
    "saas_builders": {
        "weight": 15,
        "reply_cap": 3,
        "keywords": [
            "AI agents",
            "AI automation",
            "SaaS founders",
            "indie hackers",
            "building SaaS"
        ],
    },
    "seo_geo": {
        "weight": 5,
        "reply_cap": 2,
        "keywords": [
            "SEO SaaS",
            "GEO SEO",
            "AI search optimization"
        ],
    },
}

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


def lane_definitions() -> dict:
    raw = os.getenv("X_TARGET_KEYWORDS", "").strip()
    if raw:
        parsed = [item.strip() for item in raw.split(";") if item.strip()]
        if parsed:
            return {
                "custom": {
                    "weight": 100,
                    "reply_cap": REPLY_TARGET,
                    "keywords": parsed,
                }
            }
    return LANES


def choose_lane(state: dict, lanes: dict) -> tuple[str, dict]:
    lane_stats = state.setdefault("lanes", {})
    eligible = []
    weights = []
    for name, cfg in lanes.items():
        stats = lane_stats.setdefault(name, {"replies": 0, "likes": 0, "cycles": 0})
        cap = int(cfg.get("reply_cap", REPLY_TARGET))
        if stats.get("replies", 0) >= cap:
            continue
        eligible.append((name, cfg))
        weights.append(max(1, int(cfg.get("weight", 1))))
    if not eligible:
        eligible = list(lanes.items())
        weights = [max(1, int(cfg.get("weight", 1))) for _, cfg in eligible]
    return random.choices(eligible, weights=weights, k=1)[0]


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
        "lanes": {},
        "growth_snapshot_date": None,
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


def update_state_from_metrics(
    state: dict,
    before: dict[str, int],
    after: dict[str, int],
    lane_name: str | None = None,
) -> None:
    reply_delta = max(0, after["replies"] - before["replies"])
    like_delta = max(0, after["likes"] - before["likes"])
    error_delta = max(0, after["errors"] - before["errors"])
    state["replies"] += reply_delta
    state["likes"] += like_delta
    state["errors"] += error_delta
    if lane_name:
        lane = state.setdefault("lanes", {}).setdefault(
            lane_name, {"replies": 0, "likes": 0, "cycles": 0}
        )
        lane["replies"] += reply_delta
        lane["likes"] += like_delta


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



def load_control() -> dict:
    default = {"paused": False, "run_now": False}
    if not CONTROL_FILE.exists():
        return default
    try:
        data = json.loads(CONTROL_FILE.read_text(encoding="utf-8"))
        return {**default, **data}
    except Exception:
        return default


def save_control(control: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONTROL_FILE.write_text(json.dumps(control, indent=2), encoding="utf-8")


def interruptible_sleep(seconds: int) -> None:
    deadline = time.time() + max(0, seconds)
    while time.time() < deadline:
        control = load_control()
        if control.get("run_now"):
            return
        time.sleep(min(10, max(0, deadline - time.time())))


def _auth_ok(header: str | None) -> bool:
    if not ADMIN_PASSWORD:
        return False
    if not header or not header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        _, password = raw.split(":", 1)
        return hmac.compare_digest(password, ADMIN_PASSWORD)
    except Exception:
        return False



def _parse_social_count(text: str | None) -> int | None:
    if not text:
        return None
    s = text.strip().replace(",", "").replace("\u202f", "").replace(" ", "")
    import re
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)([KkMm]?)", s)
    if not m:
        return None
    value = float(m.group(1))
    suffix = m.group(2).lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return int(round(value))


def read_growth_history(limit: int = 60) -> list[dict]:
    if not GROWTH_FILE.exists():
        return []
    try:
        rows = []
        for line in GROWTH_FILE.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        return rows[-max(1, limit):]
    except Exception:
        log.exception("Could not read growth history")
        return []


def _append_growth_snapshot(snapshot: dict) -> None:
    GROWTH_DIR.mkdir(parents=True, exist_ok=True)
    history = read_growth_history(500)
    today = snapshot.get("date")
    # Keep one latest snapshot per local day to make 1d/7d comparisons stable.
    if history and history[-1].get("date") == today:
        history[-1] = snapshot
        GROWTH_FILE.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in history),
            encoding="utf-8",
        )
        return
    with GROWTH_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(snapshot, ensure_ascii=False) + "\n")


def capture_growth_snapshot(cookie_path: Path) -> dict | None:
    account = {
        "account_id": ACCOUNT_ID,
        "is_active": True,
        "cookie_file_path": str(cookie_path.relative_to(ROOT)),
        "proxy": os.getenv("X_PROXY") or None,
    }
    manager = BrowserManager(account_config=account)
    try:
        driver = manager.get_driver()
        handle = (os.getenv("X_HANDLE") or manager.logged_in_handle or "").strip().lstrip("@")
        if not handle:
            log.warning("Growth snapshot skipped: could not determine X handle; set X_HANDLE if needed")
            return None

        driver.get(f"https://x.com/{handle}")
        time.sleep(2)

        followers = None
        following = None
        for anchor in driver.find_elements(By.XPATH, "//a[@href]"):
            href = anchor.get_attribute("href") or ""
            text_value = anchor.text or anchor.get_attribute("aria-label") or ""
            if href.rstrip("/").endswith(f"/{handle}/followers"):
                followers = _parse_social_count(text_value)
            elif href.rstrip("/").endswith(f"/{handle}/following"):
                following = _parse_social_count(text_value)

        if followers is None:
            # X sometimes points the count link at verified_followers.
            for anchor in driver.find_elements(By.XPATH, "//a[contains(@href, '/verified_followers')]"):
                followers = _parse_social_count(anchor.text or anchor.get_attribute("aria-label"))
                if followers is not None:
                    break

        if followers is None and following is None:
            log.warning("Growth snapshot could not parse followers/following from profile")
            return None

        snapshot = {
            "ts": datetime.now(TZ).isoformat(),
            "date": datetime.now(TZ).date().isoformat(),
            "handle": handle,
            "followers": followers,
            "following": following,
            "replies_total": read_metrics().get("replies", 0),
            "likes_total": read_metrics().get("likes", 0),
        }
        _append_growth_snapshot(snapshot)
        log.info("Growth snapshot: followers=%s following=%s", followers, following)
        return snapshot
    except Exception:
        log.exception("Growth snapshot failed")
        return None
    finally:
        manager.close_driver()


def growth_summary() -> dict:
    history = read_growth_history(60)
    if not history:
        return {
            "current": None,
            "delta_1d": None,
            "delta_7d": None,
            "followers_per_10_replies_7d": None,
            "history": [],
        }

    current = history[-1]

    def baseline(days: int) -> dict | None:
        target = datetime.now(TZ).date() - timedelta(days=days)
        candidates = [row for row in history if row.get("date") <= target.isoformat()]
        return candidates[-1] if candidates else None

    b1 = baseline(1)
    b7 = baseline(7)

    def follower_delta(base):
        if not base or current.get("followers") is None or base.get("followers") is None:
            return None
        return int(current["followers"]) - int(base["followers"])

    delta7 = follower_delta(b7)
    eff = None
    if b7 and delta7 is not None:
        replies_delta = int(current.get("replies_total", 0)) - int(b7.get("replies_total", 0))
        if replies_delta > 0:
            eff = round(delta7 * 10 / replies_delta, 2)

    return {
        "current": current,
        "delta_1d": follower_delta(b1),
        "delta_7d": delta7,
        "followers_per_10_replies_7d": eff,
        "history": history[-30:],
    }


def read_activity(limit: int = 50) -> list[dict]:
    path = DATA_DIR / "activity" / f"{ACCOUNT_ID}.jsonl"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        items = []
        for line in lines[-max(1, limit):]:
            try:
                items.append(json.loads(line))
            except Exception:
                continue
        items.reverse()
        return items
    except Exception:
        log.exception("Could not read activity log")
        return []


def _dashboard_payload() -> dict:
    state = load_state()
    control = load_control()
    metrics = read_metrics()
    account = {}
    try:
        accounts = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
        if accounts:
            account = accounts[0]
    except Exception:
        pass

    return {
        "account": ACCOUNT_ID,
        "paused": bool(control.get("paused")),
        "run_now": bool(control.get("run_now")),
        "today": state,
        "cumulative_metrics": metrics,
        "targets": {"replies": REPLY_TARGET, "likes": LIKE_TARGET},
        "hours": {"start": START_HOUR, "end": END_HOUR, "timezone": TZ.key},
        "current_keywords": account.get("target_keywords_override", []),
        "recent_activity": read_activity(30),
        "lanes": state.get("lanes", {}),
        "lane_config": {
            name: {"weight": cfg["weight"], "reply_cap": cfg["reply_cap"]}
            for name, cfg in lane_definitions().items()
        },
        "growth": growth_summary(),
    }


class AdminHandler(BaseHTTPRequestHandler):
    server_version = "KalyvoxXAdmin/1.0"

    def log_message(self, fmt, *args):
        log.info("admin: " + fmt, *args)

    def _require_auth(self) -> bool:
        if _auth_ok(self.headers.get("Authorization")):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Kalyvox X Agent"')
        self.end_headers()
        return False

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, path: str = "/") -> None:
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._json({"ok": True})
            return
        if not self._require_auth():
            return
        if self.path == "/status":
            self._json(_dashboard_payload())
            return
        if self.path == "/activity":
            self._json({"activity": read_activity(100)})
            return
        if self.path == "/growth":
            self._json(growth_summary())
            return
        if self.path != "/":
            self._json({"error": "not found"}, 404)
            return

        p = _dashboard_payload()
        paused = "PAUSED" if p["paused"] else "RUNNING"
        kws = ", ".join(p["current_keywords"]) or "none yet"
        html = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kalyvox X Agent</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;background:#0b1020;color:#e8ecf4}}
.card{{background:#151c2f;border:1px solid #28324b;border-radius:14px;padding:18px;margin:14px 0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}}
.big{{font-size:28px;font-weight:700}} small{{color:#9aa6bd}}
button{{padding:10px 16px;border-radius:9px;border:0;font-weight:700;cursor:pointer;margin-right:8px}}
.run{{background:#58d68d}} .pause{{background:#ff7675}} .resume{{background:#74b9ff}}
code{{word-break:break-word}}
</style></head><body>
<h1>Kalyvox X Agent</h1>
<div class="card"><div class="big">{paused}</div><small>{p["hours"]["start"]}:00–{p["hours"]["end"]}:00 · {p["hours"]["timezone"]}</small></div>
<div class="grid">
<div class="card"><small>Replies today</small><div class="big">{p["today"]["replies"]} / {REPLY_TARGET}</div></div>
<div class="card"><small>Likes today</small><div class="big">{p["today"]["likes"]} / {LIKE_TARGET}</div></div>
<div class="card"><small>Cycles</small><div class="big">{p["today"]["cycles"]}</div></div>
<div class="card"><small>Errors today</small><div class="big">{p["today"]["errors"]}</div></div>
</div>
<div class="card"><small>Growth</small>
<div class="grid" style="margin-top:10px">
<div><small>Followers</small><div class="big">{(p["growth"]["current"] or {}).get("followers", "—")}</div></div>
<div><small>Δ 1 day</small><div class="big">{p["growth"]["delta_1d"] if p["growth"]["delta_1d"] is not None else "—"}</div></div>
<div><small>Δ 7 days</small><div class="big">{p["growth"]["delta_7d"] if p["growth"]["delta_7d"] is not None else "—"}</div></div>
<div><small>Followers / 10 replies (7d)</small><div class="big">{p["growth"]["followers_per_10_replies_7d"] if p["growth"]["followers_per_10_replies_7d"] is not None else "—"}</div></div>
</div>
<div style="margin-top:10px"><small>30d history</small><div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;margin-top:6px">
<tr><th align="left">Date</th><th align="right">Followers</th><th align="right">Following</th></tr>
{"".join(
    f'<tr><td>{html.escape(str(row.get("date","")))}</td><td align="right">{html.escape(str(row.get("followers","—")))}</td><td align="right">{html.escape(str(row.get("following","—")))}</td></tr>'
    for row in reversed(p["growth"]["history"][-10:])
)}
</table></div></div>
</div>
<div class="card"><small>Current keywords</small><p><code>{html.escape(kws)}</code></p></div>
<div class="card"><small>Lane mix today</small>
<div style="margin-top:10px">
{"".join(
    f'<div style="padding:6px 0"><b>{html.escape(name)}</b> — '
    f'{int((p["lanes"].get(name) or {}).get("replies", 0))} replies / {int(cfg.get("reply_cap", 0))} cap · '
    f'{int((p["lanes"].get(name) or {}).get("likes", 0))} likes · '
    f'{int((p["lanes"].get(name) or {}).get("cycles", 0))} cycles</div>'
    for name, cfg in p["lane_config"].items()
)}
</div></div>
<div class="card"><small>Recent activity</small>
<div style="margin-top:10px">
{"".join(
    f'<div style="padding:10px 0;border-top:1px solid #28324b">'
    f'<b>{html.escape(str(item.get("action", ""))).upper()}</b> '
    f'<small>{html.escape(str(item.get("result", "")))} · {html.escape(str(item.get("ts", "")))}</small>'
    f'<div style="margin-top:6px">{html.escape(str((item.get("meta") or {}).get("original_text") or ""))}</div>'
    + (
        f'<div style="margin-top:6px;color:#74b9ff">↳ {html.escape(str((item.get("meta") or {}).get("reply_text") or ""))}</div>'
        if (item.get("meta") or {}).get("reply_text") else ""
      )
    + (
        f'<div style="margin-top:5px"><a style="color:#9ecbff" href="{html.escape(str((item.get("meta") or {}).get("tweet_url")))}" target="_blank" rel="noopener">Open on X</a></div>'
        if (item.get("meta") or {}).get("tweet_url") else ""
      )
    + '</div>'
    for item in p["recent_activity"]
) or '<div style="padding-top:8px"><small>No activity yet.</small></div>'}
</div></div>
<div class="card">
<form method="post" action="/run-now" style="display:inline"><button class="run">Run now</button></form>
<form method="post" action="/pause" style="display:inline"><button class="pause">Pause</button></form>
<form method="post" action="/resume" style="display:inline"><button class="resume">Resume</button></form>
</div>
</body></html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self._require_auth():
            return
        control = load_control()
        if self.path == "/pause":
            control["paused"] = True
        elif self.path == "/resume":
            control["paused"] = False
        elif self.path == "/run-now":
            control["paused"] = False
            control["run_now"] = True
        else:
            self._json({"error": "not found"}, 404)
            return
        save_control(control)
        self._redirect("/")


def start_admin_server() -> None:
    if not ADMIN_PASSWORD:
        log.warning("ADMIN_PASSWORD is not set: admin web UI disabled")
        return
    server = ThreadingHTTPServer(("0.0.0.0", PORT), AdminHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Admin UI listening on 0.0.0.0:%s", PORT)


def main() -> None:
    env_required("OPENAI_API_KEY")
    start_admin_server()
    cookie_path = write_cookie_file()
    lanes = lane_definitions()
    if not lanes:
        raise RuntimeError("No targeting lanes configured")

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
        control = load_control()

        if control.get("paused"):
            log.info("Worker manually paused")
            interruptible_sleep(30)
            continue

        force_run = bool(control.get("run_now"))
        if force_run:
            control["run_now"] = False
            save_control(control)
            log.info("Manual run-now requested")

        pause_until = state.get("pause_until")
        if pause_until:
            try:
                pause_dt = datetime.fromisoformat(pause_until)
                if now < pause_dt:
                    sleep_s = min(1800, max(60, int((pause_dt - now).total_seconds())))
                    log.warning("Worker paused after errors until %s", pause_dt.isoformat())
                    interruptible_sleep(sleep_s)
                    continue
                state["pause_until"] = None
                save_state(state)
            except ValueError:
                state["pause_until"] = None

        if not force_run and not in_active_window(now):
            sleep_s = seconds_until_next_window(now)
            log.info("Outside active window; sleeping %.1f minutes", sleep_s / 60)
            interruptible_sleep(min(sleep_s, 1800))
            continue

        today_iso = now.date().isoformat()
        if state.get("growth_snapshot_date") != today_iso:
            if capture_growth_snapshot(cookie_path):
                state["growth_snapshot_date"] = today_iso
                save_state(state)

        if state["replies"] >= REPLY_TARGET and state["likes"] >= LIKE_TARGET:
            log.info(
                "Daily targets reached: replies=%s/%s likes=%s/%s",
                state["replies"], REPLY_TARGET, state["likes"], LIKE_TARGET,
            )
            interruptible_sleep(1800)
            continue

        lane_name, lane_cfg = choose_lane(state, lanes)
        lane_keywords = lane_cfg["keywords"]
        selected = random.sample(
            lane_keywords, k=min(KEYWORDS_PER_CYCLE, len(lane_keywords))
        )
        write_account_config(selected, cookie_path)
        state.setdefault("lanes", {}).setdefault(
            lane_name, {"replies": 0, "likes": 0, "cycles": 0}
        )["cycles"] += 1
        save_state(state)
        log.info("Cycle lane=%s keywords=%s", lane_name, " | ".join(selected))

        before = read_metrics()
        return_codes: list[int] = []

        if state["replies"] < REPLY_TARGET:
            return_codes.append(run_pipeline("keyword_replies"))

        middle = read_metrics()
        update_state_from_metrics(state, before, middle, lane_name)

        if state["likes"] < LIKE_TARGET:
            return_codes.append(run_pipeline("likes"))

        after = read_metrics()
        update_state_from_metrics(state, middle, after, lane_name)
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
            interruptible_sleep(300)
            continue

        interval = random.randint(MIN_INTERVAL, MAX_INTERVAL)
        log.info("Next cycle in %s minutes", interval)
        interruptible_sleep(interval * 60)


if __name__ == "__main__":
    main()
