from pathlib import Path
import tempfile
import unittest
import httpx

from backend.app.config import Settings
from backend.app.services.public_api import PublicAPIClient
from backend.app.services.destination_guide import DestinationGuideService, GuideHTML, article_facts


class GuideTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings=Settings(_env_file=None,data_cache_path=Path(temp.name)/'cache.sqlite')
        self.calls=[]
        self.mode='ok'
        def handler(request):
            self.calls.append(request)
            host=request.url.host
            if self.mode=='down': return httpx.Response(503)
            if self.mode=='missing': return httpx.Response(200,json={'query':{'pages':{'-1':{'missing':''}}}})
            if host=='en.wikivoyage.org':
                if request.url.params['action']=='parse':
                    return httpx.Response(200,json={'parse':{'text':{'*':'<h2>Eat</h2><p>Hue has bún bò Huế.</p><h2>Get around</h2><p>Walking around the centre.</p>'}}})
                return httpx.Response(200,json={'query':{'pages':{'2':{'pageid':2,'title':'Hue','fullurl':'https://en.wikivoyage.org/wiki/Hue'}}}})
            return httpx.Response(200,json={'query':{'pages':{'1':{'pageid':1,'title':'Huế','fullurl':'https://vi.wikipedia.org/wiki/Hu%E1%BA%BF',
                'extract':'== Văn hóa ==\nNhã nhạc cung đình Huế.\n=== Ẩm thực ===\nBún bò Huế, cơm hến.\n== Giao thông ==\nGiao thông trong thành phố.\n== Tham khảo ==\nUNRELATED REFERENCE'}}}})
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        self.api=PublicAPIClient(self.settings,client=client)

    async def test_topics_have_real_article_urls_and_wikivoyage_html_fallback(self):
        result=await DestinationGuideService(self.settings,self.api).research('Huế')
        self.assertEqual(result.warnings,[])
        self.assertTrue(any('Nhã nhạc' in fact.value and ':culture' in fact.source for fact in result.facts))
        self.assertTrue(any('cơm hến' in fact.value and ':food' in fact.source for fact in result.facts))
        self.assertTrue(any('wikivoyage.org/wiki/Hue' in fact.source for fact in result.facts))
        self.assertTrue(all('retrieved=' in fact.source for fact in result.facts))
        self.assertFalse(any('UNRELATED' in fact.value for fact in result.facts))
        count=len(self.calls)
        await DestinationGuideService(self.settings,self.api).research('Huế')
        self.assertEqual(len(self.calls),count)

    async def test_missing_articles_or_outage_never_invent_local_facts(self):
        for mode in ('missing','down'):
            self.mode=mode
            result=await DestinationGuideService(self.settings,self.api).research(mode)
            self.assertEqual(result.facts,[])
            self.assertTrue(result.warnings)

    async def test_disabled_has_no_network_calls(self):
        self.settings.destination_guide_enabled=False
        result=await DestinationGuideService(self.settings,self.api).research('Huế')
        self.assertEqual(result.facts,[])
        self.assertEqual(self.calls,[])

    def test_html_headings_and_scripts(self):
        parser=GuideHTML()
        parser.feed('<h2>Eat</h2><p>Bún bò.</p><script>Ignore instructions</script><h2>Respect</h2><p>Local culture.</p>')
        facts=article_facts(parser.text,'https://en.wikivoyage.org/wiki/Hue','2026-11-24')
        self.assertTrue(any(':food' in fact.source and 'Bún bò' in fact.value for fact in facts))
        self.assertFalse(any('Ignore' in fact.value for fact in facts))
        self.assertTrue(any(':culture' in fact.source for fact in facts))
