"""Check the public Windows Free package using only synthetic public fixtures.

Needs Python 3.10+, Docker Desktop and an unpacked Free release. No private
repository, pip dependencies, real session, EPGU API key or scheduled job.
"""
import argparse
import base64
import copy
import json
from pathlib import Path
import socket
import subprocess
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True,
                        help='new directory outside Git; existing installations are refused')
    args = parser.parse_args()
    package, output = args.package.resolve(), args.output.resolve()
    if any((p / '.git').exists() for p in (output, *output.parents)):
        parser.error('output must be outside Git')
    if output.exists() and any(output.iterdir()):
        parser.error('output must be empty')
    setup = package / 'tools/epgu-bell-setup.exe'
    if not setup.is_file():
        parser.error('unpack the Windows Free release first; setup executable is missing')
    output.mkdir(parents=True, exist_ok=True)
    install = output / 'installation'
    fixture = json.loads(Path(__file__).with_name('snapshot.mock.json').read_text('utf-8'))
    receipt = {'mock_only': True, 'private_addons_required': False, 'edition': 'free'}
    logs, started = [], False

    def run(action, *extra, success=True):
        result = subprocess.run([str(setup), action, '--package', str(package),
            '--install-root', str(install), *extra], capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=360)
        logs.append({'action': action, 'exit_code': result.returncode,
                     'stdout': result.stdout, 'stderr': result.stderr})
        if (result.returncode == 0) != success:
            raise RuntimeError('installer action failed: ' + action + '; see installer-log.json')

    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        run('Verify')
        run('Load')
        run('Initialize', '--organization-id', 'ORG-DEMO')
        config_file = install / 'config.json'
        original = config_file.read_bytes()
        run('Initialize', '--organization-id', 'OTHER', success=False)
        assert config_file.read_bytes() == original
        receipt['existing_configuration_preserved'] = True
        run('Demo')
        demo = json.loads((install / 'data/latest-demo.json').read_text('utf-8'))
        assert demo['stored_unique'] == 2 and demo['second_organization_rejected']
        receipt['headless_collect_demo'] = demo

        # Only a loopback mock origin is configured. No portal session exists.
        config = {'connection_id': 'demo', 'organization_id': 'ORG-DEMO', 'contour': 'test',
            'notifications_url': fixture['source']['url'],
            'allowed_origins': ['http://127.0.0.1:35503'],
            'context_selector': '[data-org-id]', 'context_attribute': 'data-org-id'}
        config_file.write_text(json.dumps(config), encoding='utf-8')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        started = True
        run('StartApi', '--port', str(port))
        token = (install / 'session/api-token.txt').read_text('utf-8').strip()

        def request(path, data=None, authorized=True):
            headers = {'Content-Type': 'application/json'}
            if authorized:
                headers['Authorization'] = 'Bearer ' + token
            body = json.dumps(data).encode() if data is not None else None
            req = urllib.request.Request(f'http://127.0.0.1:{port}' + path, body, headers)
            try:
                with client.open(req, timeout=30) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)

        assert request('/v1/notifications', authorized=False)[0] == 401
        status, capabilities = request('/v1/capabilities')
        assert status == 200 and capabilities['max_organizations'] == 1
        assert capabilities['multi_organization_module'] is False
        assert request('/v1/snapshots/import', fixture)[0] == 201
        status, first = request('/v1/notifications?limit=1')
        assert status == 200 and first['pagination']['has_more']
        from urllib.parse import urlencode
        query = urlencode({'limit': 1, 'cursor': first['pagination']['next_cursor']})
        status, second = request('/v1/notifications?' + query)
        assert status == 200 and not second['pagination']['has_more']
        assert first['items'][0]['record']['id'] != second['items'][0]['record']['id']
        records = request('/v1/notifications')[1]['items']
        assert len(records) == 2
        by_id = {row['record']['id']: row['record'] for row in records}
        for expected in fixture['notifications']:
            actual = by_id[expected['id']]
            for field in ('title', 'body', 'sender', 'time_display'):
                assert actual[field] == expected[field]
                base64.b64decode(actual[field]['data'], validate=True).decode('utf-8')
        assert request('/v1/snapshots/import', fixture)[0] == 201
        assert len(request('/v1/notifications')[1]['items']) == 2
        wrong = copy.deepcopy(fixture)
        wrong['context']['organization_id'] = 'OTHER'
        status, refused = request('/v1/snapshots/import', wrong)
        assert status in (400, 403) and 'context' in refused.get('error', '')
        assert len(request('/v1/notifications')[1]['items']) == 2
        receipt.update(http_api=True, pagination=True, json_utf8_base64=True,
                       repeat_added=0, unique_notifications=2,
                       unauthorized_rejected=True, wrong_organization_rejected=True)
    finally:
        if started:
            run('Stop')
            installed = json.loads((install / 'installation.json').read_text('utf-8'))
            check = subprocess.run(['docker', 'inspect', 'epgu-bell-free-' + installed['id']],
                                   capture_output=True, timeout=30)
            assert check.returncode != 0, 'the task container still exists'
            with socket.socket() as probe:
                probe.settimeout(2)
                assert probe.connect_ex(('127.0.0.1', port)) != 0
            receipt['own_container_stopped'] = True
        (install / 'session/api-token.txt').unlink(missing_ok=True)
        (output / 'installer-log.json').write_text(json.dumps(logs, ensure_ascii=False, indent=2), 'utf-8')
    (output / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
