"""Plain-text credit records; licenses come from provider metadata, never an LLM."""

from html.parser import HTMLParser
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from pydantic import Field

from .schemas import Fact, Photo, Schema, Text


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self.hidden = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"br", "p", "div"}:
            self.parts.append(" ")
        if tag == "a" and not self.hidden:
            self.links.extend(value for key, value in attrs if key == "href" and value)

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def plain_text(value):
    parser = _Text()
    parser.feed(str(value or ""))
    return " ".join("".join(parser.parts).split())


def https_url(value, *, hosts=None):
    value = str(value or "")
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise ValueError("Expected a public HTTPS provider URL")
    if hosts and parsed.hostname.lower() not in hosts:
        raise ValueError("Unexpected provider URL host")
    return value


def author_link(html):
    parser = _Text()
    parser.feed(str(html or ""))
    for link in parser.links:
        try:
            return https_url("https://commons.wikimedia.org" + link if link.startswith("/wiki/") else link)
        except ValueError:
            continue
    return None


def unsplash_link(value, app):
    value = https_url(value, hosts={"unsplash.com", "www.unsplash.com"})
    parsed = urlsplit(value)
    query = dict(parse_qsl(parsed.query))
    query.update(utm_source=app, utm_medium="referral")
    return urlunsplit(parsed._replace(query=urlencode(query)))


def commons_license(name, url):
    """Conservative subset; unsupported/multiple ambiguous license expressions stay out."""
    name = plain_text(name)
    url = https_url(url, hosts={"creativecommons.org", "www.creativecommons.org"})
    path = urlsplit(url).path.rstrip("/")
    match = re.fullmatch(r"/licenses/(by|by-sa)/(1\.0|2\.0|2\.5|3\.0|4\.0)(?:/deed\.[a-zA-Z_-]+)?", path)
    if match:
        expected = "CC BY-SA" if match[1] == "by-sa" else "CC BY"
        if name.upper().replace("_", " ") != f"{expected} {match[2]}":
            raise ValueError("License name and URL disagree")
    elif path == "/publicdomain/zero/1.0":
        if name.upper() not in {"CC0", "CC0 1.0", "CC ZERO"}:
            raise ValueError("CC0 name and URL disagree")
    elif path == "/publicdomain/mark/1.0":
        if name.lower() not in {"public domain", "public domain mark", "public domain mark 1.0", "pd"}:
            raise ValueError("Public-domain metadata disagree")
    else:
        raise ValueError("Unsupported Commons license")
    return name, url


class AttributionRecord(Schema):
    photo_id: Text
    place_id: Text
    credit_line: Fact[Text]
    links: list[Fact[Text]] = Field(default_factory=list)
    notes: list[Fact[Text]] = Field(default_factory=list)


def attribution_record(photo) -> AttributionRecord:
    photo = Photo.model_validate(photo)
    if photo.license_url is None:
        raise ValueError("Selected photo needs a license URL")
    values = [photo.author, photo.license, photo.source_url, photo.license_url]
    if min(fact.confidence for fact in values) <= 0:
        raise ValueError("Cannot attribute unverified placeholder metadata")
    provider = {"wikimedia": "Wikimedia Commons", "unsplash": "Unsplash", "pexels": "Pexels"}.get(photo.provider, "Nguồn ảnh")
    line = f"Ảnh: {photo.author.value} / {provider} — {photo.license.value}."
    if photo.credit is not None:
        line += " " + photo.credit.value
    if photo.attribution is not None:
        line += " " + photo.attribution.value
    confidence = min(fact.confidence for fact in values)
    source = "derived:photo-attribution; " + "; ".join(fact.source for fact in values)
    links = [photo.source_url, photo.license_url] + ([photo.author_url] if photo.author_url is not None else [])
    return AttributionRecord(photo_id=photo.id, place_id=photo.place_id,
        credit_line=Fact[Text](value=line, source=source, confidence=confidence),
        links=[Fact[Text](value=str(link.value), source=link.source, confidence=link.confidence) for link in links],
        notes=photo.restrictions)


def attribution_text(records):
    """Plain text can be used in PDF or a sidecar without interpreting provider HTML."""
    return "\n\n".join(record.credit_line.value + "\n" + "\n".join(link.value for link in record.links)
        + ("\n" + "\n".join(note.value for note in record.notes) if record.notes else "") for record in records) + "\n"
