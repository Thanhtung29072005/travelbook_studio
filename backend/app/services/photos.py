"""Provider search and one traceable remote photo per place, without fabricated licenses."""

from hashlib import sha256
import re
import unicodedata
from urllib.parse import urlsplit

from pydantic import Field

from ..attribution import author_link, commons_license, https_url, plain_text, unsplash_link
from ..schemas import Fact, Photo, Place, Schema, Text
from .public_api import DataServiceError


def _fact(value, source, confidence=0.75):
    return {"value": value, "source": source, "confidence": confidence}


def _tokens(value):
    value = unicodedata.normalize("NFKD", plain_text(value).casefold().replace("đ", "d"))
    value = "".join(character for character in value if not unicodedata.combining(character))
    ignored = {"file", "jpg", "jpeg", "png", "webp", "the", "of", "at", "in", "a", "va", "bai", "bien", "beach", "chua", "bao", "tang", "cau"}
    return set(re.findall(r"[a-z0-9]+", value)) - ignored


def relevance(place_name, description):
    expected = _tokens(place_name)
    return len(expected & _tokens(description)) / len(expected) if expected else 0


class PhotoCandidate(Schema):
    photo: Photo
    provider_id: Text
    title: Fact[Text]
    width: Fact[int]
    height: Fact[int]
    relevance: Fact[float]
    download_location: str | None = None


class PhotoSearchResult(Schema):
    place_id: Text
    selected: Photo | None = None
    warnings: list[Fact[Text]] = Field(default_factory=list)
    candidate_count: int = 0


