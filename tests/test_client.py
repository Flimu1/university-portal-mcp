"""Contract tests: fixture HTTP responses exercise real client discovery and errors."""
import json
from urllib.parse import parse_qs

import httpx
import pytest

SITE = "https://eng.sbcde.by"
TOKEN = "fixture-secret-token"
COURSE = {"id": 569, "fullname": "Accounting", "shortname": "ACC", "visible": 1}
OUTLINE = [
    {"id": 1, "name": "Lecture", "visible": 1, "summary": "<p>Read this</p>", "modules": [
        {"id": 32634, "name": "Balance sheet", "modname": "resource", "instance": 18636,
         "uservisible": True, "url": SITE + "/mod/resource/view.php?id=32634",
         "contents": [{"type": "file", "filename": "balance.pdf", "filesize": 123,
                       "fileurl": SITE + "/webservice/pluginfile.php/1/mod_resource/content/1/balance.pdf"}]},
        {"id": 22017, "name": "Homework", "modname": "assign", "instance": 3768,
         "uservisible": True, "contents": []},
        {"id": 333, "name": "Locked", "modname": "quiz", "uservisible": False,
         "availabilityinfo": "<p>Opens next week</p>"},
    ]},
    {"id": 2, "name": "Future topic", "visible": 0, "modules": []},
]
ASSIGNMENTS = {"courses": [{"id": 569, "assignments": [
    {"id": 3768, "cmid": 22017, "course": 569, "name": "Homework", "intro": "<p>Prepare <b>entries</b>.</p>",
     "duedate": 1792961940, "introfiles": [], "introattachments": [
         {"filename": "task.docx", "filesize": 234, "fileurl": SITE + "/webservice/pluginfile.php/2/mod_assign/introattachment/0/task.docx"}
     ]}
]}], "warnings": []}


def make_client(tmp_path, responses=None):
    from university_mcp.client import MoodleClient
    data = {
        "core_webservice_get_site_info": {"userid": 9991, "fullname": "Student", "sitename": "Portal", "release": "3.9.14+",
                                         "downloadfiles": 1, "functions": [{"name": x} for x in [
                                             "core_course_get_contents", "core_enrol_get_users_courses", "mod_assign_get_assignments"]]},
        "core_enrol_get_users_courses": [COURSE],
        "core_course_get_contents": OUTLINE,
        "mod_assign_get_assignments": ASSIGNMENTS,
        "mod_resource_get_resources_by_courses": {"resources": [], "warnings": []},
    }
    data.update(responses or {})

    def handle(request):
        assert request.method == "POST" and request.url.path == "/webservice/rest/server.php"
        args = parse_qs(request.content.decode())
        assert args["wstoken"] == [TOKEN]
        assert "wstoken" not in str(request.url)
        name = args["wsfunction"][0]
        if name == "core_enrol_get_users_courses":
            assert args["userid"] == ["9991"]
        return httpx.Response(200, json=data[name])

    http = httpx.Client(transport=httpx.MockTransport(handle))
    return MoodleClient(TOKEN, tmp_path, http=http)


def test_course_retains_structure_restrictions_and_material_provenance(tmp_path):
    client = make_client(tmp_path)
    assert client.courses()["courses"][0]["id"] == 569
    result = client.course(569)
    assert [s["name"] for s in result["sections"]] == ["Lecture", "Future topic"]
    assert result["sections"][1]["visible"] is False
    modules = result["sections"][0]["modules"]
    assert modules[2]["accessible"] is False
    assert modules[2]["availability"] == "Opens next week"
    material = modules[0]["materials"][0]
    assert material["filename"] == "balance.pdf"
    assert material["course_id"] == 569 and material["module_id"] == 32634
    assert material["material_id"] and TOKEN not in json.dumps(result)


def test_assignment_discovers_attachments_absent_from_course_contents(tmp_path):
    client = make_client(tmp_path)
    task = client.activity(569, 22017)
    assert task["content_text"] == "Prepare entries."
    assert task["due_at"].endswith("+03:00")
    assert task["materials"][0]["filename"] == "task.docx"
    assert task["materials"][0]["source_kind"] == "assignment_attachment"
    assert client.assignments(569)["assignments"][0]["module_id"] == 22017


