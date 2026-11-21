import os
import json
import sqlite3
import hashlib
import logging
from datetime import datetime, timedelta, timezone
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
BUFFER_API_KEY = os.getenv("BUFFER_API_KEY", "")
BUFFER_ORGANIZATION_ID = os.getenv("BUFFER_ORGANIZATION_ID", "")
TZ_NAME = os.getenv("POST_TIMEZONE", "Asia/Tehran")
GLOBAL_PROVIDERS = [x.strip().lower() for x in os.getenv("GLOBAL_PROVIDERS", "facebook,instagram").split(",") if x.strip()]
IRAN_PROVIDERS = [x.strip().lower() for x in os.getenv("IRAN_PROVIDERS", "").split(",") if x.strip()]
# Safe default: create drafts until the owner explicitly enables automatic publishing.
AUTO_PUBLISH = os.getenv("AUTO_PUBLISH", "false").lower() == "true"
DB_PATH = os.getenv("DATABASE_PATH", "posts.db")
BUFFER_API_URL = "https://api.buffer.com"
BUFFER_CHANNEL_CACHE = {}
RETRYABLE_STATUSES = ("queued", "blocked", "failed")


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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status)")


def make_variants(text):
    clean = " ".join(text.split())
    return [
        clean,
        "نکته مهم: " + clean,
        clean + "\n\nاگر این موضوع برایتان مفید بود، با دیگران به اشتراک بگذارید."
    ]


def buffer_graphql(query):
    if not BUFFER_API_KEY:
        raise RuntimeError("Buffer API key is not configured")
    response = requests.post(
        BUFFER_API_URL,
        headers={"Authorization": f"Bearer {BUFFER_API_KEY}", "Content-Type": "application/json"},
        json={"query": query}, timeout=30
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"], ensure_ascii=False))
    return payload.get("data", {})


def buffer_channels():
    global BUFFER_ORGANIZATION_ID
    if not BUFFER_API_KEY:
        raise RuntimeError("Buffer API key is not configured")
    if BUFFER_CHANNEL_CACHE:
        return BUFFER_CHANNEL_CACHE
    if not BUFFER_ORGANIZATION_ID:
        data = buffer_graphql("query { account { organizations { id name } } }")
        organizations = (data.get("account") or {}).get("organizations") or []
        if not organizations:
            raise RuntimeError("No Buffer organization is available")
        BUFFER_ORGANIZATION_ID = organizations[0]["id"]
    query = """
    query GetChannels {
      channels(input: { organizationId: "%s" }) {
        id
        name
        displayName
        service
      }
    }
    """ % BUFFER_ORGANIZATION_ID
    data = buffer_graphql(query)
    for channel in (data.get("channels") or []):
        BUFFER_CHANNEL_CACHE[str(channel.get("service", "")).lower()] = channel
    return BUFFER_CHANNEL_CACHE


def buffer_schedule(text, media, provider, publish_at):
    channels = buffer_channels()
    aliases = {"x": "twitter", "twitter": "twitter", "googlebusiness": "googlebusiness"}
    service = aliases.get(provider, provider)
    channel = channels.get(service)
    if not channel:
        raise RuntimeError(f"No connected Buffer channel for provider: {provider}")
    assets = []
    for url in media:
        lower = url.lower().split("?", 1)[0]
        kind = "video" if lower.endswith((".mp4", ".mov", ".m4v", ".webm")) else "image"
        assets.append("{ " + kind + ": { url: " + json.dumps(url) + " } }")
    fields = [
        "text: " + json.dumps(text, ensure_ascii=False),
        "channelId: " + json.dumps(channel["id"]),
        "schedulingType: automatic"
    ]
    if AUTO_PUBLISH:
        due_at = publish_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        fields.extend(["mode: customScheduled", "dueAt: " + json.dumps(due_at)])
    else:
        fields.extend(["mode: addToQueue", "saveToDraft: true"])
    if assets:
        fields.append("assets: [" + ", ".join(assets) + "]")
    mutation = """
    mutation CreatePost {
      createPost(input: {
        %s
      }) {
        ... on PostActionSuccess { post { id text status dueAt } }
        ... on MutationError { message }
      }
    }
    """ % "\n        ".join(fields)
    data = buffer_graphql(mutation)
    result = data.get("createPost") or {}
    if result.get("message") and not result.get("post"):
        raise RuntimeError(result["message"])
    post = result.get("post")
    if not post:
        raise RuntimeError("Buffer returned no post result")
    return {"provider": "buffer", "post": post, "channel": channel.get("displayName") or channel.get("name")}


def save_post(text, scope, media):
    digest = hashlib.sha256(
        (scope + "|" + text + "|" + json.dumps(media, ensure_ascii=False)).encode("utf-8")
    ).hexdigest()
    with db() as conn:
        existing = conn.execute(
            "SELECT id, status FROM posts WHERE content_hash=?", (digest,)
        ).fetchone()
        if existing:
            return existing["id"], False, existing["status"]
        cur = conn.execute(
            "INSERT INTO posts(content_hash,text,scope,media,status,created_at) VALUES(?,?,?,?,?,?)",
            (digest, text, scope, json.dumps(media, ensure_ascii=False), "queued", datetime.now().isoformat())
        )
        return cur.lastrowid, True, "queued"


def schedule_campaign(post_id):
    with db() as conn:
        post = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if not post:
        raise ValueError("post not found")

    scope = post["scope"]
    providers = IRAN_PROVIDERS if scope == "iran_only" else GLOBAL_PROVIDERS
    if scope == "iran_only" and not providers:
        with db() as conn:
            conn.execute(
                "UPDATE posts SET status=?, result=? WHERE id=?",
                ("blocked", json.dumps([{"status": "blocked", "reason": "No verified Iranian-platform adapter configured"}], ensure_ascii=False), post_id)
            )
        return [{"status": "blocked", "reason": "No verified Iranian-platform adapter configured"}]
    if not providers:
        raise RuntimeError("No destination networks are configured")

    media = json.loads(post["media"])
    variants = make_variants(post["text"])
    tz = ZoneInfo(TZ_NAME)
    now = datetime.now(tz)
    outcomes = []
    pending_credentials = False

    for wave, variant in enumerate(variants):
        publish_at = now + timedelta(days=wave * 10, hours=1)
        for provider in providers:
            with db() as conn:
                already = conn.execute(
                    "SELECT status, publish_at FROM scheduled WHERE post_id=? AND wave=? AND provider=?",
                    (post_id, wave, provider)
                ).fetchone()
            if already:
                outcomes.append({
                    "wave": wave + 1, "provider": provider,
                    "status": "already_scheduled", "publish_at": already["publish_at"]
                })
                continue
            if provider == "instagram" and not media:
                outcomes.append({"wave": wave + 1, "provider": provider, "status": "skipped", "reason": "Instagram requires image or video"})
                continue
            if provider == "youtube":
                outcomes.append({"wave": wave + 1, "provider": provider, "status": "skipped", "reason": "YouTube video metadata and upload workflow are not configured"})
                continue
            try:
                result = buffer_schedule(variant, media, provider, publish_at)
                status = "scheduled" if AUTO_PUBLISH else "draft"
                with db() as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO scheduled(post_id,wave,provider,publish_at,status,result) VALUES(?,?,?,?,?,?)",
                        (post_id, wave, provider, publish_at.isoformat(), status, json.dumps(result, ensure_ascii=False))
                    )
                outcomes.append({"wave": wave + 1, "provider": provider, "status": status, "publish_at": publish_at.isoformat()})
            except RuntimeError as exc:
                pending_credentials = True
                log.warning("Post %s is waiting for credentials: %s", post_id, exc)
                outcomes.append({"wave": wave + 1, "provider": provider, "status": "queued", "reason": str(exc)})
            except Exception as exc:
                log.exception("Scheduling failed for post=%s provider=%s", post_id, provider)
                outcomes.append({"wave": wave + 1, "provider": provider, "status": "error", "error": str(exc)})

    has_success = any(x["status"] in ("scheduled", "draft", "already_scheduled") for x in outcomes)
    has_error = any(x["status"] == "error" for x in outcomes)
    final_status = "processed" if has_success and not has_error else ("queued" if pending_credentials or not has_success else "failed")
    with db() as conn:
        conn.execute(
            "UPDATE posts SET status=?, result=? WHERE id=?",
            (final_status, json.dumps(outcomes, ensure_ascii=False), post_id)
        )
    return outcomes


