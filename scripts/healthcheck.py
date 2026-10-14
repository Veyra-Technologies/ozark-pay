"""Probe loopback with an explicitly allowed Host and check dependency readiness."""
import os
from urllib.request import Request, urlopen


def main():
    # Docker already injects this setting. Avoid importing the application and
    # Pydantic on every probe: their cold start can exhaust the health timeout.
    hosts = [value.strip() for value in os.environ.get('TRUSTED_HOSTS', 'localhost,127.0.0.1').split(',')]
    host = next((value for value in hosts if value and '*' not in value), None)
    if host is None:
        raise SystemExit('healthcheck requires an explicit trusted Host')
    for path in ('/health', '/ready'):
        request = Request(f'http://127.0.0.1:8000{path}', headers={'Host': host})
        with urlopen(request, timeout=2) as response:
            if response.status != 200:
                raise SystemExit(1)


if __name__ == '__main__':
    main()
