"""One immutable visual style shared by every decorative illustration prompt."""

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING

from PIL import ImageColor

if TYPE_CHECKING:
    from ..config import Settings

DEFAULT_STYLE_LOCK = (
    "Editorial travel illustration, soft watercolor and clean shapes, warm ivory background, "
    "teal and terracotta accents, consistent palette and lighting, generous negative space."
)
ILLUSTRATION_RULES = (
    "Decorative illustration only. Do not generate text, lettering, logos or watermarks. "
    "Do not render factual maps or data charts; those are drawn separately from verified data. "
    "Do not present AI imagery as a real photograph of a destination."
)


@dataclass(frozen=True)
class StyleLock:
    description: str = DEFAULT_STYLE_LOCK
    palette: tuple[str, ...] = ("#F3EBDD", "#247C82", "#C76D4D")

    def __post_init__(self) -> None:
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("Style description must be a non-empty string")
        object.__setattr__(self, "description", self.description.strip())
        if not 2 <= len(self.palette) <= 8:
            raise ValueError("Style palette needs 2..8 opaque colors")
        normalized = []
        for color in self.palette:
            rgb = ImageColor.getrgb(color)
            if len(rgb) != 3:
                raise ValueError("Palette colors must be opaque")
            normalized.append("#%02X%02X%02X" % rgb)
        object.__setattr__(self, "palette", tuple(normalized))

    @classmethod
    def from_settings(cls, settings: "Settings") -> "StyleLock":
        palette = getattr(settings, "image_palette", "#F3EBDD,#247C82,#C76D4D")
        return cls(settings.image_style_lock.strip() or DEFAULT_STYLE_LOCK,
                   tuple(color.strip() for color in palette.split(",") if color.strip()))

    @property
    def signature(self) -> str:
        return sha256((self.description + "\n" + ILLUSTRATION_RULES + "\n" + ",".join(self.palette)).encode("utf-8")).hexdigest()

    def apply(self, prompt: str) -> str:
        """Append the same style and constraints; repeated application is idempotent."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Illustration prompt must not be blank")
        prompt = prompt.strip()
        suffix = f"\n\nSTYLE LOCK:\n{self.description}\nPALETTE (first color is background): {', '.join(self.palette)}\n\nILLUSTRATION RULES:\n{ILLUSTRATION_RULES}"
        return prompt if prompt.endswith(suffix) else prompt + suffix


def get_style_lock(settings: "Settings | None" = None) -> StyleLock:
    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    return StyleLock.from_settings(settings)


def build_prompt(prompt: str, style_lock: StyleLock | None = None) -> str:
    return (style_lock or get_style_lock()).apply(prompt)
