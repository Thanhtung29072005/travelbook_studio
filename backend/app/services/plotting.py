"""Headless PNG rendering shared by maps and charts (no pyplot/global GUI)."""

from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from threading import RLock
import json
import math
import textwrap

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from PIL import Image
from PIL.PngImagePlugin import PngInfo

RENDER_LOCK = RLock()
COLORS = {"ink": "#263F46", "teal": "#247C82", "orange": "#C76D4D", "paper": "#F7F4EE"}


@contextmanager
def plot_canvas(size=(1200, 800)):
    if not isinstance(size, (tuple, list)) or len(size) != 2 or any(
        type(n) is not int or n <= 0 for n in size
    ) or size[0] * size[1] > 40_000_000:
        raise ValueError("size must be positive integer (width, height), at most 40M pixels")
    with RENDER_LOCK:
        fig = Figure(figsize=(size[0] / 100, size[1] / 100), dpi=100, facecolor=COLORS["paper"])
        FigureCanvasAgg(fig)
        ax = fig.add_subplot(111)
        ax.set_facecolor("#FFFDF9")
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(colors=COLORS["ink"], labelsize=9)
        fig.subplots_adjust(left=0.14, right=0.96, top=0.84, bottom=0.20)
        try:
            yield fig, ax
        finally:
            fig.clear()


def footer(fig, text: str) -> None:
    fig.text(0.04, 0.035, "\n".join(textwrap.wrap(text, 130)), fontsize=8, color=COLORS["ink"])


def provenance(facts) -> tuple[list[str], float]:
    facts = list(facts)
    return list(dict.fromkeys(fact.source for fact in facts)), min(
        (fact.confidence for fact in facts), default=0.0,
    )


def save_png(fig, destination, size, *, sources, confidence, notes=(), data=None) -> Path:
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be in [0, 1]")
    path = Path(destination)
    if path.suffix.lower() != ".png":
        raise ValueError("destination must have a .png extension")
    buffer = BytesIO()
    fig.savefig(buffer, format="png", dpi=100)
    buffer.seek(0)
    info = PngInfo()
    info.add_text("Source", json.dumps(sources, ensure_ascii=False))
    info.add_text("Confidence", str(confidence))
    info.add_text("Notes", json.dumps(list(notes), ensure_ascii=False))
    info.add_text("Data", json.dumps(data or {}, ensure_ascii=False, allow_nan=False))
    path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(buffer) as rendered:
        rgb = rendered.convert("RGB")
        if rgb.size != tuple(size):
            rgb = rgb.resize(tuple(size), Image.Resampling.LANCZOS)
        rgb.save(path, format="PNG", pnginfo=info)
    return path
