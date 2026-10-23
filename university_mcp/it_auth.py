"""Optional IT login through the normal own-account form and official session bridge."""
import re
from urllib.parse import urljoin,urlsplit

from bs4 import BeautifulSoup
import httpx

from .client import AuthenticationError
from .it_portal import IT_ORIGIN,ITPortalClient

LOGIN_PATH='/it/Login.aspx'
BRIDGE='/it/SessionTransfer/SessionTransfer.aspx?dir=2asp&url=~/it/FinalGradesPrint.asp'
GRADE='/it/FinalGradesPrint.asp'


def _follow(http,response):
    for _ in range(5):
        if not response.is_redirect:return response
        target=urljoin(str(response.url),response.headers.get('location',''))
        parts=urlsplit(target)
        if (parts.scheme!='https' or parts.netloc!='it-sb.by' or parts.path not in ('/DashBoard','/',GRADE)):
            raise AuthenticationError('IT login returned an unsupported redirect. Use the normal browser login.')
        response=http.get(target,follow_redirects=False)
    raise AuthenticationError('IT login redirected too many times. Use the normal browser login.')


def password_login_it(username,password,*,http=None):
    own_http=http is None
    http=http or httpx.Client(timeout=30,follow_redirects=False)
    try:
        page=http.get(IT_ORIGIN+'/DashBoard',follow_redirects=False)
        page.raise_for_status()
        soup=BeautifulSoup(page.content,'html.parser')
        field=soup.find('input',attrs={'name':'p','type':'password'})
        form=field.find_parent('form') if field else None
        target=urljoin(IT_ORIGIN+'/DashBoard',form.get('action','')) if form else ''
        if (not form or str(form.get('method','')).lower()!='post'
                or target!=IT_ORIGIN+LOGIN_PATH+'?returnUrl=L0Rhc2hCb2FyZA=='):
            raise AuthenticationError('IT login form changed or needs browser verification. Use the normal browser login.')
        data={}
        allowed={'__VIEWSTATE','__VIEWSTATEGENERATOR','__EVENTVALIDATION','__RequestVerificationToken'}
        for item in form.find_all('input'):
            name=item.get('name');kind=item.get('type','text').lower()
            if kind=='hidden' and name in allowed:data[name]=item.get('value','')
            elif name=='bnEnter' and kind=='submit':data[name]=item.get('value','')
            elif name not in ('l','p'):
                raise AuthenticationError('IT login fields changed. Use the normal browser login.')
        data.update(l=username,p=password)
        response=_follow(http,http.post(target,data=data,follow_redirects=False))
        response.raise_for_status()
        if BeautifulSoup(response.content,'html.parser').select_one('input[type="password"]'):
            raise AuthenticationError('IT login failed. Check credentials or sign in through the browser.')
        response=_follow(http,http.get(IT_ORIGIN+BRIDGE,follow_redirects=False))
        response.raise_for_status()
        # The bridge may return a fixed navigation script instead of a redirect;
        # request the known report directly rather than executing page scripts.
        response=http.get(IT_ORIGIN+GRADE,follow_redirects=False)
        if response.is_redirect:raise AuthenticationError('IT session bridge did not authorize the report.')
        response.raise_for_status()
        soup=BeautifulSoup(response.content,'html.parser')
        identity=[]
        for table in soup.find_all('table'):
            rows=table.find_all('tr')
            for n,row in enumerate(rows):
                cells=[' '.join(c.get_text(' ',strip=True).split()) for c in row.find_all(['td','th'],recursive=False)]
                if cells[:2]!=['ID','Имя студента']:continue
                for r in rows[n+1:]:
                    values=[' '.join(c.get_text(' ',strip=True).split()) for c in r.find_all(['td','th'],recursive=False)]
                    if values and values[0]=='Среднее:':continue
                    if len(values)<2 or not values[0].isascii() or not values[0].isdecimal() or not values[1]:
                        raise AuthenticationError('IT report does not identify a single own student.')
                    identity.append(values[:2])
        if len(identity)!=1:raise AuthenticationError('IT report does not identify exactly one own student.')
        cookie=[]
        for item in http.cookies.jar:
            if item.domain.lstrip('.')=='it-sb.by' and re.fullmatch(r'ASPSESSIONID[A-Z0-9]*',item.name):
                cookie.append(item.name+'='+item.value)
        auth={'origin':IT_ORIGIN,'cookie':'; '.join(cookie),'student_id':identity[0][0],'student_name':identity[0][1]}
        verified=ITPortalClient._from_auth(auth,http).report('grades')
        return auth,verified
    except AuthenticationError:raise
    except Exception:
        raise AuthenticationError('IT login could not be completed. Retry or sign in through the browser; no password was saved.') from None
    finally:
        if own_http:http.close()
