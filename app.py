
import os
import json
import base64
import sqlite3
import logging
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv

# ==================================================
# NOVAAI - MULTI PROVIDER BACKEND
# Free-access providers + quota tracking + fallback
# ==================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("NovaAI")

app = Flask(__name__, static_folder=None)

app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024

DB_PATH = os.path.join(BASE_DIR, "novaai_usage.db")

TIMEOUT = (10, 90)
MAX_IMAGE_SIZE = 10 * 1024 * 1024
MAX_HISTORY = 30
MAX_MESSAGE_LENGTH = 20000

SYSTEM_PROMPT = """
You are NovaAI, a helpful and intelligent AI assistant.

Your responsibilities:
- Answer questions accurately.
- Explain difficult topics clearly.
- Help with science, mathematics, education and coding.
- Write clean and correct code.
- Analyse uploaded images when supported.
- Never invent facts when uncertain.
- Be friendly, professional and respectful.
"""

# ==================================================
# CONFIGURATION HELPERS
# ==================================================

def get_env(name, default=""):
    return os.getenv(name, default).strip()


def get_int(name, default=0):
    try:
        return max(0, int(get_env(name, str(default))))
    except ValueError:
        return default


# ==================================================
# PROVIDER CONFIGURATION
# ==================================================

PROVIDERS = {
    "gemini": {
        "name": "Google Gemini",
        "type": "gemini",
        "key": get_env("GEMINI_API_KEY"),
        "model": get_env("GEMINI_MODEL", "gemini-2.5-flash"),
    },

    "groq": {
        "name": "Groq",
        "type": "openai",
        "key": get_env("GROQ_API_KEY"),
        "model": get_env("GROQ_MODEL", "openai/gpt-oss-120b"),
        "url": "https://api.groq.com/openai/v1/chat/completions",
    },

    "mistral": {
        "name": "Mistral AI",
        "type": "openai",
        "key": get_env("MISTRAL_API_KEY"),
        "model": get_env("MISTRAL_MODEL", "mistral-small-latest"),
        "url": "https://api.mistral.ai/v1/chat/completions",
    },

    "openrouter": {
        "name": "OpenRouter",
        "type": "openai",
        "key": get_env("OPENROUTER_API_KEY"),
        "model": get_env("OPENROUTER_MODEL", "openrouter/free"),
        "url": "https://openrouter.ai/api/v1/chat/completions",
    },

    "sambanova": {
        "name": "SambaNova",
        "type": "openai",
        "key": get_env("SAMBANOVA_API_KEY"),
        "model": get_env("SAMBANOVA_MODEL"),
        "url": "https://api.sambanova.ai/v1/chat/completions",
    },

    "cohere": {
        "name": "Cohere",
        "type": "openai",
        "key": get_env("COHERE_API_KEY"),
        "model": get_env("COHERE_MODEL"),
        "url": "https://api.cohere.com/compatibility/v1/chat/completions",
    },

    "cloudflare": {
        "name": "Cloudflare Workers AI",
        "type": "openai",
        "key": get_env("CLOUDFLARE_API_TOKEN"),
        "model": get_env("CLOUDFLARE_MODEL"),
        "account_id": get_env("CLOUDFLARE_ACCOUNT_ID"),
        "url": (
            "https://api.cloudflare.com/client/v4/accounts/"
            + get_env("CLOUDFLARE_ACCOUNT_ID")
            + "/ai/v1/chat/completions"
        ),
    },

    "nvidia": {
        "name": "NVIDIA NIM",
        "type": "openai",
        "key": get_env("NVIDIA_API_KEY"),
        "model": get_env("NVIDIA_MODEL"),
        "url": "https://integrate.api.nvidia.com/v1/chat/completions",
    },
}

# Automatic provider order
AUTO_ORDER = [
    "gemini",
    "groq",
    "mistral",
    "openrouter",
    "sambanova",
    "cohere",
    "cloudflare",
    "nvidia",
]

# ==================================================
# LOCAL QUOTA SETTINGS
#
# 0 = no local cap
# These are local safety caps, not guaranteed
# provider allowances.
# ==================================================

