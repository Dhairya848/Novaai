
import os
import json
import time
import base64
import sqlite3
import logging
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv

# ==========================================
# NOVAAI - MULTI PROVIDER AI BACKEND
# ==========================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("novaai")

DB_PATH = os.getenv(
    "NOVA_DB_PATH",
    os.path.join(BASE_DIR, "novaai_usage.db")
)

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "90"))
MAX_MESSAGE_LENGTH = int(os.getenv("MAX_MESSAGE_LENGTH", "12000"))
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))
MAX_IMAGE_BYTES = 10 * 1024 * 1024

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are NovaAI, a helpful, accurate, friendly AI assistant. "
    "Explain clearly, admit uncertainty, and never invent sources."
)

# ==========================================
# AI PROVIDERS
# ==========================================

PROVIDERS = {
    "gemini": {
        "label": "Google Gemini",
        "kind": "gemini",
        "key_env": "GEMINI_API_KEY",
        "model_env": "GEMINI_MODEL",
        "default_model": "gemini-2.5-flash",
        "limit_env": "GEMINI_DAILY_LIMIT",
        "period": "day",
    },

    "openrouter": {
        "label": "OpenRouter",
        "kind": "openai",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_MODEL",
        "default_model": "openrouter/free",
        "limit_env": "OPENROUTER_DAILY_LIMIT",
        "period": "day",
    },

    "groq": {
        "label": "Groq",
        "kind": "openai",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "model_env": "GROQ_MODEL",
        "default_model": "openai/gpt-oss-120b",
        "limit_env": "GROQ_DAILY_LIMIT",
        "period": "day",
    },

    "cloudflare": {
        "label": "Cloudflare Workers AI",
        "kind": "cloudflare",
        "key_env": "CLOUDFLARE_API_TOKEN",
        "account_env": "CLOUDFLARE_ACCOUNT_ID",
        "model_env": "CLOUDFLARE_MODEL",
        "default_model": "@cf/meta/llama-3.1-8b-instruct",
        "limit_env": "CLOUDFLARE_DAILY_LIMIT",
        "period": "day",
    },
}

AUTO_ORDER = [
    "gemini",
    "openrouter",
    "groq",
    "cloudflare"
]

COOLDOWN_SECONDS = int(
    os.getenv("PROVIDER_COOLDOWN_SECONDS", "900")
)


# ==========================================
# ERRORS
# ==========================================

class ProviderError(Exception):

    def __init__(self, provider, message, status_code=None):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


# ==========================================
# DATABASE
# ==========================================

def now_utc():
    return datetime.now(timezone.utc)


def period_key(period):
    current = now_utc()

    if period == "month":
        return current.strftime("%Y-%m")

    return current.strftime("%Y-%m-%d")