def test_expired_token_is_actionable_and_never_echoes_secret(tmp_path):
    from university_mcp.client import AuthenticationError
    client = make_client(tmp_path, {"core_webservice_get_site_info": {
        "exception": "moodle_exception", "errorcode": "invalidtoken", "message": "Bad " + TOKEN}})
    with pytest.raises(AuthenticationError, match="(?i)(auth|token|login)") as exc:
        client.status()
    assert TOKEN not in str(exc.value)


def test_client_refuses_state_changing_functions_before_network(tmp_path):
    from university_mcp.client import PortalError
    client = make_client(tmp_path)
    with pytest.raises(PortalError, match="read-only"):
        client.call("mod_quiz_start_attempt", {"quizid": 12})


def test_course_pagination_does_not_silently_omit_sections(tmp_path):
    client = make_client(tmp_path)
    result = client.course(569, offset=0, limit=1)
    assert len(result["sections"]) == 1
    assert result["total_sections"] == 2 and result["next_offset"] == 1
    assert client.course(569, offset=1, limit=1)["next_offset"] is None


def test_unknown_and_locked_modules_do_not_claim_readable_content(tmp_path):
    from university_mcp.client import PortalError
    client = make_client(tmp_path)
    assert client.activity(569, 333)["accessible"] is False
    with pytest.raises(PortalError, match="(?i)(module|activity)"):
        client.activity(569, 99999)


def test_tokens_in_server_html_and_file_urls_are_redacted(tmp_path):
    outline = [{"id": 1, "name": "Topic", "modules": [{
        "id": 8, "modname": "label", "name": "Label", "uservisible": True,
        "description": f'<p>See <a href="{SITE}/pluginfile.php/2/a.pdf?token={TOKEN}">file</a></p>',
        "contents": [{"type": "file", "filename": "a.pdf", "fileurl": SITE + "/pluginfile.php/2/a.pdf?token=" + TOKEN}]
    }]}]
    client = make_client(tmp_path, {"core_course_get_contents": outline})
    assert TOKEN not in json.dumps(client.course(569))
    assert TOKEN not in json.dumps(client.activity(569, 8))


def test_resource_revision_zero_alias_does_not_duplicate_same_lecture(tmp_path):
    outline = [{"id": 1, "name": "Topic", "modules": [{
        "id": 8, "modname": "resource", "instance": 2, "name": "Lecture", "uservisible": True,
        "contents": [{"type": "file", "filename": "a.pdf", "fileurl": SITE + "/webservice/pluginfile.php/9/mod_resource/content/1/a.pdf?forcedownload=1"}]
    }]}]
    details = {"resources": [{"id": 2, "coursemodule": 8, "contentfiles": [
        {"filename": "a.pdf", "fileurl": SITE + "/webservice/pluginfile.php/9/mod_resource/content/0/a.pdf"}]}]}
    client = make_client(tmp_path, {"core_course_get_contents": outline, "mod_resource_get_resources_by_courses": details})
    assert len(client.activity(569, 8)["materials"]) == 1


def test_forum_array_response_is_readable_and_does_not_break_course_index(tmp_path):
    forum = {"id": 899, "cmid": 7, "name": "Announcements", "intro": "<p>Course news</p>", "introfiles": []}
    outline = [{"id": 1, "name": "General", "modules": [
        {"id": 7, "instance": 899, "modname": "forum", "name": "Announcements", "uservisible": True},
        OUTLINE[0]["modules"][0],
    ]}]
    client = make_client(tmp_path, {"core_course_get_contents": outline, "mod_forum_get_forums_by_courses": [forum]})
    assert client.activity(569, 7)["content_text"] == "Course news"
    assert client.material_index(569)["total"] == 1


def test_unavailable_detail_api_preserves_known_materials_and_reports_partial(tmp_path):
    client = make_client(tmp_path, {"mod_resource_get_resources_by_courses": {
        "exception": "moodle_exception", "errorcode": "nopermissions", "message": "Restricted"}})
    activity = client.activity(569, 32634)
    assert activity["content_status"] == "partial" and activity["warnings"]
    assert activity["materials"][0]["filename"] == "balance.pdf"
    assert client.material_index(569)["total"] == 2


def test_material_index_deduplicates_course_wide_api_warnings(tmp_path):
    warning = {"item": "module", "itemid": 999, "warningcode": "1", "message": "No access rights"}
    client = make_client(tmp_path, {"mod_assign_get_assignments": {**ASSIGNMENTS, "warnings": [warning, warning]}})
    assert client.material_index(569)["warnings"] == [warning]
