"""Read-only own-student reports from the fixed IT-system origin.

The legacy pages use POST for read-only filter selection. Never submit their
similarly named grade/attendance editing forms. No login/password automation.
"""
from datetime import datetime, timezone
from pathlib import Path
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import httpx

from .client import AuthenticationError, PortalError
from .models import redact
from .private import load_private_json

IT_ORIGIN = 'https://it-sb.by'
REPORT_PATHS = {'grades': '/it/FinalGradesPrint.asp', 'attendance': '/it/attendanceprint.asp'}
FILTERS = ('program', 'term', 'course', 'section')


def _text(node):
    return ' '.join(node.get_text(' ', strip=True).split())


def _cells(row):
    return [_text(c) or None for c in row.find_all(['td', 'th'], recursive=False)]


def _grid(table):
    """Expand bounded HTML spans so attendance month/day headers align."""
    result, pending = [], {}
    for row in table.find_all('tr'):
        values, column = [], 0
        def fill_pending():
            nonlocal column
            while column in pending:
                text, remaining = pending[column]
                values.append(text)
                if remaining == 1:
                    del pending[column]
                else:
                    pending[column] = (text, remaining - 1)
                column += 1
        for cell in row.find_all(['td', 'th'], recursive=False):
            fill_pending()
            try:
                colspan, rowspan = int(cell.get('colspan', 1)), int(cell.get('rowspan', 1))
            except (ValueError, TypeError):
                raise PortalError('IT report table layout changed; cannot safely parse it.') from None
            if not 1 <= colspan <= 200 or not 1 <= rowspan <= 200:
                raise PortalError('IT report table layout exceeds supported limits.')
            text = _text(cell) or None
            for _ in range(colspan):
                values.append(text)
                if rowspan > 1:
                    pending[column] = (text, rowspan - 1)
                column += 1
        fill_pending()
        result.append(values)
        if len(result) > 1000 or len(values) > 500:
            raise PortalError('IT report table exceeds supported limits.')
    return result


