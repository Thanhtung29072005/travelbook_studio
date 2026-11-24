"""Diagnose data access in the backend's Python environment; no LLM or server."""
import argparse
import asyncio
import json
from pathlib import Path
import platform
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import getproxies

import httpx

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.app.config import Settings
from backend.app.services.public_api import PublicAPIClient, DataServiceError
from backend.app.services.places import PlacesService
from backend.app.services.wikidata_places import WikidataPlaces


def safe_response_details(response,body):
    text=body[:8192].casefold()
    markers={
        'user_agent_policy':('user-agent','user agent'),
        'network_policy':('robots_denied','policy_denied'),
        'rate_limit':('rate limit','too many requests'),
        'challenge':('cloudflare','captcha'),
        'access_denied':('access denied','request blocked','forbidden'),
    }
    content_type=response.headers.get('content-type','').lower()
    return {'host':urlsplit(str(response.url)).hostname,'http_status':response.status_code,
        'response_type':'json' if 'json' in content_type else 'html' if 'html' in content_type else 'other',
        'response_mentions':[name for name,phrases in markers.items() if any(phrase in text for phrase in phrases)],
        'retry_after_present':bool(response.headers.get('retry-after'))}


async def main(destination):
    report={'python_version':platform.python_version(),'virtualenv':sys.prefix!=sys.base_prefix,
        'destination':destination,'status':'failed','responses':[]}
    try:
        settings=Settings()
        proxies=getproxies()
        report['use_system_proxy']=settings.data_trust_env
        report['proxy_configured']={name:bool(proxies.get(name)) for name in ('http','https','all')}
        report['user_agent_has_contact']='@' in settings.data_user_agent or 'https://' in settings.data_user_agent
        async def record_response(response):
            await response.aread()
            report['responses'].append(safe_response_details(response,response.text))
        with tempfile.TemporaryDirectory() as folder:
            # A fresh disposable cache proves current connectivity. No production
            # project, API key, proxy URL, response body or cookie is printed.
            configured=settings.model_copy(update={'data_cache_path':Path(folder)/'data.db'})
            async with httpx.AsyncClient(trust_env=configured.data_trust_env,follow_redirects=True,
                    event_hooks={'response':[record_response]}) as http:
                api=PublicAPIClient(configured,client=http)
                center=await PlacesService(configured,api).geocode(destination)
                if center is None:
                    report['error']='Geocoder returned no coordinates'
                else:
                    places=await WikidataPlaces(configured,api).nearby(center)
                    report.update(status='success' if places else 'empty',count=len(places),names=[place.name.value for place,_,_ in places])
    except DataServiceError as error:
        report.update(error=str(error),error_type=type(error).__name__)
    except Exception as error:
        report.update(error_type=type(error).__name__)
    print(json.dumps(report,ensure_ascii=True,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination',default='Nha Trang')
    args=parser.parse_args()
    asyncio.run(main(args.destination))
