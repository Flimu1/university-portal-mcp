"""Download discovered Moodle files and extract bounded text without executing content."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile
from functools import lru_cache
from importlib.metadata import version

from .client import AuthenticationError, MoodleClient, PortalError

MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000


@lru_cache(maxsize=1)
def _converter():
    from markitdown import MarkItDown
    return MarkItDown(enable_plugins=False, llm_client=None, llm_model=None)


def extract_text(path: Path, *, max_unpacked_bytes=MAX_UNPACKED_BYTES, max_text_chars=MAX_TEXT_CHARS) -> dict:
    """Convert local documents with Microsoft MarkItDown, without LLM/cloud calls."""
    suffix = path.suffix.lower()
    result = {"text": "", "extraction_status": "text", "extraction_truncated": False,
              "engine": "markitdown", "engine_version": version("markitdown"), "text_format": "markdown"}
    try:
        if suffix in {".docx", ".pptx", ".xlsx", ".zip"}:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if sum(e.file_size for e in entries) > max_unpacked_bytes or len(entries) > 20000:
                    raise PortalError("Office/archive unpacked size exceeds the extraction limit.")
                if suffix == ".zip":
                    # Do not recursively convert arbitrary archive members.
                    return {**result, "engine": "archive_listing", "extraction_status": "archive_listing",
                            "text": "\n".join(e.filename for e in entries[:1000])[:max_text_chars],
                            "archive_members": [{"name": e.filename, "size_bytes": e.file_size} for e in entries[:1000]],
                            "extraction_truncated": len(entries) > 1000}
        if suffix == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(path)
            if reader.is_encrypted and not reader.decrypt(""):
                return {**result, "extraction_status": "encrypted", "note": "PDF requires a password; original file is available."}
            result["page_count"] = len(reader.pages)
        if suffix == ".doc" and Path("/usr/bin/textutil").exists():
            converted = subprocess.run(["/usr/bin/textutil", "-convert", "txt", "-stdout", str(path)],
                                       capture_output=True, timeout=40)
            if converted.returncode != 0:
                return {**result, "engine": "textutil", "extraction_status": "download_only",
                        "note": "Legacy Word conversion failed; use the original file."}
            text = converted.stdout.decode("utf-8", errors="replace")
            result["engine"] = "textutil"
        elif suffix in {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".txt", ".csv", ".tsv", ".md", ".xml", ".json"}:
            text = _converter().convert_local(path, keep_data_uris=False).markdown
        else:
            return {**result, "engine": "none", "extraction_status": "download_only",
                    "note": "No configured local text reader for this format. Original file is available; audio is not transcribed."}
        result["text"] = text[:max_text_chars]
        result["extraction_truncated"] = len(text) > max_text_chars
        if suffix == ".pdf" and not text.strip():
            result.update(extraction_status="image_only", note="No selectable PDF text found. Download/view pages or run OCR separately.")
        return result
    except PortalError:
        raise
    except Exception as exc:
        return {**result, "text": "", "extraction_status": "extraction_failed",
                "note": f"Local conversion failed ({type(exc).__name__}); original file remains available."}


def _atomic_text(path: Path, text: str):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(text)
    os.replace(temporary, path)


def _safe_filename(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", name).strip(". ") or "material"
    # Bound bytes, retaining the extension used for format detection.
    stem, extension = os.path.splitext(name)
    return stem.encode("utf-8")[:100].decode("utf-8", errors="ignore") + extension[:12]


class MaterialStore:
    def __init__(self, client: MoodleClient, download_dir: Path):
        self.client = client
        self.download_dir = Path(download_dir).resolve()
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.max_download_bytes = MAX_DOWNLOAD_BYTES

    def download(self, material: dict, refresh=False) -> dict:
        if not re.fullmatch(r"[a-f0-9]{20}", material["material_id"]):
            raise PortalError("Invalid material identifier.")
        folder = self.download_dir / str(int(material["course_id"])) / str(int(material["module_id"]))
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (material["material_id"] + "-" + _safe_filename(material["filename"]))
        meta_path = path.with_name(path.name + ".metadata.json")
        if not refresh and path.exists() and meta_path.exists():
            try:
                cached = json.loads(meta_path.read_text(encoding="utf-8"))
                if (cached.get("source_url") == material["url"] and cached.get("modified_at") == material.get("modified_at")
                        and cached.get("size_bytes") == path.stat().st_size):
                    return {**cached, "cached": True}
            except (OSError, ValueError):
                pass
        temporary = None
        try:
            digest = hashlib.sha256()
            size = 0
            prefix = bytearray()
            with self.client.stream_file(material["url"]) as response:
                claimed = response.headers.get("content-length")
                if claimed and int(claimed) > self.max_download_bytes:
                    raise PortalError("File is too large for the configured download size limit (128 MiB by default).")
                mime = response.headers.get("content-type", "application/octet-stream").split(";")[0]
                with tempfile.NamedTemporaryFile(dir=folder, suffix=".part", delete=False) as handle:
                    temporary = Path(handle.name)
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self.max_download_bytes:
                            raise PortalError("File exceeds the download size limit; incomplete file removed.")
                        if len(prefix) < 8192:
                            prefix.extend(chunk[:8192 - len(prefix)])
                        digest.update(chunk)
                        handle.write(chunk)
            start = bytes(prefix).lower()
            if (b'type="password"' in start or b"type='password'" in start or b'name="logintoken"' in start
                    or b"<title>log in" in start or b'errorcode":"invalidtoken' in start):
                raise AuthenticationError("Portal returned a login page or invalid token instead of the requested material. Refresh authentication.")
            extension = path.suffix.lower()
            if extension == ".pdf" and not bytes(prefix).lstrip().startswith(b"%PDF-"):
                raise PortalError("Portal response is not a PDF; the requested lecture was not saved.")
            if extension in {".docx", ".pptx", ".xlsx"} and not bytes(prefix).startswith(b"PK"):
                raise PortalError("Portal response is not an Office document; the requested material was not saved.")
            os.replace(temporary, path)
            temporary = None
            metadata = {
                "material_id": material["material_id"], "course_id": material["course_id"],
                "module_id": material["module_id"], "filename": material["filename"],
                "source_url": material["url"], "modified_at": material.get("modified_at"),
                "local_path": str(path), "size_bytes": size, "mime_type": mime, "sha256": digest.hexdigest(),
            }
            _atomic_text(meta_path, json.dumps(metadata, ensure_ascii=False, indent=2))
            return {**metadata, "cached": False}
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def read(self, material: dict, offset=0, limit=12000) -> dict:
        if offset < 0 or not 1 <= limit <= 20000:
            raise PortalError("Use offset >= 0 and limit between 1 and 20000 characters.")
        downloaded = self.download(material)
        extraction_dir = self.client.data_dir / "extracted"
        extraction_dir.mkdir(exist_ok=True, mode=0o700)
        cache = extraction_dir / (downloaded["sha256"] + "-markitdown-" + version("markitdown") + "-v1.json")
        if cache.exists():
            extracted = json.loads(cache.read_text(encoding="utf-8"))
        else:
            extracted = extract_text(Path(downloaded["local_path"]))
            _atomic_text(cache, json.dumps(extracted, ensure_ascii=False))
        text = extracted.pop("text", "")
        markdown_path = Path(downloaded["local_path"] + ".markitdown-" + version("markitdown") + "-" + downloaded["sha256"][:12] + ".md")
        if not markdown_path.exists():
            _atomic_text(markdown_path, text)
        downloaded["markdown_path"] = str(markdown_path)
        return {**downloaded, **extracted, "text": text[offset:offset + limit], "offset": offset,
                "total_characters": len(text), "next_offset": offset + limit if offset + limit < len(text) else None}
