"""Synthetic own-account classic ASP contracts; no live student data."""
import json
from urllib.parse import parse_qs

import httpx
import pytest

SECRET = 'ASPSESSIONIDFIXTURE=fixturesecret'
ORIGIN = 'https://it-sb.by'


def filters(course='71', term='90'):
    return f'''<form action='/it/FinalGradesPrint.asp' method='post'>
    <select name='program'><option selected value='13'>Programme</option></select><input name='first' type='hidden'></form>
    <form action='/it/FinalGradesPrint.asp' method='post'>
    <select name='term'><option value='90' {'selected' if term == '90' else ''}>Autumn</option><option value='80' {'selected' if term == '80' else ''}>Spring</option></select>
    <input type='hidden' name='first'><input type='hidden' name='program' value='13'></form>
    <form action='/it/FinalGradesPrint.asp' method='post'>
    <select name='course'><option value='71' {'selected' if course == '71' else ''}>Accounting</option><option value='72' {'selected' if course == '72' else ''}>Finance</option></select>
    <input type='hidden' name='first'><input type='hidden' name='program' value='13'><input type='hidden' name='term' value='{term}'></form>
    <form action='/it/FinalGradesPrint.asp' method='post'>
    <select name='section'><option selected value='5'>Fifth</option></select>
    <input type='hidden' name='first'><input type='hidden' name='program' value='13'><input type='hidden' name='term' value='{term}'><input type='hidden' name='course' value='{course}'></form>'''


def grade_html(course='71', term='90', other=False):
    return '<html>' + filters(course, term) + '''<table><tr><td>Тек.</td><td>Название</td><td>Вес</td><td>Максимум</td></tr>
    <tr><td>✔</td><td>#1</td><td>Test</td><td>100%</td><td>10 баллов</td></tr></table>
    <table><tr><td colspan='2'>Студент</td><td>Точка контроля</td><td colspan='5'>Оценка</td></tr>
    <tr><td>ID</td><td>Имя студента</td><td>#1</td><td>Тек.</td><td>в %</td><td>Фин.</td><td>в %</td><td>Абс.</td></tr>
    <tr><td>10001</td><td>Example Student</td><td></td><td>1</td><td>0.00</td><td>1</td><td>0.00</td><td>0.00</td></tr>''' + (
        '<tr><td>20002</td><td>Other Student</td><td>8</td><td>8</td><td>85</td><td>8</td><td>85</td><td>8</td></tr>' if other else '') + '''<tr><td colspan='2'>Среднее:</td><td>0</td></tr></table></html>'''


def attendance_html(empty=False, other=False):
    prefix = filters().replace('FinalGradesPrint.asp', 'attendanceprint.asp')
    if empty:
        return prefix + '<p>В этой подгруппе нет ни одной даты. Пожалуйста, выберите другую подгруппу.</p>'
    return prefix + '''<table><tr><td rowspan='2' colspan='2'>Имя студента</td><td colspan='1'>Сентябрь 2026</td><td colspan='2'>Октябрь 2026</td><td rowspan='2'>Был<br>/Всего</td><td rowspan='2'>Был<br>В %</td></tr>
    <tr><td>25</td><td>5</td><td>19</td></tr><tr><td colspan='2'>Номер лекции</td><td>1</td><td>2</td><td>3</td><td colspan='2'></td></tr>
    <tr><td>1.</td><td>''' + ('Other Student' if other else 'Example\u00a0Student') + '''</td><td>X</td><td><br></td><td>X</td><td>1/3</td><td>33.33</td></tr></table>'''


def client(tmp_path, handler=None, auth_update=None):
    from university_mcp.it_portal import ITPortalClient
    auth = {'origin': ORIGIN, 'cookie': SECRET, 'student_id': '10001', 'student_name': 'Example Student'}
    auth.update(auth_update or {})
    path = tmp_path / 'auth.json'
    path.write_text(json.dumps(auth)); path.chmod(0o600)
    def default(request):
        assert request.headers['cookie'] == SECRET
        return httpx.Response(200, text=attendance_html() if request.url.path.endswith('attendanceprint.asp') else grade_html())
    return ITPortalClient(path, http=httpx.Client(transport=httpx.MockTransport(handler or default)))


def test_report_preserves_blank_checkpoint_and_portal_grade_without_inference(tmp_path):
    report = client(tmp_path).report('grades')
    assert report['student']['id'] == '10001'
    assert report['grades']['checkpoints'] == [{'number': '#1', 'value': None}]
    assert report['grades']['current'] == '1'
    assert report['grades']['final_percent'] == '0.00'
    assert report['selection']['term'] == {'id': '90', 'label': 'Autumn'}
    assert report['scope'] == 'selected_course_and_term'
    assert SECRET not in json.dumps(report)


def test_attendance_preserves_raw_marks_and_displayed_totals(tmp_path):
    report = client(tmp_path).report('attendance')
    assert report['attendance']['attended_total'] == '1/3'
    assert report['attendance']['percent'] == '33.33'
    assert report['attendance']['sessions'] == [
        {'date_label': 'Сентябрь 2026 25', 'lecture': '1', 'mark': 'X'},
        {'date_label': 'Октябрь 2026 5', 'lecture': '2', 'mark': None},
        {'date_label': 'Октябрь 2026 19', 'lecture': '3', 'mark': 'X'}]