def db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_database():

    with db_connection() as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage (
                provider TEXT NOT NULL,
                period TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (provider, period)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS cooldowns (
                provider TEXT PRIMARY KEY,
                until_time REAL NOT NULL,
                reason TEXT DEFAULT ''
            )
        """)

        conn.commit()


def get_usage(provider):

    period = PROVIDERS[provider]["period"]
    key = period_key(period)

    with db_connection() as conn:

        row = conn.execute(
            "SELECT count FROM usage WHERE provider=? AND period=?",
            (provider, key)
        ).fetchone()

    return int(row["count"]) if row else 0


def increment_usage(provider):

    period = PROVIDERS[provider]["period"]
    key = period_key(period)

    with db_connection() as conn:

        conn.execute("""
            INSERT INTO usage(provider, period, count)
            VALUES(?, ?, 1)

            ON CONFLICT(provider, period)
            DO UPDATE SET count = count + 1
        """, (provider, key))

        conn.commit()


# ==========================================
# COOLDOWN SYSTEM
# ==========================================

def get_cooldown(provider):

    with db_connection() as conn:

        row = conn.execute(
            "SELECT until_time, reason FROM cooldowns WHERE provider=?",
            (provider,)
        ).fetchone()

    if not row:
        return None

    remaining = float(row["until_time"]) - time.time()

    if remaining <= 0:

        with db_connection() as conn:
            conn.execute(
                "DELETE FROM cooldowns WHERE provider=?",
                (provider,)
            )
            conn.commit()

        return None

    return {
        "seconds": int(remaining),
        "reason": row["reason"] or "Temporarily paused"
    }


def set_cooldown(provider, seconds, reason):

    seconds = max(30, min(int(seconds), 86400))

    with db_connection() as conn:

        conn.execute("""
            INSERT INTO cooldowns(provider, until_time, reason)
            VALUES(?, ?, ?)

            ON CONFLICT(provider)
            DO UPDATE SET
                until_time=excluded.until_time,
                reason=excluded.reason
        """, (
            provider,
            time.time() + seconds,
            str(reason)[:200]
        ))

        conn.commit()


def clear_cooldown(provider):

    with db_connection() as conn:

        conn.execute(
            "DELETE FROM cooldowns WHERE provider=?",
            (provider,)
        )

        conn.commit()


# ==========================================
# PROVIDER CONFIGURATION
# ==========================================

def provider_configured(provider):

    cfg = PROVIDERS[provider]

    if not os.getenv(cfg["key_env"], "").strip():
        return False

    if provider == "cloudflare":

        if not os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip():
            return False

    return True


def provider_model(provider):

    cfg = PROVIDERS[provider]

    return (
        os.getenv(cfg["model_env"], cfg["default_model"]).strip()
        or cfg["default_model"]
    )


def local_limit(provider):

    env_name = PROVIDERS[provider]["limit_env"]

    try:
        return max(0, int(os.getenv(env_name, "0")))
    except ValueError:
        return 0


def provider_available(provider):

    if not provider_configured(provider):
        return False, "API key or account ID missing"

    cooldown = get_cooldown(provider)

    if cooldown:
        return False, f"Cooldown: {cooldown['seconds']} seconds"

    limit = local_limit(provider)

    if limit > 0 and get_usage(provider) >= limit:
        return False, "Local usage limit reached"

    return True, "Ready"


# ==========================================
# PROVIDER STATUS
# ==========================================

def get_provider_status():

    output = []

    for provider, cfg in PROVIDERS.items():

        configured = provider_configured(provider)
        available, reason = provider_available(provider)
        limit = local_limit(provider)

        output.append({

            "id": provider,
            "name": cfg["label"],
            "configured": configured,
            "available": available,
            "status": reason,
            "model": provider_model(provider),

            "quota": {
                "used": get_usage(provider) if configured else 0,

                "daily_limit": (
                    limit if cfg["period"] == "day" else 0
                ),

                "monthly_limit": (
                    limit if cfg["period"] == "month" else 0
                ),

                "period": cfg["period"],
            }
        })

    return output


# ==========================================
# RESPONSE ERROR HANDLING
# ==========================================

def safe_json(response):

    try:
        return response.json()
    except Exception:
        return {}


def response_error(provider, response):

    data = safe_json(response)

    message = (
        data.get("error", {}).get("message")
        if isinstance(data.get("error"), dict)
        else None
    )

    if not message:

        message = (
            data.get("message")
            or response.text[:500]
            or "Provider request failed"
        )

    if response.status_code == 429:

        retry_after = response.headers.get("Retry-After", "")

        try:
            delay = int(retry_after)
        except (TypeError, ValueError):
            delay = COOLDOWN_SECONDS

        set_cooldown(
            provider,
            delay,
            "Rate limit / quota reached"
        )

    elif response.status_code in (401, 403):

        set_cooldown(
            provider,
            3600,
            "Authentication or permission error"
        )

    raise ProviderError(
        provider,
        f"{PROVIDERS[provider]['label']}: {message}",
        response.status_code
    )


# ==========================================
# IMAGE HANDLING
# ==========================================

def image_to_data_url(image_file):

    if not image_file:
        return None

    raw = image_file.read()

    if not raw:
        return None

    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(
            "Image is too large. Maximum image size is 10 MB."
        )

    mime = image_file.mimetype or "application/octet-stream"

    if mime not in (
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif"
    ):
        raise ValueError(
            "Please upload a JPG, PNG, WEBP, or GIF image."
        )

    encoded = base64.b64encode(raw).decode("ascii")

    return f"data:{mime};base64,{encoded}"


# ==========================================
# CHAT HISTORY
# ==========================================

def clean_history(raw_history):

    if not raw_history:
        return []

    try:
        items = json.loads(raw_history)
    except (TypeError, json.JSONDecodeError):
        return []

    if not isinstance(items, list):
        return []

    cleaned = []

    for item in items[-MAX_HISTORY_MESSAGES:]:

        if not isinstance(item, dict):
            continue

        role = item.get("role")
        content = item.get("content")

        if role not in ("user", "assistant"):
            continue

        if not isinstance(content, str):
            continue

        content = content.strip()

        if content:

            cleaned.append({
                "role": role,
                "content": content[:MAX_MESSAGE_LENGTH]
            })

    return cleaned


def build_openai_messages(history, message, image_data_url=None):

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
        }
    ]

    messages.extend(clean_history(json.dumps(history)))

    if image_data_url:

        content = [
            {
                "type": "text",
                "text": message or "Please analyze this image."
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": image_data_url
                }
            }
        ]

    else:
        content = message

    messages.append({
        "role": "user",
        "content": content
    })

    return messages


# ==========================================
# OPENAI-COMPATIBLE PROVIDERS
# GROQ + OPENROUTER
# ==========================================

def ask_openai_compatible(
    provider,
    history,
    message,
    image_data_url=None
):

    cfg = PROVIDERS[provider]

    headers = {
        "Authorization": (
            f"Bearer {os.getenv(cfg['key_env']).strip()}"
        ),
        "Content-Type": "application/json",
    }

    if provider == "openrouter":

        headers["HTTP-Referer"] = os.getenv(
            "OPENROUTER_SITE_URL",
            "https://novaai.local"
        )

        headers["X-Title"] = os.getenv(
            "OPENROUTER_APP_NAME",
            "NovaAI"
        )

    payload = {

        "model": provider_model(provider),

        "messages": build_openai_messages(
            history,
            message,
            image_data_url
        ),

        "temperature": float(
            os.getenv("AI_TEMPERATURE", "0.7")
        ),

        "max_tokens": int(
            os.getenv("AI_MAX_TOKENS", "2048")
        ),
    }

    try:

        response = requests.post(
            cfg["url"],
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT
        )

    except requests.RequestException as exc:

        raise ProviderError(
            provider,
            f"Network error: {exc}"
        ) from exc

    if not response.ok:
        response_error(provider, response)

    data = safe_json(response)

    try:

        answer = data["choices"][0]["message"]["content"]

    except (KeyError, IndexError, TypeError):

        raise ProviderError(
            provider,
            "The provider returned an unexpected response."
        )

    if isinstance(answer, list):

        answer = "\n".join(
            part.get("text", "")
            for part in answer
            if isinstance(part, dict)
        )

    answer = str(answer or "").strip()

    if not answer:

        raise ProviderError(
            provider,
            "The provider returned an empty answer."
        )

    increment_usage(provider)
    clear_cooldown(provider)

    return answer


# ==========================================
# GOOGLE GEMINI
# ==========================================

def ask_gemini(history, message, image_data_url=None):

    provider = "gemini"

    api_key = os.getenv(
        "GEMINI_API_KEY",
        ""
    ).strip()

    model = provider_model(provider)

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{quote(model, safe='')}:generateContent"
    )

    contents = []

    for item in clean_history(json.dumps(history)):

        contents.append({

            "role": (
                "user"
                if item["role"] == "user"
                else "model"
            ),

            "parts": [
                {
                    "text": item["content"]
                }
            ]
        })

    parts = [
        {
            "text": message or "Please analyze this image."
        }
    ]

    if image_data_url:

        header, encoded = image_data_url.split(",", 1)

        mime = header.split(";")[0].replace("data:", "")

        parts.append({

            "inline_data": {
                "mime_type": mime,
                "data": encoded
            }

        })

    contents.append({
        "role": "user",
        "parts": parts
    })

    payload = {

        "system_instruction": {
            "parts": [
                {
                    "text": SYSTEM_PROMPT
                }
            ]
        },

        "contents": contents,

        "generationConfig": {

            "temperature": float(
                os.getenv("AI_TEMPERATURE", "0.7")
            ),

            "maxOutputTokens": int(
                os.getenv("AI_MAX_TOKENS", "2048")
            ),
        }
    }

    try:

        response = requests.post(

            url,

            params={
                "key": api_key
            },

            headers={
                "Content-Type": "application/json"
            },

            json=payload,

            timeout=REQUEST_TIMEOUT
        )

    except requests.RequestException as exc:

        raise ProviderError(
            provider,
            f"Network error: {exc}"
        ) from exc

    if not response.ok:
        response_error(provider, response)

    data = safe_json(response)

    try:

        answer = "".join(

            part.get("text", "")

            for part in data["candidates"][0]["content"]["parts"]

        ).strip()

    except (KeyError, IndexError, TypeError):

        answer = ""

    if not answer:

        raise ProviderError(
            provider,
            "Gemini returned an empty or blocked response."
        )

    increment_usage(provider)
    clear_cooldown(provider)

    return answer


# ==========================================
# CLOUDFLARE WORKERS AI
# ==========================================

def ask_cloudflare(history, message, image_data_url=None):

    provider = "cloudflare"

    account_id = os.getenv(
        "CLOUDFLARE_ACCOUNT_ID",
        ""
    ).strip()

    token = os.getenv(
        "CLOUDFLARE_API_TOKEN",
        ""
    ).strip()

    model = provider_model(provider)

    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{quote(account_id, safe='')}/ai/v1/chat/completions"
    )

    headers = {

        "Authorization": f"Bearer {token}",

        "Content-Type": "application/json",
    }

    payload = {

        "model": model,

        "messages": build_openai_messages(
            history,
            message,
            image_data_url
        ),

        "temperature": float(
            os.getenv("AI_TEMPERATURE", "0.7")
        ),

        "max_tokens": int(
            os.getenv("AI_MAX_TOKENS", "2048")
        ),
    }

    try:

        response = requests.post(

            url,

            headers=headers,

            json=payload,

            timeout=REQUEST_TIMEOUT
        )

    except requests.RequestException as exc:

        raise ProviderError(
            provider,
            f"Network error: {exc}"
        ) from exc

    if not response.ok:
        response_error(provider, response)

    data = safe_json(response)

    try:

        answer = data["choices"][0]["message"]["content"]

    except (KeyError, IndexError, TypeError):

        result = data.get("result", {})

        answer = (
            result.get("response", "")
            if isinstance(result, dict)
            else ""
        )

    if isinstance(answer, list):

        answer = "\n".join(
            part.get("text", "")
            for part in answer
            if isinstance(part, dict)
        )

    answer = str(answer or "").strip()

    if not answer:

        raise ProviderError(
            provider,
            "Cloudflare returned an empty or unexpected response."
        )

    increment_usage(provider)
    clear_cooldown(provider)

    return answer


# ==========================================
# PROVIDER ROUTER
# ==========================================

def ask_provider(
    provider,
    history,
    message,
    image_data_url=None
):

    available, reason = provider_available(provider)

    if not available:

        raise ProviderError(
            provider,
            reason
        )

    if provider == "gemini":

        return ask_gemini(
            history,
            message,
            image_data_url
        )

    if provider == "cloudflare":

        return ask_cloudflare(
            history,
            message,
            image_data_url
        )

    return ask_openai_compatible(
        provider,
        history,
        message,
        image_data_url
    )


# ==========================================
# AUTOMATIC FALLBACK
# ==========================================

def ask_auto(history, message, image_da
