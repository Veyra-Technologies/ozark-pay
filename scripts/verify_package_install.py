"""Run from outside the checkout after installing a wheel, optionally with a source manifest."""
import argparse
import importlib
import json
from importlib import metadata
from importlib.resources import files
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, help='JSON list of app/ and scripts/ files expected in the wheel')
    args = parser.parse_args()
    distribution = metadata.distribution('processing-platform')
    installed = {str(path).replace('\\', '/') for path in distribution.files or ()}
    required = {
        'app/main.py', 'app/core/config.py', 'app/db/session.py',
        'app/services/crossborder_operations.py', 'app/services/external_processors.py',
        'app/services/settlements.py', 'app/api/v1/auth.py', 'app/web/routes.py',
        'app/templates/cabinet.html', 'app/static/crossborder-live.js',
        'scripts/migrate.py', 'app/_branding/default.yml', 'app/_migrations/env.py', 'app/_migrations/alembic.ini',
    }
    if args.manifest:
        required.update(json.loads(args.manifest.read_text()))
    missing = required - installed
    if missing:
        raise RuntimeError(f'wheel missing {len(missing)} files: {sorted(missing)}')
    modules = sorted(path[:-3].replace('/', '.') for path in installed
                     if path.startswith('app/') and path.endswith('.py')
                     and not path.startswith('app/_migrations/'))
    for module in modules:
        importlib.import_module(module.removesuffix('.__init__'))
    from app.main import app
    from app.web.routes import templates
    from fastapi.testclient import TestClient
    from scripts.migrate import _config
    from alembic.script import ScriptDirectory
    for resource in (files('app') / 'templates').rglob('*.html'):
        templates.get_template(str(resource.relative_to(files('app') / 'templates')))
    with TestClient(app, base_url='http://localhost') as client:
        assert client.get('/health').status_code == 200
        assert client.get('/static/crossborder-live.js').status_code == 200
        assert client.get('/staff/login').status_code == 200
    heads = ScriptDirectory.from_config(_config()).get_heads()
    assert heads == ['0034_canonical_usdt'], heads
    print(f'application_package_verification=OK modules={len(modules)} required_files={len(required)} head={heads[0]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
