import importlib.util
from pathlib import Path
import unittest

import httpx

path=Path(__file__).resolve().parents[1]/'scripts/check_data_access.py'
spec=importlib.util.spec_from_file_location('data_access_diagnostic',path)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class DiagnosticsTests(unittest.TestCase):
    def test_403_mentions_classified_without_exporting_raw_body_or_cookies(self):
        response=httpx.Response(403,headers={'content-type':'text/html','set-cookie':'private-cookie'},
            request=httpx.Request('GET','https://query.wikidata.org/sparql'))
        output=module.safe_response_details(response,'Forbidden. User-Agent policy. private-token-example')
        self.assertEqual(output['http_status'],403)
        self.assertIn('user_agent_policy',output['response_mentions'])
        self.assertNotIn('private-token-example',str(output))
        self.assertNotIn('private-cookie',str(output))

    def test_json_success_has_no_guessed_reason(self):
        response=httpx.Response(200,headers={'content-type':'application/sparql-results+json'},
            request=httpx.Request('GET','https://query.wikidata.org/sparql'))
        output=module.safe_response_details(response,'{"results":{"bindings":[]}}')
        self.assertEqual(output['response_type'],'json')
        self.assertEqual(output['response_mentions'],[])
