"""Fixed-origin, read-only Moodle mobile REST client and resource discovery."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit, unquote

import httpx

from .models import SITE, clean_url, file_metadata, html_text, redact, timestamp


class PortalError(Exception):
    """Actionable, credential-free failure suitable for returning to an agent."""


class AuthenticationError(PortalError):
    pass


DETAIL_APIS = {
    "page": ("mod_page_get_pages_by_courses", "pages"),
    "book": ("mod_book_get_books_by_courses", "books"),
    "folder": ("mod_folder_get_folders_by_courses", "folders"),
    "url": ("mod_url_get_urls_by_courses", "urls"),
    "quiz": ("mod_quiz_get_quizzes_by_courses", "quizzes"),
    "lesson": ("mod_lesson_get_lessons_by_courses", "lessons"),
    "resource": ("mod_resource_get_resources_by_courses", "resources"),
    "forum": ("mod_forum_get_forums_by_courses", "forums"),
}
READ_FUNCTIONS = frozenset({
    "core_webservice_get_site_info", "core_enrol_get_users_courses", "core_course_get_contents",
    "core_course_get_course_module", "mod_assign_get_assignments",
    "mod_assign_get_submission_status", "mod_quiz_get_user_attempts", "mod_quiz_get_quiz_access_information",
    "mod_quiz_get_attempt_data", "mod_quiz_get_attempt_summary", "mod_quiz_get_attempt_review",
    "gradereport_overview_get_course_grades", "gradereport_user_get_grade_items",
    "core_completion_get_activities_completion_status", "core_calendar_get_calendar_events",
    "mod_forum_get_forum_discussions",
    *(name for name, _ in DETAIL_APIS.values()),
})


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MoodleClient:
    def __init__(self, token: str, data_dir: Path, *, http: httpx.Client | None = None):
        if not token:
            raise AuthenticationError("Portal API token is missing. Run the documented local authentication setup.")
        self._token = token
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.http = http or httpx.Client(timeout=httpx.Timeout(40, connect=15), follow_redirects=False)
        self._cache: dict[tuple, tuple[float, object, str]] = {}

    def close(self):
        self.http.close()

    def call(self, function: str, params: dict | None = None):
        if function not in READ_FUNCTIONS:
            raise PortalError("This MCP is read-only; the requested API function is not allowed.")
        return self._request_api(function, params)

    def _request_api(self, function: str, params: dict | None = None):
        """Private transport shared with explicitly scoped authorized action workflows."""
        data = {"wstoken": self._token, "moodlewsrestformat": "json", "wsfunction": function}
        data.update(params or {})
        try:
            response = self.http.post(SITE + "/webservice/rest/server.php", data=data)
            if response.status_code in (401, 403):
                raise AuthenticationError("Portal authentication was rejected. Refresh the API token using local setup.")
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PortalError(f"Portal API request failed ({type(exc).__name__}); retry or check portal connectivity.") from None
        if isinstance(result, dict) and ("exception" in result or "errorcode" in result):
            code = str(result.get("errorcode", "unknown"))
            if code in {"invalidtoken", "invalidprivatetoken", "accessexception", "requireloginerror", "webserviceaccess"}:
                raise AuthenticationError(f"Portal API authorization failed ({code}). Refresh token or check service permissions.")
            raise PortalError(f"Moodle rejected {function}: {redact(code, self._token)}. The content may be restricted.")
        return redact(result, self._token)

    def _get(self, function: str, params: dict | None = None, refresh=False):
        key = (function, json.dumps(params or {}, sort_keys=True))
        entry = self._cache.get(key)
        if refresh or entry is None or time.monotonic() - entry[0] > 300:
            value = self.call(function, params)
            entry = (time.monotonic(), value, now())
            self._cache[key] = entry
        return entry[1], entry[2]

    def status(self, include_functions=False) -> dict:
        info = self.call("core_webservice_get_site_info")
        result = {
            "authenticated": True, "site": SITE, "read_only": True,
            "user": {"id": info["userid"], "name": info.get("fullname")},
            "moodle_release": info.get("release"), "download_files": bool(info.get("downloadfiles")),
            "available_api_function_count": len(info.get("functions", [])),
            "mcp_read_functions": sorted(READ_FUNCTIONS & {f["name"] for f in info.get("functions", [])}),
            "checked_at": now(),
        }
        if include_functions:
            result["available_api_functions"] = [f["name"] for f in info.get("functions", [])]
        return result

    def courses(self, refresh=False) -> dict:
        info, _ = self._get("core_webservice_get_site_info", refresh=refresh)
        rows, fetched = self._get("core_enrol_get_users_courses", {"userid": info["userid"]}, refresh=refresh)
        courses = [{
            "id": c["id"], "name": html_text(c["fullname"]).strip(), "short_name": c.get("shortname"),
            "url": f"{SITE}/course/view.php?id={c['id']}", "visible": bool(c.get("visible", 1)),
            "summary": html_text(c.get("summary", "")), "progress_percent": c.get("progress"),
        } for c in rows]
        return {"courses": courses, "total": len(courses), "fetched_at": fetched, "cache_ttl_seconds": 300}

    def _outline(self, course_id: int, refresh=False):
        if course_id not in {c["id"] for c in self.courses(refresh=refresh)["courses"]}:
            raise PortalError("Course is not in this account's enrolled courses. Use list_courses first.")
        return self._get("core_course_get_contents", {"courseid": course_id}, refresh=refresh)

    def _module(self, course_id: int, module_id: int):
        sections, _ = self._outline(course_id)
        for section in sections:
            for module in section.get("modules", []):
                if module["id"] == module_id:
                    return module, section
        raise PortalError("Activity/module was not returned in this course. It may be hidden or unavailable.")

    def _module_summary(self, module: dict, course_id: int):
        return {
            "module_id": module["id"], "name": html_text(module.get("name", "")),
            "type": module.get("modname"), "instance_id": module.get("instance"),
            "url": clean_url(module.get("url", "")), "accessible": bool(module.get("uservisible", True)),
            "availability": html_text(module.get("availabilityinfo", "")),
            "description_preview": html_text(module.get("description", ""))[:600],
            "materials": [m for raw in module.get("contents", [])
                          if (m := file_metadata(raw, course_id, module["id"], "course_resource"))],
        }

    def course(self, course_id: int, offset=0, limit=50, refresh=False) -> dict:
        if offset < 0 or not 1 <= limit <= 100:
            raise PortalError("Use offset >= 0 and limit between 1 and 100 sections.")
        rows, fetched = self._outline(course_id, refresh=refresh)
        course = next(c for c in self.courses()["courses"] if c["id"] == course_id)
        result = []
        for section in rows[offset:offset + limit]:
            result.append({
                "section_id": section["id"], "name": html_text(section.get("name", "")),
                "visible": bool(section.get("visible", 1)), "availability": html_text(section.get("availabilityinfo", "")),
                "summary": html_text(section.get("summary", "")),
                "modules": [self._module_summary(m, course_id) for m in section.get("modules", [])],
            })
        return {"course": course, "sections": result, "total_sections": len(rows), "offset": offset,
                "next_offset": offset + limit if offset + limit < len(rows) else None, "fetched_at": fetched,
                "note": "Only content Moodle exposes to this account is listed. Hidden modules may be omitted by Moodle."}

    def _assignments(self, course_id: int):
        data, _ = self._get("mod_assign_get_assignments", {"courseids[0]": course_id})
        return [a for c in data.get("courses", []) for a in c.get("assignments", [])], data.get("warnings", [])

    def assignments(self, course_id: int) -> dict:
        self._outline(course_id)
        data, warnings = self._assignments(course_id)
        return {"course_id": course_id, "assignments": [{
            "module_id": a["cmid"], "assignment_id": a["id"], "name": html_text(a["name"]),
            "due_at": timestamp(a.get("duedate")), "cutoff_at": timestamp(a.get("cutoffdate")),
            "available_from": timestamp(a.get("allowsubmissionsfromdate")),
            "description_preview": html_text(a.get("intro", ""))[:1200],
            "attachment_count": len(a.get("introattachments", [])) + len(a.get("introfiles", [])),
        } for a in data], "total": len(data), "warnings": warnings,
                "note": "Assignment requirements only; no student submissions or grades are requested."}

    def activity(self, course_id: int, module_id: int) -> dict:
        module, section = self._module(course_id, module_id)
        result = {"course_id": course_id, "section": html_text(section.get("name", "")),
                  **self._module_summary(module, course_id)}
        if not result["accessible"]:
            return {**result, "content_text": "", "content_status": "restricted"}
        kind = module.get("modname")
        html_parts = [module.get("description", "")]
        materials = {m["material_id"]: m for m in result["materials"]}
        details = None
        details_failed = False
        try:
            if kind == "assign":
                rows, warnings = self._assignments(course_id)
                details = next((a for a in rows if a["cmid"] == module_id), None)
                result["warnings"] = warnings
                if details:
                    result.update({"due_at": timestamp(details.get("duedate")),
                                   "cutoff_at": timestamp(details.get("cutoffdate")),
                                   "available_from": timestamp(details.get("allowsubmissionsfromdate"))})
            elif kind in DETAIL_APIS:
                function, field = DETAIL_APIS[kind]
                data, _ = self._get(function, {"courseids[0]": course_id})
                details = next((a for a in (data if isinstance(data, list) else data.get(field, []))
                                if a.get("coursemodule", a.get("cmid")) == module_id
                                or a.get("id") == module.get("instance")), None)
                result["warnings"] = data.get("warnings", []) if isinstance(data, dict) else []
        except AuthenticationError:
            raise
        except PortalError as exc:
            details_failed = True
            result["warnings"] = [{"module_id": module_id, "message": str(exc)}]
        if details:
            html_parts += [details.get("intro", ""), details.get("content", "")]
            for field in ("introfiles", "introattachments", "contentfiles"):
                for raw in details.get(field, []):
                    meta = file_metadata(raw, course_id, module_id,
                                         "assignment_attachment" if field == "introattachments" else field)
                    if meta:
                        materials.setdefault(meta["material_id"], meta)
            if kind == "url":
                result["external_url"] = clean_url(details.get("externalurl", ""))
            if kind == "quiz":
                result.update({"opens_at": timestamp(details.get("timeopen")),
                               "closes_at": timestamp(details.get("timeclose")),
                               "time_limit_seconds": details.get("timelimit"),
                               "note": "Quiz description only. No attempt is started and no questions are submitted."})
        for raw in module.get("contents", []):
            if raw.get("type") == "content":
                html_parts.append(raw.get("content", ""))
            elif raw.get("type") == "url":
                result.setdefault("external_url", clean_url(raw.get("fileurl", "")))
        # Preserve order while avoiding duplicate intro HTML returned by two APIs.
        html = "\n".join(dict.fromkeys(h for h in html_parts if h))
        result.update({"content_html": html, "content_text": html_text(html), "materials": list(materials.values()),
                       "content_status": "available" if details or html or materials else "description_not_exposed"})
        if details_failed:
            result["content_status"] = "partial"
            result["note"] = "Detail API unavailable; only known course description and files are returned."
        elif kind == "assign" and details is None:
            result["content_status"] = "restricted_or_not_exposed"
        if kind not in DETAIL_APIS and kind not in {"assign", "label"}:
            result["note"] = "This module has no dedicated reader here; exposed description and files are returned."
        return redact(result, self._token)

    def material_index(self, course_id: int, module_id: int | None = None) -> dict:
        sections, fetched = self._outline(course_id)
        items = []
        warnings = []
        for section in sections:
            for module in section.get("modules", []):
                if module_id is not None and module["id"] != module_id:
                    continue
                if not module.get("uservisible", True):
                    continue
                activity = self.activity(course_id, module["id"])
                items.extend({**m, "activity_name": activity["name"], "section": activity["section"]}
                             for m in activity["materials"])
                warnings.extend(activity.get("warnings", []))
        unique = {item["material_id"]: item for item in items}
        warnings = list({json.dumps(w, sort_keys=True): w for w in warnings}.values())
        return {"course_id": course_id, "materials": list(unique.values()), "total": len(unique),
                "fetched_at": fetched, "warnings": warnings}

    def resolve_material(self, course_id: int, module_id: int, material_id: str):
        activity = self.activity(course_id, module_id)
        for material in activity["materials"]:
            if material["material_id"] == material_id:
                return {**material, "activity_name": activity["name"]}
        raise PortalError("Material ID is not present in this accessible activity. Get the activity again to refresh IDs.")

    @contextmanager
    def stream_file(self, url: str):
        """Keep file authentication private and reject redirects outside approved file routes."""
        target = clean_url(url)
        for _ in range(4):
            decoded_path = urlsplit(target).path
            for _decode in range(5):
                if "\\" in decoded_path or any(segment in {".", ".."} for segment in decoded_path.split("/")):
                    raise PortalError("Ambiguous traversal path is not an approved portal file URL.")
                decoded = unquote(decoded_path)
                if decoded == decoded_path:
                    break
                decoded_path = decoded
            else:
                raise PortalError("Excessively encoded portal file path is not allowed.")
            # Validate precisely the URL HTTPX will send, after its normalization.
            parts = urlsplit(str(httpx.URL(target)))
            if (parts.scheme, parts.netloc) != ("https", "eng.sbcde.by") or not parts.path.startswith(
                ("/webservice/pluginfile.php/", "/pluginfile.php/")
            ):
                raise PortalError("Only discovered files on the portal's HTTPS file endpoints can be downloaded.")
            if parts.path.startswith("/pluginfile.php/"):
                parts = parts._replace(path="/webservice" + parts.path)
            query = [(k, v) for k, v in parse_qsl(parts.query) if k not in {"token", "wstoken"}]
            query.append(("token", self._token))
            authenticated = urlunsplit(parts._replace(query=urlencode(query)))
            try:
                with self.http.stream("GET", authenticated, follow_redirects=False) as response:
                    if response.is_redirect:
                        location = response.headers.get("location", "")
                        if "/login/" in location:
                            raise AuthenticationError("File download requires renewed portal authentication.")
                        target = urljoin(target, location)
                        continue
                    if response.status_code in (401, 403):
                        raise AuthenticationError("Portal refused this file. Refresh authentication or check access.")
                    response.raise_for_status()
                    yield response
                    return
            except httpx.HTTPError as exc:
                raise PortalError(f"File request failed ({type(exc).__name__}); no file was accepted.") from None
        raise PortalError("Too many portal file redirects.")