DAILY_LIMITS = {
    "gemini": get_int("GEMINI_DAILY_LIMIT", 0),
    "groq": get_int("GROQ_DAILY_LIMIT", 0),
    "mistral": get_int("MISTRAL_DAILY_LIMIT", 0),
    "openrouter": get_int("OPENROUTER_DAILY_LIMIT", 50),
    "sambanova": get_int("SAMBANOVA_DAILY_LIMIT", 0),
    "cloudflare": get_int("CLOUDFLARE_DAILY_LIMIT", 0),
    "nvidia": get_int("NVIDIA_DAILY_LIMIT", 0),
}

MONTHLY_LIMITS = {
    "cohere": get_int("COHERE_MONTHLY_LIMIT", 1000),
}

# How long to pause a provider after a 429 response.
DEFAULT_COOLDOWN = get_int("PROVIDER_COOLDOWN_SECONDS", 900)

# ==================================================
# DATABASE
# ==================================================

def database():
    return sqlite3.connect(DB_PATH, timeout=20)


def initialize_database():
    with database() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS usage (
                provider TEXT NOT NULL,
                period TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(provider, period)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS cooldowns (
                provider TEXT PRIMARY KEY,
                until_time REAL NOT NULL
            )
        """)

    logger.info("Usage database initialized.")


def current_day():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def current_month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def get_usage(provider, period):
    with database() as db:
        row = db.execute(
            "SELECT count FROM usage WHERE provider=? AND period=?",
            (provider, period)
        ).fetchone()

    return row[0] if row else 0


def increase_usage(provider, period):
    with database() as db:
        db.execute("""
            INSERT INTO usage(provider, period, count)
            VALUES (?, ?, 1)
            ON CONFLICT(provider, period)
            DO UPDATE SET count = count + 1
        """, (provider, period))


def get_cooldown(provider):
    with database() as db:
        row = db.execute(
            "SELECT until_time FROM cooldowns WHERE provider=?",
            (provider,)
        ).fetchone()

    if not row:
        return 0

    remaining = int(row[0] - time.time())

    if remaining <= 0:
        with database() as db:
            db.execute(
                "DELETE FROM cooldowns WHERE provider=?",
                (provider,)
            )
        return 0

    return remaining


def set_cooldown(provider, seconds=None):
    if seconds is None:
        seconds = DEFAULT_COOLDOWN

    seconds = max(60, min(int(seconds), 86400))
    until_time = time.time() + seconds

    with database() as db:
        db.execute("""
            INSERT INTO cooldowns(provider, until_time)
            VALUES (?, ?)
            ON CONFLICT(provider)
            DO UPDATE SET until_time=excluded.until_time
        """, (provider, until_time))


# ==================================================
# QUOTA CHECKING
# ==================================================

def quota_information(provider):
    daily_limit = DAILY_LIMITS.get(provider, 0)
    monthly_limit = MONTHLY_LIMITS.get(provider, 0)

    daily_used = get_usage(provider, current_day())
    monthly_used = get_usage(provider, current_month())

    daily_remaining = (
        max(0, daily_limit - daily_used)
        if daily_limit else None
    )

    monthly_remaining = (
        max(0, monthly_limit - monthly_used)
        if monthly_limit else None
    )

    cooldown = get_cooldown(provider)

    allowed = (
        (not daily_limit or daily_used < daily_limit)
        and (not monthly_limit or monthly_used < monthly_limit)
        and cooldown == 0
    )

    return {
        "daily_limit": daily_limit or None,
        "daily_used": daily_used,
        "daily_remaining": daily_remaining,
        "monthly_limit": monthly_limit or None,
        "monthly_used": monthly_used,
        "monthly_remaining": monthly_remaining,
        "cooldown_seconds": cooldown,
        "allowed": allowed,
    }


def can_use_provider(provider):
    return quota_information(provider)["allowed"]


def is_configured(provider):
    p = PROVIDERS[provider]

    if not p.get("key") or not p.get("model"):
        return False

    if provider == "cloudflare":
        return bool(p.get("account_id"))

    return True


def get_provider_status():
    result = []

    for provider, p in PROVIDERS.items():
        info = quota_information(provider)

        result.append({
            "id": provider,
            "name": p["name"],
            "model": p["model"],
            "configured": is_configured(provider),
            "quota": info,
        })

    return result


# ==================================================
# ERROR HANDLING
# ==================================================

class ProviderError(Exception):
    def __init__(self, message, status_code=None, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def read_error(response):
    try:
        data = response.json()
        error = data.get("error", data)

        if isinstance(error, dict):
            return str(
                error.get("message")
                or error.get("type")
                or "Provider error"
            )[:400]

        return str(error)[:400]

    except Exception:
        return response.text[:200]


def send_request(url, headers, payload, provider):
    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=TIMEOUT,
        )

    except requests.Timeout:
        raise ProviderError("Request timed out.")

    except requests.RequestException:
        raise ProviderError("Network connection failed.")

    if not response.ok:
        retry_after = response.headers.get("Retry-After")

        try:
            retry_after = int(retry_after) if retry_after else None
        except ValueError:
            retry_after = None

        raise ProviderError(
            read_error(response),
            status_code=response.status_code,
            retry_after=retry_after,
        )

    try:
        return response.json()

    except ValueError:
        raise ProviderError("Invalid response from provider.")


# ==================================================
# INPUT VALIDATION
# ==================================================

def get_history():
    raw = request.form.get("history", "[]")

    try:
        history = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError("Invalid chat history.")

    if not isinstance(history, list):
        raise ValueError("History must be a list.")

    cleaned = []

    for item in history[-MAX_HISTORY:]:
        if not isinstance(item, dict):
            continue

        role = item.get("role")
        content = item.get("content")

        if role not in ("user", "assistant"):
            continue

        if not isinstance(content, str):
            continue

        content = content.strip()[:MAX_MESSAGE_LENGTH]

        if content:
            cleaned.append({
                "role": role,
                "content": content,
            })

    return cleaned


def get_image():
    file = request.files.get("image")

    if not file or not file.filename:
        return None

    mime = (file.mimetype or "").lower()

    allowed = {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
    }

    if mime not in allowed:
        raise ValueError("Unsupported image format.")

    raw = file.read(MAX_IMAGE_SIZE + 1)

    if not raw:
        raise ValueError("Empty image.")

    if len(raw) > MAX_IMAGE_SIZE:
        raise ValueError("Image must be under 10 MB.")

    return {
        "mime_type": mime,
        "data": base64.b64encode(raw).decode("utf-8"),
    }


# ==================================================
# GEMINI API
# ==================================================

def call_gemini(p, history, message, image):
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + quote(p["model"], safe="")
        + ":generateContent"
    )

    contents = []

    for item in history:
        contents.append({
            "role": "model" if item["role"] == "assistant" else "user",
            "parts": [{"text": item["content"]}],
        })

    parts = [{"text": message or "Please analyse this image."}]

    if image:
        parts.append({
            "inline_data": {
                "mime_type": image["mime_type"],
                "data": image["data"],
            }
        })

    contents.append({
        "role": "user",
        "parts": parts,
    })

    payload = {
        "systemInstruction": {
            "parts": [{"text": SYSTEM_PROMPT}]
        },
        "contents": contents,
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 4096,
        },
    }

    data = send_request(
        url + "?key=" + quote(p["key"], safe=""),
        {"Content-Type": "application/json"},
        payload,
        "gemini",
    )

    try:
        answer = "\n".join(
            part.get("text", "")
            for part in data["candidates"][0]["content"]["parts"]
            if part.get("text")
        ).strip()

        if answer:
            return answer

    except (KeyError, IndexError, TypeError):
        pass

    raise ProviderError("Gemini returned no text.")


# ==================================================
# OPENAI-COMPATIBLE PROVIDERS
# ==================================================

def extract_ai_text(data):
    try:
        content = data["choices"][0]["message"]["content"]

        if isinstance(content, str):
            return content.strip()

        if isinstance(content, list):
            return "\n".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ).strip()

    except (KeyError, IndexError, TypeError):
        pass

    raise ProviderError("Could not read AI response.")


def call_openai_provider(provider, p, history, message, image):
    headers = {
        "Authorization": "Bearer " + p["key"],
        "Content-Type": "application/json",
    }

    if provider == "openrouter":
        headers["HTTP-Referer"] = "https://novaai.local"
        headers["X-Title"] = "NovaAI"

    messages = [{
        "role": "system",
        "content": SYSTEM_PROMPT,
    }]

    for item in history:
        messages.append({
            "role": item["role"],
            "content": item["content"],
        })

    if image:
        user_content = [
            {
                "type": "text",
                "text": message or "Please analyse this image.",
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": (
                        "data:"
                        + image["mime_type"]
                        + ";base64,"
                        + image["data"]
                    )
                },
            },
        ]
    else:
        user_content = message

    messages.append({
        "role": "user",
        "content": user_content,
    })

    payload = {
        "model": p["model"],
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 4096,
        "stream": False,
    }

    data = send_request(
        p["url"],
        headers,
        payload,
        provider,
    )

    return extract_ai_text(data)


# ==================================================
# MAIN PROVIDER DISPATCHER
# ==================================================

def call_provider(provider, history, message, image):
    p = PROVIDERS[provider]

    if p["type"] == "gemini":
        return call_gemini(p, history, message, image)

    if p["type"] == "openai":
        return call_openai_provider(
            provider, p, history, message, image
        )

    raise ProviderError("Unsupported provider.")


# ==================================================
# PROVIDER REQUEST WITH USAGE TRACKING
# ==================================================

def ask_one(provider, history, message, image):
    if not is_configured(provider):
        raise ProviderError("Provider is not configured.")

    if not can_use_provider(provider):
        raise ProviderError("Provider quota exhausted or cooling down.")

    # Count the attempt before sending it. This prevents repeated
    # attempts from bypassing the local safety cap.
    period = current_month() if provider in MONTHLY_LIMITS else current_day()
    increase_usage(provider, period)

    try:
        answer = call_provider(
            provider,
            history,
            message,
            image,
        )

        if not answer:
            raise ProviderError("Empty response.")

        return answer

    except ProviderError as exc:
        if exc.status_code == 429:
            set_cooldown(
                provider,
                exc.retry_after or DEFAULT_COOLDOWN
            )

        elif exc.status_code in (401, 403):
            # Authentication or permission problems should not
            # repeatedly hit the same provider.
            set_cooldown(provider, 3600)

        raise


# ==================================================
# AUTOMATIC FALLBACK
# ==================================================

def ask_auto(history, message, image):
    errors = []

    for provider in AUTO_ORDER:
        if not is_configured(provider):
            continue

        if not can_use_provider(provider):
            logger.info("Skipping %s: quota/cooldown", provider)
            continue

        try:
            answer = ask_one(
                provider,
                history,
                message,
                image,
            )

            return answer, provider

        except Exception as exc:
            logger.warning(
                "%s failed: %s",
                provider,
                str(exc)[:200],
            )

            errors.append(provider + ": " + str(exc)[:120])

    if not errors:
        raise ProviderError(
            "No configured providers are currently available. "
            "Check your API keys, model IDs and quota settings."
        )

    raise ProviderError(
        "All available providers failed. "
        + " | ".join(errors)
    )


# ==================================================
# FRONTEND ROUTES
# Only serve the three intended frontend files.
# ==================================================

@app.route("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/style.css")
def css():
    return send_from_directory(BASE_DIR, "style.css")


@app.route("/script.js")
def js():
    return send_from_directory(BASE_DIR, "script.js")


# ==================================================
# CHAT ENDPOINT
# ==================================================

@app.route("/api/chat", methods=["POST"])
def chat():
    try:
        message = request.form.get("message", "").strip()
        selected = request.form.get("provider", "auto").lower().strip()

        if len(message) > MAX_MESSAGE_LENGTH:
            return jsonify({
                "error": "Message is too long."
            }), 400

        history = get_history()
        image = get_image()

        if not message and not image:
            return jsonify({
                "error": "Enter a message or upload an image."
            }), 400

        if selected != "auto" and selected not in PROVIDERS:
            ret