"""Normalize Moodle HTML and file metadata without exposing authentication."""
from datetime import datetime
import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

SITE = "https://eng.sbcde.by"
SECRET_KEYS = {"token", "wstoken", "privatetoken", "sesskey", "password", "autologin"}


def clean_url(value: str) -> str:
    parts = urlsplit(value)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in SECRET_KEYS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def redact(value, token: str = ""):
    if isinstance(value, dict):
        return {k: redact(v, token) for k, v in value.items() if k.lower() not in SECRET_KEYS}
    if isinstance(value, list):
        return [redact(v, token) for v in value]
    if isinstance(value, str):
        if token:
            value = value.replace(token, "[redacted]")
        return re.sub(r"([?&](?:token|wstoken|privatetoken|sesskey)=)[^&\s\"'<>]+", r"\1[redacted]", value, flags=re.I)
    return value


def html_text(value: str) -> str:
    soup = BeautifulSoup(value or "", "html.parser")
    for node in soup(["script", "style"]):
        node.decompose()
    for node in soup.find_all(["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "br"]):
        node.insert_before("\n")
    for node in soup.find_all(["td", "th"]):
        node.insert_before(" | ")
    text = soup.get_text("", strip=False)
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip()).strip()


def timestamp(value) -> str | None:
    return datetime.fromtimestamp(int(value), ZoneInfo("Europe/Minsk")).isoformat() if value else None


def file_metadata(raw: dict, course_id: int, module_id: int, source_kind: str) -> dict | None:
    url = raw.get("fileurl")
    if not url or raw.get("type", "file") != "file":
        return None
    url = clean_url(url)
    parts = urlsplit(url)
    identity_path = re.sub(r"(/mod_resource/content/)\d+/", r"\1latest/", parts.path)
    identity_query = [(k, v) for k, v in parse_qsl(parts.query) if k != "forcedownload"]
    identity = f"{course_id}:{module_id}:{parts.netloc}:{identity_path}:{urlencode(identity_query)}"
    return {
        "material_id": hashlib.sha256(identity.encode()).hexdigest()[:20],
        "course_id": course_id,
        "module_id": module_id,
        "filename": raw.get("filename", "material"),
        "url": url,
        "size_bytes": raw.get("filesize"),
        "mime_type": raw.get("mimetype"),
        "modified_at": timestamp(raw.get("timemodified")),
        "source_kind": source_kind,
        "external": bool(raw.get("isexternalfile")) or urlsplit(url).netloc != urlsplit(SITE).netloc,
    }