def test_selector_posts_only_verified_filter_form_fields(tmp_path):
    calls = []
    def handle(request):
        calls.append((request.method, request.url.path, parse_qs(request.content.decode(), keep_blank_values=True)))
        body = parse_qs(request.content.decode())
        return httpx.Response(200, text=grade_html(course=body.get('course', ['71'])[0]))
    result = client(tmp_path, handle).report('grades', course='72')
    assert result['selection']['course']['id'] == '72'
    assert calls == [('GET', '/it/FinalGradesPrint.asp', {}), ('POST', '/it/FinalGradesPrint.asp',
        {'first':[''], 'program':['13'], 'term':['90'], 'course':['72']})]


def test_unavailable_attendance_is_not_interpreted_as_zero_or_perfect(tmp_path):
    def handle(request):
        return httpx.Response(200, text=attendance_html(empty=True) if request.url.path.endswith('attendanceprint.asp') else grade_html())
    result = client(tmp_path, handle).report('attendance')
    assert result['available'] is False
    assert result['attendance'] is None
    assert result['unavailable_reason'] == 'no_dates_for_selected_section'


@pytest.mark.parametrize('kind', ['grades', 'attendance'])
def test_other_student_report_is_rejected_without_returning_their_data(tmp_path, kind):
    from university_mcp.client import PortalError
    def handle(request):
        return httpx.Response(200, text=attendance_html(other=True) if request.url.path.endswith('attendanceprint.asp') else grade_html(other=kind=='grades'))
    with pytest.raises(PortalError, match='own|student'):
        client(tmp_path, handle).report(kind)


@pytest.mark.parametrize('status,location', [(302, 'https://attacker.invalid/'), (302, '/Account/Login'), (403, None)])
def test_redirects_and_expired_auth_never_forward_credentials(tmp_path, status, location):
    from university_mcp.client import PortalError
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={'Location': location} if location else {})
    with pytest.raises(PortalError) as exc:
        client(tmp_path, handle).report('grades')
    assert len(calls) == 1
    assert SECRET not in str(exc.value)


@pytest.mark.parametrize('kwargs', [{'course':'99999'}, {'term':'x&student=2'}, {'kind':'transcript'}])
def test_unknown_selection_and_route_are_rejected(tmp_path, kwargs):
    from university_mcp.client import PortalError
    with pytest.raises(PortalError):
        client(tmp_path).report(**({'kind':'grades'} | kwargs))


def test_auth_file_requires_private_permissions_and_pinned_identity(tmp_path):
    from university_mcp.client import PortalError
    with pytest.raises(PortalError):
        client(tmp_path, auth_update={'student_id':''})
    path = tmp_path / 'auth.json'; path.chmod(0o644)
    from university_mcp.it_portal import ITPortalClient
    with pytest.raises(PortalError): ITPortalClient(path)


@pytest.mark.parametrize('auth_update', [{'origin':'http://it-sb.by'}, {'cookie': SECRET+'\r\nX-Inject: bad'}])
def test_invalid_origin_and_header_injection_fail_before_request(tmp_path, auth_update):
    from university_mcp.client import PortalError
    with pytest.raises(PortalError): client(tmp_path, auth_update=auth_update)


def test_login_html_and_changed_report_shape_are_actionable(tmp_path):
    from university_mcp.client import PortalError
    with pytest.raises(PortalError):
        client(tmp_path, lambda r:httpx.Response(200,text='<form><input type="password"></form>')).report('grades')


def test_filter_form_cannot_expand_allowed_fields_or_target_origin(tmp_path):
    from university_mcp.client import PortalError
    html = grade_html().replace("action='/it/FinalGradesPrint.asp'", "action='https://attacker.invalid/'")
    with pytest.raises(PortalError):
        client(tmp_path, lambda r:httpx.Response(200,text=html)).report('grades', course='72')


def test_explicit_parent_filter_cannot_be_silently_reset_by_later_selection(tmp_path):
    from university_mcp.client import PortalError
    def handle(request):
        return httpx.Response(200, text=grade_html(course='72',term='80') if request.method=='POST' else grade_html())
    with pytest.raises(PortalError, match='filter|selection'):
        client(tmp_path,handle).report('grades',term='90',course='72')


def test_reordered_grade_columns_are_rejected_instead_of_mislabelled(tmp_path):
    from university_mcp.client import PortalError
    html=grade_html().replace('<td>Тек.</td><td>в %</td><td>Фин.</td>','<td>Фин.</td><td>в %</td><td>Тек.</td>')
    with pytest.raises(PortalError, match='column'):
        client(tmp_path,lambda r:httpx.Response(200,text=html)).report('grades')


def test_cookies_echoed_in_display_fields_are_redacted(tmp_path):
    def handle(request):
        return httpx.Response(200,text=grade_html().replace('Accounting','Acc fixturesecret'))
    result=client(tmp_path,handle).report('grades')
    assert 'fixturesecret' not in json.dumps(result)