class ITPortalClient:
    """A separate, optional source of official IT grades and attendance.

    Auth JSON must contain origin, cookie, student_id and student_name. The
    identity pins are taken from the user's own authenticated grades page by
    local setup, never supplied by a report caller. Missing filters preserve
    the server's current selection; they do not imply all courses or terms.
    """
    def __init__(self, auth_file: Path, *, http: httpx.Client | None = None):
        try:
            auth = load_private_json(auth_file)
        except (OSError, ValueError, TypeError):
            raise AuthenticationError('IT authentication is missing or invalid. Use the local login wizard.') from None
        self._initialize(auth, http)

    @classmethod
    def _from_auth(cls, auth, http):
        instance = cls.__new__(cls)
        instance._initialize(auth, http)
        return instance

    def _initialize(self, auth, http):
        try:
            if auth.get('origin') != IT_ORIGIN:
                raise ValueError('origin')
            cookie = auth.get('cookie', '')
            identity = str(auth.get('student_id', ''))
            name = auth.get('student_name', '')
            if (not isinstance(cookie, str) or not cookie
                    or any(ord(c) < 32 or ord(c) == 127 for c in cookie)
                    or not all(re.fullmatch(r'ASPSESSIONID[A-Z0-9]*=[A-Za-z0-9]+', part.strip())
                               for part in cookie.split(';'))
                    or not identity.isascii() or not identity.isdecimal()
                    or not isinstance(name, str) or not name.strip()):
                raise ValueError('session and own-student identity required')
        except (OSError, ValueError, TypeError, AttributeError):
            raise AuthenticationError('IT authentication is missing or invalid. Use a private local auth file with the own-student identity pin.') from None
        self._cookie, self._student_id = cookie, identity
        self._student_name = ' '.join(name.split())
        self.http = http or httpx.Client(timeout=httpx.Timeout(40, connect=15), follow_redirects=False)

    def close(self):
        self.http.close()

    def _page(self, kind, data=None):
        try:
            response = self.http.request('POST' if data is not None else 'GET',
                IT_ORIGIN + REPORT_PATHS[kind], data=data,
                headers={'Cookie': self._cookie}, follow_redirects=False)
            if response.is_redirect or response.status_code in (401, 403):
                raise AuthenticationError('IT session expired or redirected. Refresh the local session in the authenticated own account.')
            response.raise_for_status()
            if len(response.content) > 4_000_000:
                raise PortalError('IT report exceeds supported size.')
            soup = BeautifulSoup(response.content, 'html.parser')
        except httpx.HTTPError as exc:
            raise PortalError(f'IT report request failed ({type(exc).__name__}); check connectivity or refresh the local session.') from None
        if soup.select_one('input[type="password"]'):
            raise AuthenticationError('IT session requires login. Refresh the local session in the browser.')
        self._selection(soup)
        return soup

    def _selection(self, soup):
        selected, options = {}, {}
        for name in FILTERS:
            element = soup.find('select', attrs={'name': name})
            if element is None:
                raise PortalError('IT report filters are unavailable; session expired or page layout changed.')
            rows = [{'id': o.get('value', ''), 'label': _text(o)} for o in element.find_all('option')]
            chosen = element.find('option', selected=True) or element.find('option')
            if not rows or chosen is None:
                raise PortalError('IT report has no selectable values for this account.')
            options[name] = rows
            selected[name] = {'id': chosen.get('value', ''), 'label': _text(chosen)}
        return selected, options

    def _grade(self, soup):
        for table in soup.find_all('table'):
            rows = table.find_all('tr')
            header_index = next((i for i, row in enumerate(rows)
                if len(c := _cells(row)) >= 2 and c[0] == 'ID' and c[1] == 'Имя студента'), None)
            if header_index is None:
                continue
            headers = _cells(rows[header_index])
            students = []
            for row in rows[header_index + 1:]:
                cells = _cells(row)
                if cells and cells[0] == 'Среднее:':
                    continue
                if (len(cells) != len(headers) or not cells[0]
                        or cells[0] != self._student_id
                        or not cells[1] or cells[1].casefold() != self._student_name.casefold()):
                    raise PortalError('IT report does not contain only the pinned own student; refusing other student data.')
                students.append(cells)
            if len(students) != 1:
                raise PortalError('IT grades do not identify exactly one pinned own student.')
            cells = students[0]
            count = len(headers) - 7
            if (count < 0 or headers[2:2 + count] != [f'#{i+1}' for i in range(count)]
                    or headers[2 + count:] != ['Тек.', 'в %', 'Фин.', 'в %', 'Абс.']):
                raise PortalError('IT grade columns changed; refusing ambiguous grades.')
            values = cells[2 + count:]
            return {'checkpoints': [{'number': headers[2+i], 'value': cells[2+i]} for i in range(count)],
                'current': values[0], 'current_percent': values[1], 'final': values[2],
                'final_percent': values[3], 'absolute': values[4]}
        raise PortalError('IT own-student grade table is unavailable or its layout changed.')

    def _attendance(self, soup):
        if 'В этой подгруппе нет ни одной даты' in soup.get_text(' ', strip=True):
            return None
        for table in soup.find_all('table'):
            if 'Номер лекции' not in table.get_text():
                continue
            rows = _grid(table)
            lecture_index = next((i for i, r in enumerate(rows) if r and r[0] == 'Номер лекции'), None)
            if lecture_index is None or lecture_index != 2 or len(rows) != lecture_index + 2:
                raise PortalError('IT attendance does not identify exactly one own student.')
            cells = rows[-1]
            if (len(cells) < 4 or not cells[1]
                    or cells[1].casefold() != self._student_name.casefold()
                    or len(rows[0]) != len(cells) or len(rows[1]) != len(cells)
                    or len(rows[2]) != len(cells)):
                raise PortalError('IT attendance does not match the pinned own student or report layout.')
            return {'sessions': [{'date_label': ' '.join(x for x in (rows[0][i], rows[1][i]) if x),
                                 'lecture': rows[2][i], 'mark': cells[i]}
                                for i in range(2, len(cells) - 2)],
                    'attended_total': cells[-2], 'percent': cells[-1],
                    'marks_are_raw': True}
        raise PortalError('IT own-student attendance table is unavailable or its layout changed.')

    def report(self, kind='grades', *, program=None, term=None, course=None, section=None):
        if kind not in REPORT_PATHS:
            raise PortalError('Only own-student grades and attendance reports are allowed.')
        requested = dict(zip(FILTERS, (program, term, course, section)))
        # Attendance lacks a student ID, so authenticate the session against the
        # pinned numeric ID on the grade page before matching its attendance name.
        own_grades = self._page('grades')
        self._grade(own_grades)
        soup = own_grades if kind == 'grades' else self._page(kind)
        for name in FILTERS:
            if requested[name] is None:
                continue
            value = str(requested[name])
            selected, options = self._selection(soup)
            if value not in {o['id'] for o in options[name]}:
                raise PortalError('Requested IT filter value is not available to this account. Use returned options.')
            if selected[name]['id'] == value:
                continue
            element = soup.find('select', attrs={'name': name})
            form = element.find_parent('form')
            if (form is None or str(form.get('method', '')).lower() != 'post'
                    or urljoin(IT_ORIGIN + REPORT_PATHS[kind], form.get('action', '')) != IT_ORIGIN + REPORT_PATHS[kind]):
                raise PortalError('IT filter form target changed; refusing to submit it.')
            expected = set(FILTERS[:FILTERS.index(name)]) | {'first'}
            inputs = form.find_all('input')
            if (len(inputs) != len(expected) or {e.get('name') for e in inputs} != expected
                    or len(form.find_all('select')) != 1
                    or any(e.get('type', '').lower() != 'hidden' for e in inputs)):
                raise PortalError('IT filter form fields changed; refusing a potentially state-changing form.')
            data = {e['name']: e.get('value', '') for e in inputs}
            if data.get('first') != '' or any(data[x] != selected[x]['id'] for x in expected - {'first'}):
                raise PortalError('IT filter form selection changed; refusing inconsistent fields.')
            data[name] = value
            soup = self._page(kind, data)
            if self._selection(soup)[0][name]['id'] != value:
                raise PortalError('IT portal did not apply the requested filter; report may be unavailable.')
            if kind == 'grades':
                self._grade(soup)
        selected, options = self._selection(soup)
        if any(v is not None and selected[k]['id'] != str(v) for k, v in requested.items()):
            raise PortalError('IT portal changed an explicitly requested filter; refusing an inconsistent selection.')
        value = self._grade(soup) if kind == 'grades' else self._attendance(soup)
        result = {'source': 'it_system', 'source_url': IT_ORIGIN + REPORT_PATHS[kind],
            'report': kind, 'scope': 'selected_course_and_term',
            'selection': selected, 'options': options,
            'selection_defaults': 'server_current_selection; omitted filters do not mean all terms or courses',
            'student': {'id': self._student_id, 'name': self._student_name},
            'available': value is not None, kind: value,
            'fetched_at': datetime.now(timezone.utc).isoformat(),
            'read_only': True}
        if value is None:
            result['unavailable_reason'] = 'no_dates_for_selected_section'
        # Never attach the page HTML, calculation links, cookies or form data.
        for credential in [self._cookie, *(part.strip().split('=', 1)[1] for part in self._cookie.split(';'))]:
            result = redact(result, credential)
        return result
