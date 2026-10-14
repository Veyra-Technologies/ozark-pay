"""Return successful/failed form submissions to a validated local profile URL."""
import re
from urllib.parse import urlsplit, parse_qsl, urlencode
from fastapi.routing import APIRoute

PROFILE_PATH = re.compile(r'^/(?:staff/)?cabinet/profiles/(?:merchants|traders)/[0-9a-f-]{36}$')


class ProfileReturnRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request):
            response = await handler(request)
            if request.method != 'POST' or response.status_code != 303:
                return response
            location = urlsplit(response.headers.get('location', ''))
            if location.netloc or location.path not in ('/cabinet', '/staff/cabinet'):
                return response
            form = await request.form()
            target = urlsplit(str(form.get('return_to', '')))
            if target.scheme or target.netloc or not PROFILE_PATH.fullmatch(target.path):
                return response
            params = {k:v for k,v in parse_qsl(target.query) if k in ('tab','page')}
            params.update({k:v for k,v in parse_qsl(location.query) if k != 'section'})
            response.headers['location'] = target.path + ('?' + urlencode(params) if params else '')
            return response
        return wrapped
