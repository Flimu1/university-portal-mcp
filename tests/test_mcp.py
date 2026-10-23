import json

import anyio
import httpx

from test_client import make_client, SITE, TOKEN
from test_files import tiny_pdf


def payload(result):
    return result.structuredContent or json.loads(result.content[0].text)


def test_mcp_discovery_to_download_to_read_and_error_contract(tmp_path):
    from mcp.shared.memory import create_connected_server_and_client_session
    from university_mcp.server import create_server
    from university_mcp.files import MaterialStore
    client = make_client(tmp_path / "state", {'gradereport_overview_get_course_grades': {
        'grades': [{'courseid': 569, 'rawgrade': None, 'grade': '-'}], 'warnings': []}})
    api_transport = client.http._transport

    def handle(request):
        if request.method == "GET":
            assert request.url.params["token"] == TOKEN
            return httpx.Response(200, content=tiny_pdf(), headers={"Content-Type": "application/pdf"})
        return api_transport.handle_request(request)

    client.http = httpx.Client(transport=httpx.MockTransport(handle))
    store = MaterialStore(client, tmp_path / "downloads")
    server = create_server(client, store)

    async def scenario():
        async with create_connected_server_and_client_session(server) as session:
            tools = (await session.list_tools()).tools
            names = {t.name for t in tools}
            assert names == {"portal_status", "list_courses", "get_course", "list_assignments", "get_activity",
                             "list_materials", "download_material", "read_material", "search_materials",
                             "list_deadlines", "get_submission_status", "save_assignment_submission", "submit_assignment",
                             "get_quiz_status", "start_quiz_attempt", "get_quiz_attempt", "save_quiz_answers", "finish_quiz_attempt",
                             "list_grades", "get_course_grades", "get_course_progress", "list_calendar_events", "list_announcements", "get_study_overview", "get_it_grades", "get_it_attendance"}
            mutation_names = {'save_assignment_submission', 'submit_assignment', 'start_quiz_attempt', 'save_quiz_answers', 'finish_quiz_attempt'}
            assert all(t.annotations.readOnlyHint == (t.name not in mutation_names) for t in tools)
            assert all("url" not in t.inputSchema.get("properties", {}) for t in tools)
            courses = payload(await session.call_tool("list_courses", {}))
            assert courses["courses"][0]["id"] == 569
            grades = payload(await session.call_tool('list_grades', {}))
            assert grades['courses'][0]['id'] == 569 and grades['courses'][0]['score'] is None
            outline = payload(await session.call_tool("get_course", {"course_id": 569}))
            assert outline["total_sections"] == 2
            activity = payload(await session.call_tool("get_activity", {"course_id": 569, "module_id": 32634}))
            material_id = activity["materials"][0]["material_id"]
            args = {"course_id": 569, "module_id": 32634, "material_id": material_id}
            downloaded = payload(await session.call_tool("download_material", args))
            assert downloaded["size_bytes"] > 100
            read = payload(await session.call_tool("read_material", {**args, "limit": 1000}))
            assert "Assets equal liabilities plus equity." in read["text"]
            assert TOKEN not in json.dumps(read)
            task = payload(await session.call_tool("get_activity", {"course_id": 569, "module_id": 22017, "limit": 8}))
            assert task["content_text"] == "Prepare " and task["next_offset"] == 8
            found = payload(await session.call_tool("search_materials", {"query": "balance", "course_id": 569}))
            assert any(h["module_id"] == 32634 for h in found["matches"])
            failed = await session.call_tool("get_activity", {"course_id": 569, "module_id": 9999})
            assert failed.isError and TOKEN not in failed.content[0].text

    anyio.run(scenario)


def test_missing_auth_does_not_crash_server_or_expose_config(tmp_path, monkeypatch):
    from mcp.shared.memory import create_connected_server_and_client_session
    from university_mcp.server import create_server
    monkeypatch.setenv("UNIVERSITY_MCP_AUTH_FILE", str(tmp_path / "missing.json"))

    async def scenario():
        async with create_connected_server_and_client_session(create_server()) as session:
            assert len((await session.list_tools()).tools) == 26
            result = await session.call_tool("portal_status", {})
            assert result.isError and "auth" in result.content[0].text.lower()

    anyio.run(scenario)


def test_it_tools_are_independent_of_moodle_auth(tmp_path, monkeypatch):
    from mcp.shared.memory import create_connected_server_and_client_session
    from university_mcp.server import create_server
    monkeypatch.delenv("UNIVERSITY_IT_AUTH_FILE", raising=False)
    monkeypatch.setenv("UNIVERSITY_MCP_AUTH_FILE", str(tmp_path / "missing.json"))
    async def scenario():
        async with create_connected_server_and_client_session(create_server()) as session:
            result = await session.call_tool("get_it_grades", {})
            assert result.isError and "UNIVERSITY_IT_AUTH_FILE" in result.content[0].text
    anyio.run(scenario)
