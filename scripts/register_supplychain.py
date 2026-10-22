"""Register reusable research cards in an explicitly selected QuantLab workspace."""
from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from quantlab_supplychain.catalog import card_sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    parser.add_argument('--email', required=True)
    parser.add_argument('--workspace', required=True, help='Explicit destination workspace ID')
    args = parser.parse_args()
    session = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(route, data=None):
        headers = {'Content-Type': 'application/json', 'X-Requested-With': 'supplychain-catalog', 'X-Workspace-Id': args.workspace}
        payload = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(args.url.rstrip('/') + route, data=payload, headers=headers)
        with session.open(req, timeout=60) as response:
            return json.load(response)

    request('/api/auth/login', {'email': args.email, 'password': getpass.getpass('QuantLab password: ')})
    profile = request('/api/auth/me')
    if not any(w['id'] == args.workspace for w in profile['workspaces']):
        raise SystemExit('Destination workspace does not belong to this account')
    for code in card_sources():
        result = request('/api/workflows/definitions', {'code': code})
        print('Registered', result.get('kind', 'research card'), result.get('version', ''))
    print('Card catalog ready. Import your own source files before constructing a workflow.')


if __name__ == '__main__':
    main()
