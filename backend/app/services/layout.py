"""Reader-facing travel-book PDF with a separate provenance manifest."""

from __future__ import annotations

from datetime import timedelta

from hashlib import sha256
from io import BytesIO
import json
import os
import math
import re
from pathlib import Path
import tempfile
from threading import RLock
from xml.sax.saxutils import escape

from PIL import Image as PILImage, ImageChops, ImageOps
from pydantic import TypeAdapter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    Flowable, Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

from ..schemas import Artifact, Budget, BudgetSavingsPlan, ContentDocument, DayPlan, Fact, Photo, Place, TripRequest, WeatherReport
from .budget_savings import savings_plan

INK = colors.HexColor("#243D35")
TEAL = colors.HexColor("#2D7465")
ORANGE = colors.HexColor("#C76D4D")
PAPER = colors.HexColor("#F4F7F3")
FONT_LOCK = RLock()


class _NumberedCanvas(Canvas):
    def __init__(self, *args, footer_font, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_pages = []
        self._footer_font = footer_font

    def showPage(self):
        self._saved_pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_pages)
        for state in self._saved_pages:
            self.__dict__.update(state)
            self.setFillColor(INK)
            self.setStrokeColor(colors.HexColor("#DDE6E0"))
            self.line(42, 40, A4[0] - 42, 40)
            self.setFont(self._footer_font, 7)
            self.drawString(42, 25, "TRAVELBOOK STUDIO")
            self.drawRightString(A4[0] - 42, 25, f"Trang {self._pageNumber} / {total}")
            super().showPage()
        super().save()


class _BookDoc(SimpleDocTemplate):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sections = []

    def beforeDocument(self):
        self.sections = []
        self.boxes = []
        self.canv._layout_boxes = self.boxes

    def afterFlowable(self, flowable):
        if hasattr(flowable, "section_key"):
            self.sections.append({"id": flowable.section_key, "page": self.page,
                                  "title": flowable.getPlainText()})
            if flowable.section_key not in {"cover", "toc"}:
                self.notify("TOCEntry", (0, flowable.getPlainText(), self.page))


def _track(canvas, kind, x, y, width, height, label=""):
    if hasattr(canvas, "_layout_boxes"):
        x, y = canvas.absolutePosition(x, y)
        canvas._layout_boxes.append({"page": canvas.getPageNumber(), "kind": kind,
            "bbox": [round(x, 3), round(y, 3), round(x+width, 3), round(y+height, 3)], "label": label})


class _Paragraph(Paragraph):
    def drawOn(self, canvas, x, y, _sW=0):
        _track(canvas, "text", self._hAlignAdjust(x, _sW), y, self.width, self.height)
        return super().drawOn(canvas, x, y, _sW)


class _Image(Image):
    def drawOn(self, canvas, x, y, _sW=0):
        _track(canvas, "image", self._hAlignAdjust(x, _sW), y, self.drawWidth, self.drawHeight, getattr(self, "asset_path", str(self.filename)))
        return super().drawOn(canvas, x, y, _sW)


class _ColorPanel(Flowable):
    def __init__(self, width, height, color=TEAL):
        super().__init__()
        self.width, self.height, self.color = width, height, color

    def draw(self):
        self.canv.setFillColor(self.color)
        self.canv.roundRect(0, 0, self.width, self.height, 12, fill=1, stroke=0)
        self.canv.setFillColor(ORANGE)
        self.canv.circle(self.width * 0.82, self.height * 0.58, self.height * 0.22, fill=1, stroke=0)
        self.canv.setFillColor(PAPER)
        self.canv.roundRect(self.width * 0.08, self.height * 0.22,
                           self.width * 0.53, self.height * 0.10, 6, fill=1, stroke=0)


def _money(value):
    text = f"{value:,.0f}" if value == value.to_integral_value() else f"{value:,f}".rstrip("0").rstrip(".")
    return text.replace(",", " ")


