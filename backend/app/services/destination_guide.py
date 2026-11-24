"""Destination food, culture and practical travel context from attributed articles."""

import asyncio
from dataclasses import dataclass, field
import re
from urllib.parse import quote

from ..schemas import Fact
from .places import destination_key
from .official_prices import OfficialHTML
from .public_api import DataServiceError

TOPICS = {
    'food': ('am thuc', 'dac san', 'cuisine', 'eat', 'drink', 'food'),
    'culture': ('van hoa', 'le hoi', 'ton giao', 'culture', 'respect', 'religion', 'etiquette', 'di san'),
    'tips': ('di lai', 'giao thong', 'du lich', 'khi hau', 'get around', 'get in', 'stay safe', 'cope', 'climate', 'transport'),
}


@dataclass
class GuideResult:
    facts: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


class GuideHTML(OfficialHTML):
    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if re.fullmatch(r'h[1-6]', tag) and not self.hidden:
            self.parts.append('\n== ')

    def handle_endtag(self, tag):
        if re.fullmatch(r'h[1-6]', tag) and not self.hidden:
            self.parts.append(' ==\n')
        super().handle_endtag(tag)


def article_facts(text, url, retrieved, *, limit=2400):
    """Prefer named sections, keeping a small amount of their subheading context."""
    sections = list(re.finditer(r'^(={2,6})\s*(.*?)\s*\1\s*$', text, flags=re.M))
    gathered = {topic: [] for topic in TOPICS}
    active = {}
    headings = {}
    for index, section in enumerate(sections):
        level = len(section[1])
        heading = section[2].strip()
        body = text[section.end():sections[index+1].start() if index+1<len(sections) else len(text)].strip()
        active = {depth: topics for depth, topics in active.items() if depth < level}
        headings = {depth: value for depth, value in headings.items() if depth < level}
        headings[level] = heading
        direct = {topic for topic, keywords in TOPICS.items() if any(key in destination_key(heading) for key in keywords)}
        if direct:
            active[level] = direct
        if body:
            for topic in set().union(*active.values()) if active else set():
                gathered[topic].append(' / '.join(headings.values()) + ': ' + body)
    result = []
    for topic, paragraphs in gathered.items():
        if topic == 'tips':
            paragraphs.sort(key=lambda value: -sum(weight for key, weight in
                (('get around', 3), ('di lai', 3), ('giao thong', 3), ('transport', 2), ('du lich', 1))
                if key in destination_key(value.split(':', 1)[0])))
        # Avoid a long climate/history paragraph crowding out every other subtopic.
        selected = paragraphs[:3]
        per_section = max(400, limit//max(1, len(selected)))
        body = '\n'.join(value[:per_section] for value in selected)[:limit].strip()
        if body:
            result.append(Fact(value=body, source=f'{url}; retrieved={retrieved}; scope=destination-guide:{topic}', confidence=.7))
    return result


class DestinationGuideService:
    def __init__(self, settings, public_api):
        self.settings, self.api = settings, public_api

    async def article(self, host, title, *, on_event=None):
        endpoint = f'https://{host}/w/api.php'
        response = await self.api.request('destination-guide', endpoint,
            params={'action': 'query', 'format': 'json', 'prop': 'extracts|info|pageprops', 'inprop': 'url',
                    'explaintext': 1, 'redirects': 1, 'titles': title}, ttl=86400, on_event=on_event)
        body = response.data
        pages = body.get('query', {}).get('pages', {}) if isinstance(body, dict) else {}
        for page in pages.values():
            if not isinstance(page, dict) or 'missing' in page or 'disambiguation' in page.get('pageprops', {}):
                continue
            text = page.get('extract')
            # Wikivoyage deployments without TextExtracts expose the same article through parse.
            if not text and host.endswith('wikivoyage.org'):
                parsed = await self.api.request('destination-guide', endpoint,
                    params={'action': 'parse', 'format': 'json', 'pageid': page['pageid'], 'prop': 'text',
                            'disableeditsection': 1, 'disabletoc': 1}, ttl=86400, on_event=on_event)
                html = parsed.data.get('parse', {}).get('text', {}).get('*', '')
                parser = GuideHTML()
                parser.feed(html)
                text = parser.text
            if isinstance(text, str) and text.strip():
                canonical = page.get('fullurl') or f'https://{host}/wiki/' + quote(page['title'].replace(' ', '_'))
                if not canonical.startswith(f'https://{host}/'):
                    raise DataServiceError('destination-guide: unexpected article URL')
                return article_facts(text[:200000], canonical, response.retrieved_at)
        return []

    async def research(self, destination, *, on_event=None):
        result = GuideResult()
        if not self.settings.destination_guide_enabled:
            return result
        title = re.sub(r'^(thành phố|tỉnh|đảo|đặc khu)\s+', '', destination.strip(), flags=re.I)
        async def fetch(host, query):
            try:
                return await self.article(host, query, on_event=on_event)
            except (DataServiceError, ValueError, KeyError, TypeError, AttributeError):
                return []
        async def gather():
            for facts in await asyncio.gather(fetch('vi.wikipedia.org', title),
                    fetch('en.wikivoyage.org', destination_key(title).title())):
                result.facts.extend(facts)
        try:
            await asyncio.wait_for(gather(), timeout=self.settings.destination_guide_timeout_seconds)
        except asyncio.TimeoutError:
            result.warnings.append(Fact(value='Chưa lấy đủ thông tin ẩm thực/văn hóa trong thời gian cho phép.',
                source='execution:destination-guide-timeout', confidence=1))
        missing = [topic for topic in TOPICS if not any(f'scope=destination-guide:{topic}' in fact.source for fact in result.facts)]
        if missing:
            result.warnings.append(Fact(value='Nguồn tham khảo chưa có đủ nội dung: '+', '.join(missing),
                source='execution:destination-guide-missing-topics', confidence=1))
        return result
