import importlib.util
import json
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('planner_api', ROOT / 'api/index.py')
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

class PlanningAPI(unittest.TestCase):
    def setUp(self):
        self.server = patch.object(api, '_server_key', return_value='')
        self.server.start()
        self.addCleanup(self.server.stop)
        self.access = patch.dict(api.os.environ, {'PLANNER_ACCESS_CODE': ''})
        self.access.start()
        self.addCleanup(self.access.stop)

    def payload(self):
        return {'apiKey': 'test-key', 'model': api.MODELS[0]['id'], 'text': 'site brief'}

    def test_original_and_rewritten_routes(self):
        from io import BytesIO
        for path in ('/api/models', '/api/models?cache=1', '/api/index?endpoint=models'):
            request = object.__new__(api.handler)
            request.path = path
            with patch.object(request, '_respond') as respond:
                request.do_GET()
                self.assertEqual(respond.call_args.args[0], 200)
                self.assertEqual(respond.call_args.args[1]['models'], api.MODELS)
        for path in ('/api/plan', '/api/index?endpoint=plan'):
            request = object.__new__(api.handler)
            request.path = path
            request.headers = {'Content-Length': '2'}
            request.rfile = BytesIO(b'{}')
            with patch.object(request, '_respond') as respond:
                request.do_POST()
                self.assertEqual(respond.call_args.args[0], 400)
        request.path = '/api/index?endpoint=unknown'
        with patch.object(request, '_respond') as respond:
            request.do_GET()
            self.assertEqual(respond.call_args.args[0], 404)

    def test_homepage_is_served_as_html(self):
        from io import BytesIO
        for path in ('/', '/index.html', '/api/index?endpoint=panel'):
            request = object.__new__(api.handler)
            request.path = path
            request.wfile = BytesIO()
            with patch.object(request, 'send_response') as status, patch.object(request, 'send_header') as headers, patch.object(request, 'end_headers'):
                request.do_GET()
            status.assert_called_once_with(200)
            headers.assert_any_call('Content-Type', 'text/html; charset=utf-8')
            html = request.wfile.getvalue().decode()
            self.assertIn('id="form"', html)
            self.assertIn('id="apiKey"', html)
            self.assertIn('lang="en"', html)

    def test_openrouter_request_uses_selected_model_and_key(self):
        from io import BytesIO
        response = BytesIO(json.dumps({'choices': [{'message': {'content': '{"facts": []}'}}]}).encode())
        with patch.object(api, 'urlopen', return_value=response) as call:
            result = api._RequestAgent('test-key', 'deepseek/deepseek-v4.1-flash').complete('instructions', 'brief')
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, 'https://openrouter.ai/api/v1/chat/completions')
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-key')
        self.assertEqual(json.loads(request.data)['model'], 'deepseek/deepseek-v4.1-flash')
        self.assertEqual(result['raw'], '{"facts": []}')

    def test_forbidden_error_preserves_provider_reason_and_redacts_key(self):
        from io import BytesIO
        failure = HTTPError('https://openrouter.ai', 403, 'Forbidden', {}, BytesIO(b'{"error":{"message":"Model access denied test-key"}}'))
        with patch.object(api, 'urlopen', side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, 'OpenRouter HTTP 403') as caught:
                api._RequestAgent('test-key', api.MODELS[0]['id']).complete('s', 'u')
        self.assertIn('Model access denied', str(caught.exception))
        self.assertNotIn('test-key', str(caught.exception))

    def test_interpretation_failure_is_not_reported_as_missing_fields(self):
        context = {'missing': ['gpu_count'], 'research': {}, 'semantic_trace': {'extract_error': 'OpenRouter HTTP 403: access denied'}}
        with patch.object(api, '_verify_key', return_value=True), patch('initial_input.complete_initial_information', return_value=context):
            status, body = api._plan(self.payload())
        self.assertEqual(status, 502)
        self.assertNotIn('gpu_count', json.dumps(body))
        self.assertIn('403', body['details'][0])

    def test_server_key_used_without_browser_key(self):
        with patch.object(api, '_server_key', return_value='private-server-key'), patch.object(api, '_verify_key', return_value=False) as verify:
            status, body = api._plan({'model': api.MODELS[0]['id'], 'text': 'brief'})
        verify.assert_called_once_with('private-server-key')
        self.assertEqual(status, 401)
        self.assertNotIn('private-server-key', json.dumps(body))

    def test_optional_project_access_code(self):
        with patch.dict(api.os.environ, {'PLANNER_ACCESS_CODE': 'project-code'}):
            self.assertEqual(api._plan(self.payload())[0], 401)
            with patch.object(api, '_verify_key', return_value=False):
                self.assertEqual(api._plan({**self.payload(), 'accessCode': 'project-code'})[0], 401)

    def test_v4_failure_does_not_issue_a_report(self):
        from local_input import complete_local_information
        text = "GPU count 64; roof area 2000 m2; grid capacity 1000 kW; backup duration 2 hours; UPS duration 10 minutes; training share 50%; PV peak capacity factor 0.7; electricity price USD 0.12/kWh; ambient temperature 25 C; no training burst; minimize annual cost."
        context = complete_local_information({'text': text, 'backend': 'scipy', 'interactive': False})
        context['research']['point'] = {'latitude': 33.4484, 'longitude': -112.074, 'name': 'Phoenix'}
        with patch.object(api, '_verify_key', return_value=True), patch('initial_input.complete_initial_information', return_value=context), patch('v4_simulation.evaluate', side_effect=RuntimeError('test failure')):
            status, body = api._plan(self.payload())
        self.assertEqual(status, 500)
        self.assertIn('v4 evaluation did not complete', body['error'])
        self.assertNotIn('report', body)

    def test_invalid_input(self):
        self.assertEqual(api._plan({})[0], 400)
        self.assertEqual(api._plan({**self.payload(), 'model': 'unknown'})[0], 400)
        self.assertEqual(api._plan({**self.payload(), 'text': 'x' * 12001})[0], 400)

    def test_rejected_key(self):
        with patch.object(api, '_verify_key', side_effect=HTTPError('https://openrouter.ai', 401, 'Unauthorized', {}, None)):
            self.assertEqual(api._plan(self.payload())[0], 401)

    def test_clarification_and_request_agent_cleanup(self):
        from utils import REQUEST_AGENT, APIAgent
        def complete(request):
            self.assertFalse(request['interactive'])
            self.assertIsInstance(REQUEST_AGENT.get(), api._RequestAgent)
            with patch.object(api._RequestAgent, 'complete', return_value={'raw': 'ok'}):
                self.assertEqual(APIAgent().complete('system', 'brief')['raw'], 'ok')
            return {'missing': ['gpu_count'], 'research': {}}
        with patch.object(api, '_verify_key', return_value=True), patch('initial_input.complete_initial_information', side_effect=complete):
            status, body = api._plan(self.payload())
        self.assertEqual(status, 422)
        self.assertIn('gpu_count', body['details'])
        self.assertIsNone(REQUEST_AGENT.get())

    def test_real_cached_solver_and_table(self):
        from local_input import complete_local_information
        text = "Location: Phoenix, USA; GPU count 64; roof area 2000 m2; grid capacity 1000 kW; backup duration 2 hours; UPS duration 10 minutes; training share 50%; growth multiplier 1.1; PV peak capacity factor 0.7; electricity price USD 0.12/kWh; ambient temperature 25 C; ambient swing 8 C; no training burst; minimize annual cost."
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'point.json'
            cache.write_text(json.dumps({'location_query': 'Phoenix, USA', 'point': {'latitude': 33.4484, 'longitude': -112.074, 'name': 'Phoenix'}}))
            with patch.object(sys, 'argv', ['web', '--local', '--point-cache', str(cache)]):
                context = complete_local_information({'text': text, 'backend': 'scipy', 'interactive': False})
        with patch.object(api, '_verify_key', return_value=True), patch('initial_input.complete_initial_information', return_value=context):
            status, body = api._plan(self.payload())
        self.assertEqual(status, 200, body)
        self.assertTrue(any(row['item'] == 'Optimization and audits' and row['value'] == 'Passed' for row in body['rows']))
        self.assertEqual(sum(r['category'] == 'Scenario' for r in body['rows']), 7)
        self.assertEqual(sum(r['category'] == 'Fault replay' for r in body['rows']), 8)
        self.assertNotIn('test-key', json.dumps(body))
        report = body['report']
        self.assertTrue(all(row['executed'] for row in report['validation']))
        self.assertEqual([row['version'] for row in report['validation']], ['v4.1','v4.2','v4.3','v4.4','v4.5'])
        self.assertIn('v4.5', report['html'])
        self.assertIn('Hourly operating plan', report['html'])
        self.assertIn('Equipment in each candidate', report['html'])
        self.assertIn('Conservation audit', report['html'])
        self.assertIn('not_applicable', report['html'])
        self.assertTrue(report['evaluation']['replanning'])
        self.assertGreater(len(report['html']), 10000)
        import os
        if os.environ.get('TEST_REPORT_OUTPUT'):
            Path(os.environ['TEST_REPORT_OUTPUT']).write_text(json.dumps(body))

if __name__ == '__main__':
    unittest.main()