class LayoutService:
    def __init__(self, *, font_regular: str | Path | None = None, font_bold: str | Path | None = None):
        pairs = [
            (Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/arial.ttf",
             Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/arialbd.ttf"),
            (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
             Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
        ]
        if font_regular is not None:
            regular, bold = Path(font_regular), Path(font_bold or font_regular)
        else:
            selected = next((pair for pair in pairs if all(path.is_file() for path in pair)), None)
            if selected is None:
                raise FileNotFoundError("Supply Unicode TrueType fonts through font_regular/font_bold")
            regular, bold = selected
        if not regular.is_file() or not bold.is_file():
            raise FileNotFoundError("Both Unicode font files must exist")
        suffix = sha256(f"{regular.resolve()}:{bold.resolve()}".encode()).hexdigest()[:12]
        self.font, self.bold = f"TravelBook-{suffix}", f"TravelBookBold-{suffix}"
        with FONT_LOCK:
            if self.font not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(self.font, str(regular)))
                pdfmetrics.registerFont(TTFont(self.bold, str(bold)))
                pdfmetrics.registerFontFamily(self.font, normal=self.font, bold=self.bold,
                                               italic=self.font, boldItalic=self.bold)
        self.styles = {
            "body": ParagraphStyle("Body", fontName=self.font, fontSize=9.5, leading=14.5,
                                   textColor=INK, spaceAfter=8, splitLongWords=True),
            "small": ParagraphStyle("Small", fontName=self.font, fontSize=8, leading=11,
                                    textColor=INK, spaceAfter=5, splitLongWords=True),
            "heading": ParagraphStyle("Heading", fontName=self.bold, fontSize=23, leading=30,
                                      textColor=INK, spaceAfter=17, keepWithNext=True),
            "subheading": ParagraphStyle("Subheading", fontName=self.bold, fontSize=12, leading=17,
                                         textColor=TEAL, spaceBefore=12, spaceAfter=8, keepWithNext=True),
            "cover": ParagraphStyle("Cover", fontName=self.bold, fontSize=32, leading=41,
                                    textColor=INK, spaceAfter=18),
            "eyebrow": ParagraphStyle("Eyebrow", fontName=self.bold, fontSize=8, leading=12,
                                      textColor=TEAL, spaceAfter=12),
            "table_header": ParagraphStyle("TableHeader", fontName=self.bold, fontSize=8, leading=12,
                                           textColor=colors.white),
            "place": ParagraphStyle("Place", fontName=self.bold, fontSize=10.5, leading=15,
                                    textColor=INK, spaceAfter=4),
            "time": ParagraphStyle("Time", fontName=self.bold, fontSize=10.5, leading=15, textColor=TEAL),
            "schedule_detail": ParagraphStyle("ScheduleDetail",fontName=self.font,fontSize=9,leading=13,
                                               textColor=INK,spaceAfter=4,splitLongWords=True),
            "source": ParagraphStyle("Source", fontName=self.font, fontSize=7, leading=10,
                                     textColor=colors.HexColor("#77847C"), spaceAfter=4, splitLongWords=True),
            "metric": ParagraphStyle("Metric", fontName=self.bold, fontSize=15, leading=21, textColor=INK),
        }
        self.width = A4[0] - 84

    def _p(self, text, style="body"):
        return _Paragraph(escape(str(text)).replace("\n", "<br/>"), self.styles[style])

    def _heading(self, text, key, style="heading"):
        paragraph = self._p(text, style)
        paragraph.section_key = key
        return paragraph

    def _photo_caption(self, photo, place_name):
        def link(label, url):
            if url is None:
                return escape(str(label))
            target = escape(str(url), {'"': "&quot;"})
            return f'<link href="{target}" color="#2D7465">{escape(str(label))}</link>'
        author_url = photo.author_url.value if photo.author_url else photo.source_url.value
        license_url = photo.license_url.value if photo.license_url else None
        credit = "Ảnh: " + link(photo.author.value, author_url) + " / " + link(photo.license.value, license_url)
        credit += " / " + link("Nguồn", photo.source_url.value) + " / Cắt khung"
        lines = [escape(place_name), credit]
        for fact in (photo.credit, photo.attribution):
            if fact is not None and fact.value:
                lines.append(link("Ghi công", fact.value) if fact.value.startswith(("https://", "http://")) else escape(fact.value))
        return _Paragraph("<br/>".join(lines), self.styles["source"])

    def _table(self, rows, widths):
        table = Table([[self._p(value, "table_header" if index == 0 else "small") for value in row] for index, row in enumerate(rows)],
                      colWidths=widths, repeatRows=1, hAlign="LEFT", splitInRow=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), TEAL),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PAPER]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, TEAL),
        ]))
        return table

    def _metrics(self, values):
        table = Table([[self._p(label, "source") for label, value in values],
                       [self._p(value, "metric") for label, value in values]],
                      colWidths=[self.width/len(values)]*len(values), hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), PAPER), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOX", (0, 0), (-1, -1), .5, colors.HexColor("#DDE7DF")),
            ("LINEAFTER", (0, 0), (-2, -1), 2, colors.white),
            ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, 0), 12), ("BOTTOMPADDING", (0, 1), (-1, 1), 13),
        ]))
        return table

    def _image(self, path, *, max_height=300, max_width=None, crop=False, trim=False):
        with PILImage.open(path) as source:
            source.load()
            processed = ImageOps.exif_transpose(source).convert("RGB")
        if trim:
            background = PILImage.new("RGB", processed.size, processed.getpixel((0, 0)))
            difference = ImageChops.difference(processed, background).point(lambda value: 255 if value > 28 else 0)
            box = difference.getbbox()
            if box:
                processed = processed.crop((max(0, box[0]-18), max(0, box[1]-18), min(processed.width, box[2]+18), min(processed.height, box[3]+18)))
        if crop:
            processed = ImageOps.fit(processed, (round((max_width or self.width-12)*2), round(max_height*2)))
        width, height = processed.size
        scale = min((max_width or self.width - 12) / width, max_height / height)
        buffer = BytesIO()
        processed.save(buffer, format="JPEG" if crop else "PNG", **({"quality": 92} if crop else {}))
        buffer.seek(0)
        result = _Image(buffer, width=width * scale, height=height * scale, hAlign="CENTER")
        result.asset_path = str(path)
        return result

    def _stop_card(self, stop, place):
        details = []
        if stop.travel_minutes is not None:
            details.append(f"Di chuyển {stop.travel_minutes.value} phút")
        if stop.distance_km is not None:
            details.append(f"{stop.distance_km.value:.1f} km".replace(".", ","))
        return self._schedule_card(stop.arrival.value,stop.departure.value,place.name.value,
            [self._p("  /  ".join(details),'schedule_detail')] if details else [])

    def _schedule_card(self,start,end,title,details):
        card = Table([[self._p(f"{start:%H:%M}\n{end:%H:%M}", "time"),
                       [self._p(title, "place"), *details]]],
                     colWidths=[82, self.width-82], hAlign="LEFT", splitInRow=1)
        card.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), PAPER), ("BACKGROUND", (1, 0), (1, 0), colors.white),
            ("BOX", (0, 0), (-1, -1), .5, colors.HexColor("#DDE7DF")),
            ("LINEBEFORE", (0, 0), (0, -1), 2, TEAL), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        return KeepTogether([card])

    def _activity_card(self, activity):
        details=[]
        if activity.venue:
            venue=activity.venue
            if venue.address:
                details.append(self._p(venue.address.value,'schedule_detail'))
            target=escape(str(venue.source_url.value),{'"':'&quot;'})
            details.append(_Paragraph(f'<link href="{target}" color="#2D7465">Xem vị trí và nguồn bản đồ</link>',self.styles['schedule_detail']))
            if activity.venue_transfer_minutes:
                details.append(self._p(f'Khung giờ gồm khoảng {activity.venue_transfer_minutes.value} phút đi bộ hai chiều và thời gian ăn; di chuyển là ước tính.','schedule_detail'))
        return self._schedule_card(activity.start.value,activity.end.value,activity.title.value,details)

    def _timeline_images(self,path,max_height=620):
        """Enlarge the original chart at full page width; split only between rows."""
        with PILImage.open(path) as original:
            image=original.convert('RGB')
            metadata=json.loads(original.info.get('Data','{}'))
        width,height=image.size
        limit=max(1,int(max_height*width/self.width))
        breaks=sorted({int(value) for value in metadata.get('row_breaks_px',[]) if isinstance(value,(float,int)) and 0<value<height})
        day_breaks=sorted({int(value) for value in metadata.get('day_breaks_px',[]) if isinstance(value,(float,int)) and 0<value<height})
        start=0
        images=[]
        while start<height:
            end=min(height,start+limit)
            if end<height:
                legal=[value for value in breaks if start+100<value<=end]
                by_day=[value for value in day_breaks if start+limit*.45<value<=end]
                if by_day:
                    end=max(by_day)
                elif legal:
                    end=max(legal)
            buffer=BytesIO()
            image.crop((0,start,width,end)).save(buffer,format='PNG')
            buffer.seek(0)
            part=_Image(buffer,width=self.width,height=(end-start)*self.width/width,hAlign='CENTER')
            part.asset_path=str(path)
            images.append(part)
            start=end
        return images

    @staticmethod
    def _background(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(colors.white)
        canvas.rect(0, 0, A4[0], A4[1], fill=1, stroke=0)
        if doc.page > 1:
            canvas.setFont(doc.book_font, 7)
            canvas.setFillColor(TEAL)
            canvas.drawString(42, A4[1]-27, "TRAVELBOOK  /  CẨM NANG DU LỊCH")
            canvas.setStrokeColor(colors.HexColor("#DDE6E0"))
            canvas.line(42, A4[1] - 36, A4[0] - 42, A4[1] - 36)
        canvas.restoreState()

    def build_from_json(self, folder: str | Path, output_path: str | Path | None = None, *, warnings=None) -> Path:
        folder = Path(folder).resolve()
        def read(name):
            return json.loads((folder / name).read_text(encoding="utf-8"))
        days = TypeAdapter(list[DayPlan]).validate_python(read("itinerary.json"))
        return self.build_pdf(
            output_path or folder / "travelbook.pdf", days=days, places=read("research.json"),
            budget=read("budget.json"), content=read("content.json"), photos=read("photos.json"),
            maps={day.day_number: folder / f"maps/day{day.day_number}.png" for day in days},
            charts={name: folder / f"charts/{name}.png" for name in ("budget", "weather", "timeline")},
            title="Đà Nẵng - Cẩm nang 3 ngày", image_root=folder, warnings=warnings,
        )

    def build_pdf(
        self, output_path: str | Path, *, days, places, budget, content, photos,
        maps: dict[int, str | Path], charts: dict[str, str | Path],
        title: str = "Cẩm nang du lịch", image_root: str | Path | None = None,
        warnings=None,
        with_toc=False, editorial=None, weather=None, day_images=None, cover_image=None,
        low_confidence_threshold=None,
        illustration_credits=None, trip_request=None, budget_savings=None,
    ) -> Path:
        """Build the book and a sibling .manifest.json; does not download images.

        Maps/charts must already exist. Internal review details stay in the
        manifest. Compact captions credit photos embedded in the book.
        """
        path = Path(output_path).resolve()
        if path.suffix.lower() != ".pdf":
            raise ValueError("output_path must end in .pdf")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must not be blank")
        days = sorted(TypeAdapter(list[DayPlan]).validate_python(days), key=lambda day: day.day_number)
        places = TypeAdapter(list[Place]).validate_python(places)
        budget = Budget.model_validate(budget)
        content = TypeAdapter(list[Fact[str]]).validate_python(content)
        photos = TypeAdapter(list[Photo]).validate_python(photos)
        warnings = TypeAdapter(list[Fact[str]]).validate_python(warnings or [])
        editorial = ContentDocument.model_validate(editorial) if editorial is not None else None
        weather = WeatherReport.model_validate(weather) if weather is not None else None
        trip = TripRequest.model_validate(trip_request) if trip_request is not None else None
        savings = BudgetSavingsPlan.model_validate(budget_savings) if budget_savings is not None else None
        if savings and (savings.current_total != budget.total.value or savings.limit != budget.limit.value or savings.currency != budget.currency.value):
            raise ValueError('Savings plan must match the current budget')
        if budget.total.value > budget.limit.value and savings is None:
            savings = savings_plan(budget,trip)
        if low_confidence_threshold is not None and (not math.isfinite(low_confidence_threshold) or not 0 <= low_confidence_threshold <= 1):
            raise ValueError("Confidence threshold must be between 0 and 1")
        threshold = low_confidence_threshold
        def text(fact):
            value = str(fact.value)
            if "; editorial-suggestion-unverified" in fact.source:
                value = value.removeprefix("Gợi ý chưa xác minh: ")
            return value
        day_images = day_images or {}
        illustration_credits = TypeAdapter(list[Artifact]).validate_python(illustration_credits or [])
        indexed = {place.id: place for place in places}
        if not days or len({day.day_number for day in days}) != len(days) or len(indexed) != len(places):
            raise ValueError("Itinerary must be non-empty; day numbers and place IDs must be unique")
        if len({photo.id for photo in photos}) != len(photos):
            raise ValueError("Photo IDs must be unique")
        if editorial and [item.day_number for item in editorial.days] != [day.day_number for day in days]:
            raise ValueError("Editorial days must match itinerary")
        if any(stop.place_id not in indexed for day in days for stop in day.stops) or any(
            photo.place_id not in indexed for photo in photos
        ):
            raise ValueError("Itinerary/photos must reference known places")
        required = [Path(maps[day.day_number]) for day in days if day.day_number in maps]
        if len(required) != len(days) or any(key not in charts for key in ("budget", "weather", "timeline")):
            raise ValueError("Supply one map per day and budget/weather/timeline charts")
        required += [Path(charts[key]) for key in ("budget", "weather", "timeline")]
        for asset in required:
            if not asset.is_file():
                raise FileNotFoundError(f"Required PNG is missing: {asset}")
            with PILImage.open(asset) as check:
                check.verify()

        # Preserve the full provenance in the manifest, independent of PDF text length.
        payload = {"days": [day.model_dump(mode="json") for day in days],
                   "places": [place.model_dump(mode="json") for place in places],
                   "budget": budget.model_dump(mode="json"),
                   "content": [fact.model_dump(mode="json") for fact in content],
                   "photos": [photo.model_dump(mode="json") for photo in photos],
                   "revision_warnings": [fact.model_dump(mode="json") for fact in warnings]}
        if trip:
            payload["trip_request"] = trip.model_dump(mode="json")
        if editorial:
            payload["editorial"] = editorial.model_dump(mode="json")
        if weather:
            payload["weather"] = weather.model_dump(mode="json")
        payload["illustrations"] = [{"value": item.name, "source": item.source, "confidence": item.confidence} for item in illustration_credits]
        facts = []
        def collect(value, location="input"):
            if isinstance(value, dict):
                if {"value", "source", "confidence"} <= value.keys():
                    facts.append({**value, "path": location})
                for key, item in value.items():
                    collect(item, location+"."+key)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    collect(item, location+f".{index}")
        collect(payload)
        sources = list(dict.fromkeys(fact["source"] for fact in facts))
        demo = any(source.startswith("fixture:") for source in sources)

        embedded_photos = []
        omitted_photos = []
        root = Path(image_root).resolve() if image_root is not None else None
        def local_photo(photo):
            eligible = min(photo.author.confidence, photo.license.confidence, photo.source_url.confidence, photo.url.confidence) > 0
            eligible = eligible and not photo.license.value.upper().startswith(("UNVERIFIED", "PLACEHOLDER")) and photo.provider != "unsplash"
            local = Path(photo.local_path) if photo.local_path else None
            if local is not None and not local.is_absolute():
                local = root / local if root is not None else None
            if root is not None and local is not None and not local.resolve().is_relative_to(root):
                eligible = False
            return local if eligible and local is not None and local.is_file() else None
        cover_photo = next((photo for day in days for stop in day.stops for photo in photos
                            if photo.place_id == stop.place_id and local_photo(photo)), None)
        story = [self._p("TRAVELBOOK STUDIO", "eyebrow"), self._heading(title, "cover", "cover")]
        if trip:
            summary = [trip.destination.value, f"{trip.days.value} ngày", f"{trip.travelers.value} người"]
            if trip.departure:
                summary.insert(0, trip.departure.value)
            if trip.start_date:
                summary.append(trip.start_date.value.strftime("%d/%m/%Y"))
            story += [self._p("  /  ".join(summary), "small"), Spacer(1, 12)]
        embedded_illustrations = []
        if cover_image:
            story += [self._image(cover_image, max_height=340, max_width=self.width, crop=True), Spacer(1, 7)]
            embedded_illustrations.append(str(cover_image))
        elif cover_photo:
            story += [self._image(local_photo(cover_photo), max_height=340, max_width=self.width, crop=True),
                      Spacer(1, 7), self._photo_caption(cover_photo, indexed[cover_photo.place_id].name.value)]
            embedded_photos.append(cover_photo.id)
        else:
            story += [_ColorPanel(self.width, 180), Spacer(1, 12)]
        story += [Spacer(1, 24), self._metrics([
            ("THỜI LƯỢNG", f"{len(days)} ngày"),
            (f"NGÂN SÁCH / {budget.currency.value}", _money(budget.limit.value)),
            (f"DỰ TOÁN / {budget.currency.value}", _money(budget.total.value)),
        ])]
        if budget.total.value > budget.limit.value:
            story += [Spacer(1, 10), self._p(
                f"Dự toán vượt ngân sách {_money(budget.total.value-budget.limit.value)} {budget.currency.value}. "
                'Các khoản chi hiện tại chưa đáp ứng mức ngân sách yêu cầu. Xem mục “Phương án tiết kiệm” để so sánh lựa chọn đi-về và lưu trú.', 'small')]
        elif savings:
            story += [Spacer(1, 10), self._p('Dự toán đang dùng các giả định tiết kiệm về đi-về/lưu trú, chưa phải giá đặt dịch vụ. Xem mục “Phương án tiết kiệm” để biết thay đổi và điều kiện cần xác nhận.', 'small')]
        story.append(PageBreak())
        if with_toc:
            toc = TableOfContents()
            toc.levelStyles = [ParagraphStyle("TOC", fontName=self.font, fontSize=9, leading=16, textColor=INK, spaceBefore=7)]
            story += [self._heading("Mục lục", "toc"), toc, PageBreak()]
        story.append(self._heading("Tổng quan hành trình", "overview"))
        if trip:
            rows = [["THÔNG TIN CHUYẾN ĐI", "CHI TIẾT"], ["Điểm đến", trip.destination.value]]
            rows.append(["Khởi hành từ", trip.departure.value if trip.departure else "Chưa cung cấp"])
            rows.append(["Ngày khởi hành", trip.start_date.value.strftime("%d/%m/%Y") if trip.start_date else "Chưa có ngày cụ thể"])
            end = trip.end_date.value if trip.end_date else trip.start_date.value + timedelta(days=trip.days.value-1) if trip.start_date else None
            rows.append(["Ngày kết thúc" if trip.end_date else "Ngày kết thúc dự kiến", end.strftime("%d/%m/%Y") if end else "Chưa xác định"])
            rows += [["Thời lượng", f"{trip.days.value} ngày"], ["Số người", str(trip.travelers.value)]]
            for name, label in (('transport_mode','Phương tiện'), ('outbound_departure_at','Rời nơi khởi hành'),
                                ('destination_arrival_at','Đến điểm du lịch'), ('destination_departure_at','Rời điểm du lịch'),
                                ('return_arrival_at','Về đến nơi khởi hành'), ('hotel_name','Khách sạn')):
                item = getattr(trip,name)
                if item:
                    from .day_schedule import local_stamp
                    value = local_stamp(item).strftime('%H:%M - %d/%m/%Y') if name.endswith('_at') else item.value
                    rows.append([label,value])
            suggested_hotel=next((item.venue for day in days for item in day.activities if item.kind=='hotel' and item.venue),None)
            if suggested_hotel and not trip.hotel_name:
                rows.append(['Nơi ở đề xuất',suggested_hotel.name.value+(' — '+suggested_hotel.address.value if suggested_hotel.address else '')])
                rows.append(['Tình trạng lưu trú','Chưa đặt phòng; giá và phòng trống cần xác nhận. Chi phí lưu trú trong dự toán vẫn là ước tính.'])
            if trip.transport_times_flexible and trip.transport_times_flexible.value:
                rows.append(['Giờ chuyến đi','Chưa chốt vé; lịch tạm cần điều chỉnh theo giờ đến/về thực tế'])
            rows += [["Ngân sách cả nhóm", f"{_money(budget.limit.value)} {budget.currency.value}"],
                     ["Ngân sách bình quân/người", f"{_money(round(budget.limit.value / trip.travelers.value, 0 if budget.currency.value == 'VND' else 2))} {budget.currency.value}"],
                     ["Dự toán cả nhóm", f"{_money(budget.total.value)} {budget.currency.value}"],
                     ["Dự toán bình quân/người", f"{_money(round(budget.total.value / trip.travelers.value, 0 if budget.currency.value == 'VND' else 2))} {budget.currency.value}"]]
            if trip.interests:
                rows.append(["Sở thích", ", ".join(item.value for item in trip.interests)])
            story += [self._table(rows, [self.width*.32, self.width*.68]), Spacer(1, 16)]
        story.extend(self._p(text(fact)) for fact in ([editorial.introduction] if editorial else content))
        if with_toc:
            story.append(self._p("Lịch trình rút gọn", "subheading"))
            overview_rows = [["NGÀY", "NGÀY ĐI", "CÁC ĐIỂM THAM QUAN"]]
            for day in days:
                overview_rows.append([f"Ngày {day.day_number}", day.date.value.strftime("%d/%m/%Y") if day.date else "Chưa xác định",
                    " → ".join(indexed[stop.place_id].name.value for stop in day.stops)])
            story.append(self._table(overview_rows, [self.width*.13, self.width*.21, self.width*.66]))
        else:
            story.append(self._p("Các ngày trong cẩm nang", "subheading"))
            story.extend(self._p(f"Ngày {day.day_number}: " + (day.title.value if day.title else "Lịch trình"), "small") for day in days)

        for day in days:
            story += [PageBreak(), self._heading(day.title.value if day.title else f"Ngày {day.day_number}", f"day_{day.day_number}"),
                      self._p(f"Ngày đi: {day.date.value:%d/%m/%Y}" if day.date else "Chưa chọn ngày đi cụ thể.", "small")]
            daily_photos = [photo for photo in photos if photo.place_id in {stop.place_id for stop in day.stops}]
            hero = next((photo for photo in daily_photos if local_photo(photo)), None) if with_toc else None
            featured_ids = set()
            if with_toc:
                day_illustration = day_images.get(day.day_number)
                if day_illustration:
                    cells = [self._image(day_illustration, max_height=168, max_width=self.width*.62-12)]
                    captions = [""]
                    widths = [self.width*.62]
                    if hero:
                        cells.append(self._image(local_photo(hero), max_height=168, max_width=self.width*.38-12, crop=True))
                        captions.append(self._photo_caption(hero, indexed[hero.place_id].name.value))
                        widths.append(self.width*.38)
                        featured_ids.add(hero.id)
                        embedded_photos.append(hero.id)
                    else:
                        cells = [self._image(day_illustration, max_height=220, max_width=self.width-12)]
                        widths = [self.width]
                    gallery = Table([cells, captions] if hero else [cells], colWidths=widths, hAlign="LEFT", splitInRow=1)
                    gallery.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
                    story.append(gallery)
                    embedded_illustrations.append(str(day_illustration))
                elif hero:
                    featured = [photo for photo in daily_photos if local_photo(photo)][:2]
                    column_width = self.width / len(featured)
                    gallery = Table([
                        [self._image(local_photo(photo), max_height=120, max_width=column_width-12, crop=True) for photo in featured],
                        [self._photo_caption(photo, indexed[photo.place_id].name.value) for photo in featured],
                    ], colWidths=[column_width] * len(featured), hAlign="LEFT", splitInRow=1)
                    gallery.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
                    story.append(gallery)
                    featured_ids = {photo.id for photo in featured}
                    embedded_photos.extend(photo.id for photo in featured)
            if editorial:
                story.append(self._p(text(editorial.days[day.day_number-1].introduction)))
            story.append(self._p("Lịch trình trong ngày", "subheading"))
            entries = [(stop.arrival.value, 0, stop) for stop in day.stops]
            entries += [(activity.start.value, 1, activity) for activity in day.activities]
            if any(item.venue for item in day.activities):
                story.append(self._p('Nhà hàng/nơi ở dưới đây là đề xuất từ dữ liệu bản đồ, chưa xác nhận giá, giờ hoạt động thực tế hoặc chỗ trống. Chi phí ăn/lưu trú vẫn là khoản dự trù trong ngân sách.','small'))
            elif day.activities:
                story.append(self._p('Giờ ăn, nghỉ, khách sạn và di chuyển là khung dự kiến; chưa xác nhận đặt chỗ.', 'small'))
            for _, kind, item in sorted(entries,key=lambda row:(row[0],row[1])):
                story += [self._activity_card(item) if kind else self._stop_card(item,indexed[item.place_id]), Spacer(1,6)]
            if not day.stops:
                continue
            with PILImage.open(maps[day.day_number]) as map_png:
                map_data = json.loads(map_png.info.get('Data', '{}'))
            if map_data.get('basemap') == 'openstreetmap':
                story += [PageBreak(), self._p(f"Bản đồ hành trình - Ngày {day.day_number}", 'heading'),
                          self._image(maps[day.day_number], max_height=390, max_width=self.width)]
                scope = ('Tuyến đường tham khảo từ OSRM.' if map_data.get('route_type') == 'road_geometry' else
                         'Nét đứt chỉ thứ tự điểm, chưa có dữ liệu đường đi thực tế.' if map_data.get('route_type') == 'straight_segments' else 'Vị trí điểm tham quan.')
                story.append(self._p(scope + ' Các số trên bản đồ tương ứng thứ tự trong lịch trình.', 'small'))
                story.append(_Paragraph('<link href="https://www.openstreetmap.org/copyright" color="#2D7465">© OpenStreetMap contributors</link>', self.styles['source']))
            else:
                map_column = [self._image(maps[day.day_number], max_height=140, max_width=self.width*.43, trim=True),
                              self._p("Sơ đồ thứ tự tham quan dự phòng, không phải bản đồ đường đi thực tế.", "source")]
                route_panel = Table([[map_column]], colWidths=[self.width*.45], hAlign="LEFT", splitInRow=1)
                route_panel.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (0, -1), 16)]))
                story += [Spacer(1, 10), route_panel]
            for photo in daily_photos:
                if photo.id not in featured_ids and local_photo(photo) is None:
                    omitted_photos.append(photo.id)

        if editorial:
            story += [PageBreak(), self._heading("Mẹo, món ăn và văn hóa", "travel_tips")]
            for label, values in (("Mẹo du lịch", editorial.tips), ("Món ăn", editorial.food), ("Văn hóa", editorial.culture), ("Lưu ý", editorial.warnings)):
                values = [fact for fact in values if fact.source != "policy:content-editor-review-required"]
                if values:
                    story.append(self._p(label, "subheading"))
                    story.extend(self._p(text(fact)) for fact in values)
            guide_sources = list(dict.fromkeys(url for fact in [*editorial.tips, *editorial.food, *editorial.culture]
                if 'scope=destination-guide:' in fact.source
                for url in re.findall(r'https://(?:vi\.wikipedia\.org|en\.wikivoyage\.org)/wiki/[^\s;]+', fact.source)))
            if guide_sources:
                links = [f'<link href="{escape(url, {chr(34): "&quot;"})}" color="#2D7465">'
                         + ('Wikivoyage' if 'wikivoyage.org' in url else 'Wikipedia') + '</link>' for url in guide_sources]
                story += [Spacer(1, 8), _Paragraph('Thông tin tham khảo: ' + ' / '.join(links), self.styles['source'])]

        extra_photos = [photo for photo in photos if photo.id not in embedded_photos and local_photo(photo)]
        for index in range(0, len(extra_photos), 6):
            group = extra_photos[index:index+6]
            story += [PageBreak(), self._heading("Góc nhìn hành trình", f"photo_gallery_{index//6+1}")]
            for offset in range(0, len(group), 2):
                pair = group[offset:offset+2]
                gallery = Table([
                    [self._image(local_photo(photo), max_height=150, max_width=self.width/2-12, crop=True) for photo in pair],
                    [self._photo_caption(photo, indexed[photo.place_id].name.value) for photo in pair]
                ], colWidths=[self.width/2]*len(pair), hAlign="LEFT")
                gallery.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
                story += [gallery, Spacer(1, 18)]
                embedded_photos.extend(photo.id for photo in pair)

        chart_titles = {"budget": "Ngân sách dự kiến", "weather": "Thời tiết", "timeline": "Timeline toàn chuyến"}
        numeric_weather = weather is None or any(any((point.temperature_min, point.temperature_max, point.rain_probability)) for point in weather.daily)
        for key in ("budget", "weather", "timeline"):
            if key == "timeline":
                story += [PageBreak(),self._heading(chart_titles[key],key)]
                for index,image in enumerate(self._timeline_images(charts[key])):
                    if index:
                        story += [PageBreak(),self._p('Timeline toàn chuyến (tiếp)','subheading')]
                    story.append(image)
                continue
            story.append(PageBreak())
            story.append(self._heading(chart_titles[key], key))
            if key == "budget":
                balance = budget.limit.value-budget.total.value
                story += [self._metrics([
                    (f"DỰ TOÁN NHÓM / {budget.currency.value}", _money(budget.total.value)),
                    (f"NGÂN SÁCH NHÓM / {budget.currency.value}", _money(budget.limit.value)),
                    ("CHÊNH LỆCH DỰ KIẾN" if balance >= 0 else "VƯỢT NGÂN SÁCH", _money(abs(balance))),
                ]), Spacer(1, 18)]
                if trip:
                    story.append(self._p(f"Tổng cho {trip.travelers.value} người trong {trip.days.value} ngày. Bình quân {_money(round(budget.total.value / trip.travelers.value, 0 if budget.currency.value == 'VND' else 2))} {budget.currency.value}/người cho cả chuyến."))
                story.append(self._p("Các dòng dưới là tổng tiền cho cả nhóm; đơn giá và số lượng nằm trong diễn giải. Khoản tạm dự trù vé chưa rõ giá không phải giá vé thực tế. Tiền đi-về tạm tính cần được tra lại theo phương tiện và ngày khởi hành.", "small"))
                if trip and not trip.departure:
                    story.append(self._p("Chưa có nơi khởi hành: dự toán chưa gồm tiền đi-về điểm đến.", "small"))
                story.append(self._p("Giá ước tính chưa phải báo giá. Chưa gồm mua sắm và các chi tiêu cá nhân ngoài lịch trình.", "small"))
            if key != "weather" or numeric_weather:
                story += [self._image(charts[key], max_height=250 if key == "budget" else 300, trim=True), Spacer(1, 14)]
            if key == "budget":
                totals = {}
                for item in budget.items:
                    totals[item.category] = totals.get(item.category, 0) + item.amount.value
                labels = {"food": "Ăn uống", "accommodation": "Lưu trú", "transport": "Di chuyển",
                          "tickets": "Vé tham quan", "contingency": "Dự phòng", "other": "Khác"}
                rows = [["Khoản chi", budget.currency.value]] + [[labels[key], _money(value)] for key, value in totals.items()]
                if not with_toc:
                    story.append(self._table(rows, [self.width * .65, self.width * .35]))
                if with_toc:
                    story.append(self._p(f"Giá có nguồn: {_money(budget.sourced_total.value)}; giá ước tính: {_money(budget.estimated_total.value)}; chênh lệch so với ngân sách: {_money(budget.variance.value)} {budget.currency.value}."))
                    generated_labels = {"Food allowance (estimate)": "Ăn uống", "Local transport allowance (estimate)": "Di chuyển địa phương", "Accommodation allowance (estimate)": "Lưu trú"}
                    rows = [["Khoản chi", "Loại giá", budget.currency.value]]
                    for item in budget.items:
                        description = text(item.description)
                        if item.description.source.startswith("estimate:configuration:") and description in generated_labels:
                            description = generated_labels[description]
                            if item.day_number is not None:
                                description += f" - Ngày {item.day_number}"
                        rows.append([description, "Có nguồn" if item.price_kind == "sourced" else "Ước tính", _money(item.amount.value)])
                    story.append(self._table(rows, [self.width*.55, self.width*.20, self.width*.25]))
                evidenced = {str(item.price_evidence.source_url) + '|' + item.price_evidence.excerpt: item
                             for item in budget.items if item.price_evidence and item.price_kind == 'sourced'}
                if evidenced:
                    story.append(self._p("Nguồn giá đã tra", "subheading"))
                    for item in evidenced.values():
                        quote = item.price_evidence
                        target = escape(str(quote.source_url), {'"': '&quot;'})
                        caption = (f'<link href="{target}" color="#2D7465">{escape(item.description.value)}</link>'
                                   f'<br/>Tra ngày {quote.retrieved_at:%d/%m/%Y} / {escape(quote.conditions)}'
                                   f'<br/>{escape(quote.excerpt)}')
                        story.append(_Paragraph(caption, self.styles['source']))
                if savings:
                    story += [PageBreak(), self._heading('Phương án tiết kiệm','savings_plan')]
                    story.append(self._p(f'Ngân sách cả nhóm: {_money(savings.limit)} {savings.currency}. Dự toán ban đầu: {_money(savings.initial_total)} {savings.currency}; dự toán hiện tại: {_money(savings.current_total)} {savings.currency}.'))
                    if savings.current_total>savings.limit:
                        story.append(self._p(f'Dự toán hiện tại vẫn vượt {_money(savings.current_total-savings.limit)} {savings.currency} sau các vòng sửa. Các lựa chọn dưới đây cần được xác nhận trước khi áp dụng.'))
                    else:
                        story.append(self._p('Các mức tạm tính tiết kiệm dưới đây đã được dùng trong dự toán hiện tại. Cần xác nhận giá và khả năng thực hiện trước khi đặt dịch vụ.'))
                    if savings.options:
                        rows=[['PHƯƠNG ÁN','BAN ĐẦU','MỨC MỤC TIÊU','GIẢM DỰ KIẾN']]
                        rows.extend([option.title,_money(option.original_amount),_money(option.target_amount),_money(option.saving)] for option in savings.options)
                        story.append(self._table(rows,[self.width*.37,self.width*.21,self.width*.21,self.width*.21]))
                        for option in savings.options:
                            story += [self._p(option.title,'subheading'),self._p(option.assumption)]
                    else:
                        story += [self._p('Đi-về: so sánh phương tiện khác hoặc khung giờ/vé giá thấp nếu phù hợp tuyến và thời lượng chuyến đi.'),
                                  self._p('Lưu trú: xin báo giá mức phòng thấp hơn, giữ đủ chỗ cho số người và điều kiện đã yêu cầu.')]
                    story += [Spacer(1,12),self._metrics([
                        ('KỊCH BẢN TẠM TÍNH',_money(savings.proposed_total)),
                        ('GIẢM SO VỚI BAN ĐẦU',_money(max(0,savings.initial_total-savings.proposed_total))),
                        ('CÒN VƯỢT' if savings.proposed_total>savings.limit else 'DƯ SO VỚI NGÂN SÁCH',_money(abs(savings.proposed_total-savings.limit))),
                    ])]
                    story.extend(self._p(note,'small') for note in savings.notes)
            elif key == "weather":
                story.append(self._p("Cập nhật dự báo thời tiết gần ngày khởi hành."))
                if weather:
                    if weather.status == "outside_forecast_window":
                        story.append(self._p("Ngày khởi hành nằm ngoài phạm vi dự báo 16 ngày. Dự báo sẽ có khi gần đến ngày đi."))
                    elif weather.status == "not_requested":
                        story.append(self._p("Cần ngày khởi hành cụ thể để lấy dự báo cho chuyến đi."))
                    elif weather.status == "unavailable":
                        story.append(self._p("Chưa lấy được dự báo từ nguồn thời tiết. Vui lòng cập nhật lại trước chuyến đi."))
                    for forecast in weather.daily:
                        parts = [f"Ngày {forecast.day_number}"]
                        for name, label in (("temperature_min", "Thấp nhất (°C)"), ("temperature_max", "Cao nhất (°C)"), ("rain_probability", "Xác suất mưa (%)")):
                            value = getattr(forecast, name, None)
                            if value is not None:
                                parts.append(label + ": " + text(value))
                        story.append(self._p("; ".join(parts)))
            else:
                story.append(self._p("Các thanh màu biểu diễn thời gian tham quan và di chuyển dự kiến. "
                                     "Khoảng trống là thời gian chưa gán hoạt động; khung giờ chi tiết nằm ở từng ngày."))

        low = [fact for fact in facts if threshold is not None and fact["confidence"] < threshold]

        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix="travelbook-", suffix=".pdf", dir=path.parent)
        os.close(fd)
        temporary = Path(temporary_name)
        doc = _BookDoc(str(temporary), pagesize=A4, leftMargin=42, rightMargin=42,
                       topMargin=52, bottomMargin=54, title=title, author="TravelBook Studio",
                       subject="Travel guide from validated JSON; source metadata retained in manifest.")
        doc.book_font = self.font
        try:
            builder = doc.multiBuild if with_toc else doc.build
            builder(story, onFirstPage=self._background, onLaterPages=self._background,
                      canvasmaker=lambda *args, **kwargs: _NumberedCanvas(*args, footer_font=self.font, **kwargs))
            overlaps = []
            for image in [box for box in doc.boxes if box["kind"] == "image"]:
                a = image["bbox"]
                for paragraph in [box for box in doc.boxes if box["kind"] == "text" and box["page"] == image["page"]]:
                    b = paragraph["bbox"]
                    if min(a[2], b[2])-max(a[0], b[0]) > .5 and min(a[3], b[3])-max(a[1], b[1]) > .5:
                        overlaps.append({"image": image, "text": paragraph})
            if overlaps:
                raise ValueError("PDF layout contains text/image overlap")
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()
        manifest = {"pdf": path.name, "page_count": doc.page, "sections": doc.sections,
                    'budget_savings':savings.model_dump(mode='json') if savings else None,
                    "demo_data": demo, "sources": sources,
                    "revision_warnings": [fact.model_dump(mode="json") for fact in warnings],
                    "confidence": min((fact["confidence"] for fact in facts), default=0),
                    "embedded_photo_ids": list(dict.fromkeys(embedded_photos)),
                    "omitted_photo_ids": list(dict.fromkeys(omitted_photos)),
                    "photo_credits": [photo.model_dump(mode="json") for photo in photos if photo.id in embedded_photos],
                    "price_evidence": [item.price_evidence.model_dump(mode="json") for item in budget.items if item.price_evidence and item.price_kind == 'sourced'],
                    "embedded_illustration_paths": list(dict.fromkeys(embedded_illustrations))}
        manifest.update({"toc": [entry for entry in doc.sections if entry["id"] not in {"cover", "toc"}] if with_toc else [],
            "low_confidence_threshold": threshold, "low_confidence_facts": low, "layout_boxes": doc.boxes, "text_image_overlap_count": len(overlaps),
            "source_index": {f"S{index:03d}": source for index, source in enumerate(sources, 1)}})
        path.with_suffix(".manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        return path
