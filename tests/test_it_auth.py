"""Normal IT login: credentials stay on the fixed origin and identity comes from own report."""
from urllib.parse import parse_qs
import httpx
import pytest
from university_mcp.client import AuthenticationError

GRADE='''<select name="program"><option value="13">Program</option></select><select name="term"><option value="2026">Autumn2026</option></select><select name="course"><option value="42">Example</option></select><select name="section"><option value="1">One</option></select><table><tr><td>ID</td><td>Имя студента</td><td>#1</td><td>Тек.</td><td>в %</td><td>Фин.</td><td>в %</td><td>Абс.</td></tr><tr><td>9991</td><td>Example Student</td><td>8</td><td>8</td><td>80</td><td></td><td></td><td>8</td></tr></table>'''


def login_transport(*,bad=False,foreign=False):
    calls=[]
    def handle(r):
        calls.append(r)
        assert r.url.host=='it-sb.by'
        if r.url.path=='/DashBoard':
            if 'SessionId=NETSESSION' in r.headers.get('cookie',''):return httpx.Response(200,text='<a href="/it/SessionTransfer/SessionTransfer.aspx">Grades</a>')
            return httpx.Response(200,text='<form method="post" action="/it/Login.aspx?returnUrl=L0Rhc2hCb2FyZA=="><input name="l"><input type="password" name="p"><input type="submit" name="bnEnter" value="Войти"></form>')
        if r.url.path=='/it/Login.aspx':
            a=parse_qs(r.content.decode());assert a['l']==['student'] and a['p']==['private-password']
            if foreign:return httpx.Response(302,headers={'location':'https://foreign.example/'})
            if bad:return httpx.Response(200,text='<input type="password" name="p">Wrong password: private-password')
            return httpx.Response(302,headers={'location':'/DashBoard','set-cookie':'SessionId=NETSESSION; Path=/; Secure'})
        if r.url.path.endswith('SessionTransfer.aspx'):
            assert r.url.params['dir']=='2asp'
            return httpx.Response(302,headers={'location':'/it/FinalGradesPrint.asp','set-cookie':'ASPSESSIONIDTEST=FIXTURESESSION; Path=/; Secure'})
        if r.url.path.endswith('FinalGradesPrint.asp'):
            assert 'ASPSESSIONIDTEST=FIXTURESESSION' in r.headers.get('cookie','')
            return httpx.Response(200,text=GRADE)
        raise AssertionError(r.url)
    return httpx.Client(transport=httpx.MockTransport(handle)),calls


def test_it_normal_login_pins_own_student_and_returns_only_asp_session():
    from university_mcp.it_auth import password_login_it
    http,calls=login_transport()
    auth,report=password_login_it('student','private-password',http=http)
    assert auth=={'origin':'https://it-sb.by','cookie':'ASPSESSIONIDTEST=FIXTURESESSION','student_id':'9991','student_name':'Example Student'}
    assert report['grades']['current']=='8' and report['available']
    assert sum(r.method=='POST' for r in calls)==1


@pytest.mark.parametrize('mode',['bad','foreign'])
def test_it_failed_login_or_foreign_redirect_never_leaks_password(mode):
    from university_mcp.it_auth import password_login_it
    http,_=login_transport(**{mode:True})
    with pytest.raises(AuthenticationError) as exc:password_login_it('student','private-password',http=http)
    assert 'private-password' not in str(exc.value) and 'NETSESSION' not in str(exc.value)
