import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from app.httpd import create_server
from app.service import Service


class ForecastWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / 'source.json'
        self.source.write_text(Path('data/source.json').read_text())
        self.service = Service(self.source, Path(self.temporary.name) / 'state.sqlite3')
        self.request = {
            'request_id': 'forecast-check', 'operation': 'collection_forecast',
            'as_of': '2026-09-30T23:59:59Z',
            'options': {'weeks': 8, 'paths': 500, 'scenarios': [
                {'name': 'c3延迟两周', 'clients': {'c3': {'delay_days': 14}}}]}}

    def test_forecast_persists_and_replays(self):
        result = self.service.run(self.request)
        saved = self.service.repository.analysis('forecast-check')
        self.assertEqual(result, saved)
        self.assertEqual(self.service.run(self.request), result)
        self.assertEqual(result['reconciliation']['open_cents'],
                         result['scenarios'][0]['expected_total_cents'])
        self.assertEqual([scenario['name'] for scenario in result['scenarios']],
                         ['baseline', 'c3延迟两周'])

    def test_options_participate_in_request_identity(self):
        self.service.run(self.request)
        changed = dict(self.request, options={'weeks': 6, 'paths': 500})
        with self.assertRaises(ValueError):
            self.service.run(changed)

    def test_forecast_http_roundtrip(self):
        server = create_server(self.service)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            request = Request(base + '/v1/analyses',
                              data=json.dumps(self.request).encode(),
                              headers={'Content-Type': 'application/json'})
            with urlopen(request) as response:
                result = json.load(response)
            self.assertEqual(result['operation'], 'collection_forecast')
            with urlopen(base + '/v1/analyses/forecast-check') as response:
                self.assertEqual(result, json.load(response))
            catalog = json.load(urlopen(base + '/v1/catalog'))
            self.assertIn('collection_forecast', catalog['operations'])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
