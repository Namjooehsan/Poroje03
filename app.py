import os
import json
import sqlite3
import hashlib
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("global-post-engine")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

APP_SECRET = os.getenv("APP_SECRET", "")
API_TOKEN = os.getenv("METRICOOL_API_TOKEN", "")
USER_ID = os.getenv("METRICOOL_USER_ID", "")
BLOG_ID = os.getenv("METRICOOL_BLOG_ID", "")
TZ_NAME = os.getenv("METRICOOL_TIMEZONE", "Asia/Tehran")
GLOBAL_PROVIDERS = [x.strip().lower() for x in os.getenv("GLOBAL_PROVIDERS", "facebook,instagram").split(",") if x.strip()]
IRAN_PROVIDERS = [x.strip().lower() for x in os.getenv("IRAN_PROVIDERS", "").split(",") if x.strip()]
AUTO_PUBLISH = os.getenv("AUTO_PUBLISH", "true").lower() == "true"
DB_PATH = os.getenv("DATABASE_PATH", "posts.db")
METRICOOL_URL = "https://app.metricool.com/api/v2/scheduler/posts"


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT NOT NULL UNIQUE,
            text TEXT NOT NULL,
            scope TEXT NOT NULL,
            media TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            result TEXT NOT NULL DEFAULT '{}'
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS scheduled (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            wave INTEGER NOT NULL,
            provider TEXT NOT NULL,
            publish_at TEXT NOT NULL,
            status TEXT NOT NULL,
            result TEXT NOT NULL DEFAULT '{}',
            UNIQUE(post_id, wave, provider)
        )""")


def make_variants(text):
    clean = " ".join(text.split())
    return [
        clean,
        "نکته مهم: " + clean,
        clean + "\n\nاگر این موضوع برایتان مفید بود، با دیگران به اشتراک بگذارید."
    ]


def metricool_schedule(text, media, provider, publish_at):
    if not all([API_TOKEN, USER_ID, BLOG_ID]):
        raise RuntimeError("Metricool API credentials are not configured")
    payload = {
        "publicationDate": {
            "dateTime": publish_at.strftime("%Y-%m-%dT%H:%M:%S"),
            "timezone": TZ_NAME
        },
        "text": text,
        "providers": [{"network": provider}],
        "media": media,
        "autoPublish": AUTO_PUBLISH,
        "draft": not AUTO_PUBLISH,
        "saveExternalMediaFiles": True
    }
    headers = {"X-Mc-Auth": API_TOKEN, "Content-Type": "application/json"}
    response = requests.post(
        METRICOOL_URL,
        params={"blogId": BLOG_ID, "userId": USER_ID},
        headers=headers, json=payload, timeout=30
    )
    response.raise_for_status()
    return response.json()


def save_post(text, scope, media):
    digest = hashlib.sha256(
        (scope + "|" + text + "|" + json.dumps(media, ensure_ascii=False)).encode("utf-8")
    ).hexdigest()
    with db() as conn:
        existing = conn.execute("SELECT id FROM posts WHERE content_hash=?", (digest,)).fetchone()
        if existing:
            return existing["id"], False
        cur = conn.execute(
            "INSERT INTO posts(content_hash,text,scope,media,status,created_at) VALUES(?,?,?,?,?,?)",
            (digest, text, scope, json.dumps(media), "queued", datetime.utcnow().isoformat())
        )
        return cur.lastrowid, True


def schedule_campaign(post_id):
    with db() as conn:
        post = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if not post:
        raise ValueError("post not found")

    scope = post["scope"]
    providers = IRAN_PROVIDERS if scope == "iran_only" else GLOBAL_PROVIDERS
    if scope == "iran_only" and not providers:
        with db() as conn:
            conn.execute("UPDATE posts SET status=? WHERE id=?", ("blocked_no_iran_adapter", post_id))
        return [{"status": "blocked", "reason": "No verified Iranian-platform adapter configured"}]
    if not providers:
        raise RuntimeError("No destination networks are configured")

    media = json.loads(post["media"])
    variants = make_variants(post["text"])
    tz = ZoneInfo(TZ_NAME)
    now = datetime.now(tz)
    outcomes = []

    for wave, variant in enumerate(variants):
        publish_at = now + timedelta(days=wave * 10, hours=1)
        for provider in providers:
            if provider == "instagram" and not media:
                outcomes.append({"wave": wave + 1, "provider": provider, "status": "skipped", "reason": "Instagram requires image or video"})
                continue
            if provider == "youtube":
                outcomes.append({"wave": wave + 1, "provider": provider, "status": "skipped", "reason": "YouTube video metadata and upload workflow are not configured"})
                continue
            try:
                result = metricool_schedule(variant, media, provider, publish_at)
                status = "scheduled" if AUTO_PUBLISH else "draft"
                with db() as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO scheduled(post_id,wave,provider,publish_at,status,result) VALUES(?,?,?,?,?,?)",
                        (post_id, wave, provider, publish_at.isoformat(), status, json.dumps(result, ensure_ascii=False))
                    )
                outcomes.append({"wave": wave + 1, "provider": provider, "status": status, "publish_at": publish_at.isoformat()})
            except Exception as exc:
                log.exception("Scheduling failed for post=%s provider=%s", post_id, provider)
                outcomes.append({"wave": wave + 1, "provider": provider, "status": "error", "error": str(exc)})

    final_status = "processed" if any(x["status"] in ("scheduled", "draft") for x in outcomes) else "blocked"
    with db() as conn:
        conn.execute("UPDATE posts SET status=?, result=? WHERE id=?",
                     (final_status, json.dumps(outcomes, ensure_ascii=False), post_id))
    return outcomes


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "global-post-engine",
        "auto_publish": AUTO_PUBLISH,
        "global_providers": GLOBAL_PROVIDERS,
        "iran_providers_configured": bool(IRAN_PROVIDERS)
    })


@app.post("/api/posts")
def create_post():
    if not APP_SECRET or request.headers.get("X-App-Secret", "") != APP_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", "")).strip()
    scope = str(data.get("scope", "global")).strip().lower()
    media = data.get("media") or []

    if not text:
        return jsonify({"error": "text is required"}), 400
    if "فقط ایران" in text:
        scope = "iran_only"
    if scope not in ("global", "iran_only"):
        return jsonify({"error": "scope must be global or iran_only"}), 400
    if not isinstance(media, list) or any(not isinstance(x, str) or not x.startswith("https://") for x in media):
        return jsonify({"error": "media must be a list of public HTTPS URLs"}), 400

    try:
        post_id, created = save_post(text, scope, media)
        if not created:
            return jsonify({"id": post_id, "status": "duplicate_ignored"}), 200
        schedule = schedule_campaign(post_id)
        return jsonify({"id": post_id, "scope": scope, "schedule": schedule}), 202
    except RuntimeError as exc:
        log.warning("Post queued but not scheduled: %s", exc)
        return jsonify({"id": post_id, "status": "queued", "message": str(exc)}), 202
    except Exception:
        log.exception("Post intake failed")
        return jsonify({"error": "internal_error"}), 500



TELEGRAM_ADMIN_IDS = {x.strip() for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if x.strip()}
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

@app.post("/telegram/webhook")
def telegram_webhook():
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not TELEGRAM_WEBHOOK_SECRET or supplied != TELEGRAM_WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    update = request.get_json(silent=True) or {}
    message = update.get("message") or {}
    sender = str((message.get("from") or {}).get("id", ""))
    if not sender or sender not in TELEGRAM_ADMIN_IDS:
        return jsonify({"ok": True, "ignored": True}), 200
    text = str(message.get("text") or message.get("caption") or "").strip()
    if not text or text.startswith("/"):
        return jsonify({"ok": True, "ignored": True}), 200
    scope = "iran_only" if "فقط ایران" in text else "global"
    try:
        post_id, created = save_post(text, scope, [])
        if not created:
            return jsonify({"ok": True, "status": "duplicate_ignored"}), 200
        schedule = schedule_campaign(post_id)
        return jsonify({"ok": True, "post_id": post_id, "scope": scope, "schedule": schedule}), 202
    except Exception:
        log.exception("Telegram post intake failed")
        return jsonify({"ok": False, "error": "processing_failed"}), 500


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