def process_incoming(text, scope, media):
    post_id, created, status = save_post(text, scope, media)
    if not created and status not in RETRYABLE_STATUSES:
        return post_id, status, [{"status": "duplicate_ignored"}]
    schedule = schedule_campaign(post_id)
    with db() as conn:
        current = conn.execute("SELECT status FROM posts WHERE id=?", (post_id,)).fetchone()
    return post_id, current["status"], schedule


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "global-post-engine",
        "publishing_provider": "buffer",
        "auto_publish": AUTO_PUBLISH,
        "publishing_mode": "automatic" if AUTO_PUBLISH else "draft_review",
        "buffer_credentials_configured": bool(BUFFER_API_KEY),
        "global_providers": GLOBAL_PROVIDERS,
        "iran_providers_configured": bool(IRAN_PROVIDERS)
    })


@app.get("/api/buffer/channels")
def list_buffer_channels():
    if not APP_SECRET or request.headers.get("X-App-Secret", "") != APP_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    try:
        channels = list(buffer_channels().values())
        return jsonify({
            "organization_id": BUFFER_ORGANIZATION_ID,
            "channels": [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "display_name": item.get("displayName"),
                    "service": item.get("service")
                }
                for item in channels
            ]
        }), 200
    except Exception:
        log.exception("Buffer channel discovery failed")
        return jsonify({"error": "buffer_connection_failed"}), 502


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
        post_id, status, schedule = process_incoming(text, scope, media)
        code = 200 if status == "duplicate_ignored" else 202
        return jsonify({"id": post_id, "scope": scope, "status": status, "schedule": schedule}), code
    except Exception:
        log.exception("Post intake failed")
        return jsonify({"error": "internal_error"}), 500


@app.post("/api/retry-pending")
def retry_pending():
    if not APP_SECRET or request.headers.get("X-App-Secret", "") != APP_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    with db() as conn:
        rows = conn.execute(
            "SELECT id FROM posts WHERE status IN ('queued','blocked','failed') ORDER BY id ASC LIMIT 100"
        ).fetchall()
    results = []
    for row in rows:
        try:
            results.append({"id": row["id"], "schedule": schedule_campaign(row["id"])})
        except Exception:
            log.exception("Retry failed for post=%s", row["id"])
            results.append({"id": row["id"], "status": "error"})
    return jsonify({"retried": len(results), "results": results}), 200


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
        post_id, status, schedule = process_incoming(text, scope, [])
        return jsonify({"ok": True, "post_id": post_id, "status": status, "scope": scope, "schedule": schedule}), 202
    except Exception:
        log.exception("Telegram post intake failed")
        return jsonify({"ok": False, "error": "processing_failed"}), 500


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