class PhotoService:
    def __init__(self, settings, http):
        self.settings, self.http = settings, http

    def _candidate(self, provider, provider_id, place, source, *, title, author, image_url,
                   source_url, license, license_url, width, height, author_url=None,
                   credit=None, attribution=None, restrictions=(), download_location=None):
        title, author = plain_text(title), plain_text(author)
        if not title or not author or author.casefold() in {"unknown", "unknown author", "unverified", "n/a"}:
            raise ValueError("Missing title/author")
        if type(width) is not int or type(height) is not int or min(width, height) <= 0:
            raise ValueError("Invalid dimensions")
        match = relevance(place.name.value, title)
        selection_conf = min(0.6, match, place.name.confidence)
        photo = Photo(id=provider + "-" + sha256(f"{provider_id}|{place.id}".encode()).hexdigest()[:24],
            place_id=place.id, provider=provider, url=_fact(image_url, source),
            author=_fact(author, source), license=_fact(license, source + "; license-policy=" + license_url),
            source_url=_fact(source_url, source), license_url=_fact(license_url, source + "; provider-license"),
            author_url=_fact(author_url, source) if author_url else None,
            caption=_fact(title, source, selection_conf),
            credit=_fact(plain_text(credit), source) if plain_text(credit) else None,
            attribution=_fact(plain_text(attribution), source) if plain_text(attribution) else None,
            restrictions=[Fact[Text](**_fact(plain_text(note), source)) for note in restrictions if plain_text(note)],
            selection_note=_fact("Khớp tên bằng tìm kiếm văn bản, chưa xác minh hình ảnh bằng mắt; cần Reviewer kiểm tra đúng địa điểm.",
                "calculation:place-name-token-overlap; " + place.name.source, selection_conf))
        return PhotoCandidate(photo=photo, provider_id=str(provider_id), title=_fact(title, source),
            width=_fact(width, source), height=_fact(height, source),
            relevance=_fact(match, "calculation:place-name-token-overlap", selection_conf), download_location=download_location)

    async def _wikimedia(self, place, query, on_event):
        response = await self.http.request("wikimedia", self.settings.wikimedia_api_url,
            params={"action": "query", "format": "json", "formatversion": 2, "generator": "search",
                "gsrsearch": query + " filetype:bitmap", "gsrnamespace": 6, "gsrlimit": self.settings.photo_search_limit,
                "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1280,
                "iiextmetadatalanguage": "en"}, ttl=86400, on_event=on_event)
        pages = response.data.get("query", {}).get("pages", [])
        if not isinstance(pages, list):
            raise ValueError("Malformed Commons pages")
        candidates = []
        for page in pages:
            try:
                info = page["imageinfo"][0]
                if info["mime"] not in {"image/jpeg", "image/png", "image/webp"}:
                    continue
                meta = info["extmetadata"]
                get = lambda key: meta.get(key, {}).get("value", "")
                license_url = str(get("LicenseUrl"))
                if license_url.startswith("http://creativecommons.org/"):
                    license_url = "https://" + license_url[7:]
                license_name, license_url = commons_license(get("LicenseShortName"), license_url)
                restrictions = [get("Restrictions"), get("UsageTerms")]
                if "BY-SA" in license_name.upper():
                    restrictions.append("Nếu chỉnh sửa ảnh, cần giữ điều kiện ShareAlike của giấy phép; ghi rõ thay đổi khi sử dụng.")
                source = response.source + "; retrieved=" + response.retrieved_at + "; file=" + str(page["pageid"])
                candidates.append(self._candidate("wikimedia", str(page["pageid"]), place, source,
                    title=page["title"] + " " + plain_text(get("ImageDescription")), author=get("Artist"),
                    image_url=https_url(info.get("thumburl") or info["url"], hosts={"upload.wikimedia.org", "thumb.wikimedia.org"}),
                    source_url=https_url(info["descriptionurl"], hosts={"commons.wikimedia.org"}),
                    license=license_name, license_url=license_url,
                    width=info.get("thumbwidth", info["width"]), height=info.get("thumbheight", info["height"]),
                    author_url=author_link(get("Artist")), credit=get("Credit"), attribution=get("Attribution"),
                    restrictions=restrictions))
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                continue
        return candidates

    async def _unsplash(self, place, query, on_event):
        key = self.settings.unsplash_access_key.get_secret_value().strip()
        if not key:
            raise DataServiceError("Unsplash key missing")
        response = await self.http.request("unsplash", self.settings.unsplash_base_url + "/search/photos",
            params={"query": query, "per_page": self.settings.photo_search_limit, "content_filter": "high"},
            headers={"Authorization": "Client-ID " + key, "Accept-Version": "v1"}, ttl=86400, on_event=on_event)
        candidates = []
        for item in response.data["results"]:
            try:
                if item.get("premium") or item.get("plus"):
                    continue
                source = response.source + "; retrieved=" + response.retrieved_at + "; photo=" + str(item["id"])
                download = https_url(item["links"]["download_location"], hosts={urlsplit(self.settings.unsplash_base_url).hostname})
                if urlsplit(download).path != "/photos/" + str(item["id"]) + "/download":
                    raise ValueError("Invalid download tracking URL")
                candidates.append(self._candidate("unsplash", str(item["id"]), place, source,
                    title=" ".join(str(item.get(name) or "") for name in ("description", "alt_description")),
                    author=item["user"]["name"], image_url=https_url(item["urls"]["regular"], hosts={"images.unsplash.com"}),
                    source_url=unsplash_link(item["links"]["html"], self.settings.photo_utm_source),
                    author_url=unsplash_link(item["user"]["links"]["html"], self.settings.photo_utm_source),
                    license="Unsplash License", license_url="https://unsplash.com/license",
                    width=item["width"], height=item["height"], download_location=download,
                    restrictions=["Giữ hotlink URL ảnh do API trả về, ghi công tác giả và Unsplash với liên kết; không tự tải lại để bỏ yêu cầu API."]))
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        return candidates

    async def _pexels(self, place, query, on_event):
        key = self.settings.pexels_api_key.get_secret_value().strip()
        if not key:
            raise DataServiceError("Pexels key missing")
        response = await self.http.request("pexels", self.settings.pexels_base_url + "/search",
            params={"query": query, "per_page": self.settings.photo_search_limit},
            headers={"Authorization": key}, ttl=86400, on_event=on_event)
        candidates = []
        for item in response.data["photos"]:
            try:
                source = response.source + "; retrieved=" + response.retrieved_at + "; photo=" + str(item["id"])
                candidates.append(self._candidate("pexels", str(item["id"]), place, source,
                    title=item["alt"], author=item["photographer"],
                    image_url=https_url(item["src"]["original"], hosts={"images.pexels.com"}),
                    source_url=https_url(item["url"], hosts={"www.pexels.com", "pexels.com"}),
                    author_url=https_url(item["photographer_url"], hosts={"www.pexels.com", "pexels.com"}),
                    license="Pexels License", license_url="https://www.pexels.com/license/",
                    width=item["width"], height=item["height"],
                    restrictions=["Ghi công Pexels và nhiếp ảnh gia theo hướng dẫn API; việc sử dụng chịu Pexels License."]))
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        return candidates

    async def select(self, place, destination="", *, used=None, on_event=None, provider_order=None, candidate_selector=None):
        place = Place.model_validate(place)
        used = used if used is not None else set()
        result = PhotoSearchResult(place_id=place.id)
        query = place.name.value + (" " + destination if destination else "")
        providers = provider_order or self.settings.photo_provider_order.split(",")
        if not providers or set(providers) - {"wikimedia", "unsplash", "pexels"}:
            raise ValueError("Invalid photo provider order")
        for provider in providers:
            try:
                candidates = await getattr(self, "_" + provider)(place, query, on_event)
            except DataServiceError as error:
                detail = f"HTTP {error.status_code}" if error.status_code else "thiếu khóa hoặc không truy cập được API"
                result.warnings.append(Fact[Text](**_fact(f"{provider}: {detail}; thử nguồn tiếp theo.", "photo-provider:" + provider, 1)))
                continue
            except (ValueError, TypeError, KeyError, AttributeError):
                result.warnings.append(Fact[Text](**_fact(f"{provider}: thiếu khóa, lỗi API hoặc phản hồi không hợp lệ; thử nguồn tiếp theo.", "photo-provider:" + provider, 1)))
                continue
            result.candidate_count += len(candidates)
            candidates.sort(key=lambda item: (-item.relevance.value, -min(item.width.value, item.height.value), item.provider_id))
            candidates = [candidate for candidate in candidates if (provider, candidate.provider_id) not in used
                and candidate.relevance.value >= self.settings.photo_min_relevance
                and candidate.width.value >= self.settings.photo_min_width and candidate.height.value >= self.settings.photo_min_height]
            if candidates and candidate_selector:
                selected_id = await candidate_selector(provider, candidates)
                if selected_id is None:
                    continue
                if selected_id not in {candidate.photo.id for candidate in candidates}:
                    raise ValueError("Selector referenced unknown photo")
                candidates.sort(key=lambda candidate: candidate.photo.id != selected_id)
            for candidate in candidates:
                key = (provider, candidate.provider_id)
                if (key in used or candidate.relevance.value < self.settings.photo_min_relevance
                    or candidate.width.value < self.settings.photo_min_width or candidate.height.value < self.settings.photo_min_height):
                    continue
                if provider == "unsplash":
                    try:
                        # An automated choice for the book is a download-like event. Never reuse cache.
                        await self.http.request("unsplash-download", candidate.download_location,
                            headers={"Authorization": "Client-ID " + self.settings.unsplash_access_key.get_secret_value(), "Accept-Version": "v1"},
                            ttl=0, on_event=on_event)
                    except DataServiceError:
                        result.warnings.append(Fact[Text](**_fact("Không ghi nhận được lựa chọn Unsplash; bỏ ảnh này.", "photo-provider:unsplash-download", 1)))
                        continue
                used.add(key)
                result.selected = candidate.photo
                return result
        result.warnings.append(Fact[Text](**_fact("Không có ảnh đủ metadata giấy phép, kích thước và mức khớp tên. Không tạo ảnh/giấy phép giả.", "validation:photo-selection", 1)))
        return result
