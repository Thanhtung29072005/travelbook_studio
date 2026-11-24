"""Public-data health check; no LLM, workflow server, or sample travel JSON."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.app.config import Settings
from backend.app.services.public_api import PublicAPIClient
from backend.app.services.places import PlacesService
from backend.app.services.wikidata_places import WikidataPlaces


async def main(args):
    settings=Settings();client=PublicAPIClient(settings)
    try:
        service=PlacesService(settings,client)
        if args.wikidata_only:
            center=await service.geocode(args.destination)
            if center is None: raise ValueError('Destination geocoder returned no coordinates')
            places=[place for place,_,_ in await WikidataPlaces(settings,client).nearby(center)]
            warnings=[]
        else:
            result=await service.search(args.destination)
            places=result.places;warnings=[warning.model_dump(mode='json') for warning in result.warnings]
        print(json.dumps({'destination':args.destination,'count':len(places),
            'places':[place.model_dump(mode='json') for place in places],'warnings':warnings},ensure_ascii=True,indent=2))
        if not places: raise SystemExit(1)
    finally:
        await client.aclose()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination',required=True)
    parser.add_argument('--wikidata-only',action='store_true',help='Bypass Overpass for a direct independent provider check')
    asyncio.run(main(parser.parse_args()))
