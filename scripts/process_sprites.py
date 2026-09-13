#!/usr/bin/env python3
"""
Sprite processor for Shiro
==========================

Converts the raw character sprite sheets (``assets/sprites/IMG_*.jpg``) into the
PNG sprite strips the Rust overlay expects:

  - Named ``{state}.png`` (idle, talking, happy, sad, surprised, thinking,
    sleeping, waving, angry) — see ``overlay/src/sprite.rs``.
  - Exactly ``1024x256`` (height MUST equal the configured frame size of 256, or
    the loader rejects the sheet) → four 256x256 frames side by side.
  - RGBA with a TRANSPARENT background (the overlay alpha-composites the sprite).

The raw JPGs are ``1024x254`` RGB with a white background, so for each one we:
  1. pad the height 254 → 256 (centered, transparent rows) — padding keeps the
     width at 1024 = exactly four frames; resizing would distort the frame grid;
  2. remove the white background by an *edge* flood-fill (only white that is
     connected to the image border becomes transparent — the character's own
     light/white tones survive);
  3. optionally feather the alpha edge by 1px to avoid a hard halo.

The IMG → state mapping is defined in ``DEFAULT_MAP`` below and can be overridden
with ``assets/sprites/sprite_map.toml`` (``state = "IMG_....jpg"`` lines) or
``--map FILE``.

Usage:
    python scripts/process_sprites.py                # process all, write PNGs
    python scripts/process_sprites.py --dry-run      # report only, write nothing
    python scripts/process_sprites.py --tolerance 30 # looser white key
    python scripts/process_sprites.py --no-floodfill # global white key (legacy)
    python scripts/process_sprites.py --map my.toml  # custom mapping
"""

from __future__ import annotations

import argparse
import sys
from collections import deque
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

from PIL import Image

FRAME_SIZE = 256  # must match overlay config window.width / sprite.rs frame_size
SHEET_WIDTH = 1024  # 4 frames * 256

# Verified mapping (raw filename -> emotion state) from visual inspection of the
# four poses on each sheet.
DEFAULT_MAP: dict[str, str] = {
    "IMG_20260617_214704_256.jpg": "idle",
    "IMG_20260617_214706_557.jpg": "talking",
    "IMG_20260617_214708_411.jpg": "happy",
    "IMG_20260617_214709_854.jpg": "sad",
    "IMG_20260617_214712_095.jpg": "surprised",
    "IMG_20260617_214713_615.jpg": "thinking",
    "IMG_20260617_214716_181.jpg": "sleeping",
    "IMG_20260617_214717_809.jpg": "waving",
    "IMG_20260617_214718_740.jpg": "angry",
}

SPRITES_DIR = Path(__file__).resolve().parent.parent / "assets" / "sprites"


def load_mapping(map_file: Path | None) -> dict[str, str]:
    """Return {filename: state}. Optional TOML overrides as state = "file.jpg"."""
    mapping = dict(DEFAULT_MAP)
    candidate = map_file or (SPRITES_DIR / "sprite_map.toml")
    if candidate.exists():
        with open(candidate, "rb") as f:
            data = tomllib.load(f)
        # Accept either a top-level table or a [map] table; keys are states.
        table = data.get("map", data)
        overrides = {fname: state for state, fname in table.items()
                     if isinstance(fname, str)}
        mapping = overrides or mapping
        print(f"  (loaded mapping override from {candidate})")
    return mapping


BG_MIN_BRIGHTNESS = 80  # background tones are all light; the character is dark


def detect_bg_colors(px: list, w: int, h: int, k: int) -> list[tuple[int, int, int]]:
    """Return up to k dominant *light* colors found on the image border.

    The sprite backgrounds are light (white / gray wall + gray floor), while the
    character is dark, so we keep only the bright border clusters. This lets a
    multi-tone scene background (wall + floor) be removed without ever matching
    the dark character — even where boots touch the bottom border.
    """
    from collections import Counter

    border = []
    for x in range(w):
        border.append(px[x])
        border.append(px[(h - 1) * w + x])
    for y in range(h):
        border.append(px[y * w])
        border.append(px[y * w + (w - 1)])

    counts = Counter((r // 16 * 16, g // 16 * 16, b // 16 * 16) for r, g, b in border)
    refs: list[tuple[int, int, int]] = []
    for color, _ in counts.most_common():
        if min(color) >= BG_MIN_BRIGHTNESS:
            refs.append(color)
        if len(refs) >= k:
            break
    return refs or [(255, 255, 255)]


def remove_background(
    img: Image.Image, tolerance: int, floodfill: bool, feather: bool
) -> tuple[Image.Image, float]:
    """Return an RGBA copy with the (light) background keyed to alpha=0.

    The background color(s) are auto-detected from the border, so flat white,
    flat gray, and wall+floor scene backgrounds all work. With ``floodfill``
    (default), only background reachable from the image border is cleared, so
    the character's interior light tones survive. Returns (image, frac_clear).
    """
    rgb = img.convert("RGB")
    w, h = rgb.size
    px = list(rgb.getdata())  # flat list of (r, g, b)
    n = w * h

    refs = detect_bg_colors(px, w, h, k=6)

    def is_bg(i: int) -> bool:
        r, g, b = px[i]
        for R, G, B in refs:
            if abs(r - R) <= tolerance and abs(g - G) <= tolerance and abs(b - B) <= tolerance:
                return True
        return False

    alpha = bytearray(b"\xff" * n)

    if floodfill:
        visited = bytearray(n)
        stack: deque[int] = deque()
        # Seed from every border pixel.
        for x in range(w):
            stack.append(x)               # top row
            stack.append((h - 1) * w + x)  # bottom row
        for y in range(h):
            stack.append(y * w)            # left col
            stack.append(y * w + (w - 1))  # right col
        while stack:
            i = stack.pop()
            if visited[i]:
                continue
            visited[i] = 1
            if not is_bg(i):
                continue
            alpha[i] = 0
            x = i % w
            y = i // w
            if x > 0:
                stack.append(i - 1)
            if x < w - 1:
                stack.append(i + 1)
            if y > 0:
                stack.append(i - w)
            if y < h - 1:
                stack.append(i + w)
    else:
        for i in range(n):
            if is_bg(i):
                alpha[i] = 0

    if feather:
        # Soften any opaque pixel that borders a transparent one (1px edge).
        softened = bytearray(alpha)
        for i in range(n):
            if alpha[i] != 255:
                continue
            x = i % w
            y = i // w
            neighbor_clear = (
                (x > 0 and alpha[i - 1] == 0)
                or (x < w - 1 and alpha[i + 1] == 0)
                or (y > 0 and alpha[i - w] == 0)
                or (y < h - 1 and alpha[i + w] == 0)
            )
            if neighbor_clear:
                softened[i] = 128
        alpha = softened

    transparent = sum(1 for a in alpha if a == 0)
    out = Image.new("RGBA", (w, h))
    out.putdata([(px[i][0], px[i][1], px[i][2], alpha[i]) for i in range(n)])
    return out, transparent / n


def pad_to_frame(img: Image.Image) -> Image.Image:
    """Center the image on a transparent SHEET_WIDTH x FRAME_SIZE canvas."""
    w, h = img.size
    canvas = Image.new("RGBA", (SHEET_WIDTH, FRAME_SIZE), (0, 0, 0, 0))
    canvas.alpha_composite(img, ((SHEET_WIDTH - w) // 2, (FRAME_SIZE - h) // 2))
    return canvas


def process_one(
    src: Path, dst: Path, tolerance: int, floodfill: bool, feather: bool,
    dry_run: bool,
) -> bool:
    try:
        img = Image.open(src)
    except Exception as e:  # noqa: BLE001
        print(f"  !! {src.name}: cannot open ({e})")
        return False

    w, h = img.size
    if w != SHEET_WIDTH:
        print(f"  !! {src.name}: width {w} != {SHEET_WIDTH}; frame grid may be "
              f"wrong (expected 4x{FRAME_SIZE}). Proceeding anyway.")
    if h > FRAME_SIZE:
        print(f"  !! {src.name}: height {h} > {FRAME_SIZE}; will be cropped by "
              f"the canvas. Consider resizing first.")

    keyed, frac = remove_background(img, tolerance, floodfill, feather)
    out = pad_to_frame(keyed)

    print(f"  {src.name:32s} -> {dst.name:14s} "
          f"({w}x{h} -> {out.size[0]}x{out.size[1]}, "
          f"{frac * 100:4.1f}% transparent)")

    if out.size != (SHEET_WIDTH, FRAME_SIZE):
        print(f"  !! output size {out.size} != ({SHEET_WIDTH}, {FRAME_SIZE})")
        return False

    if not dry_run:
        out.save(dst)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Process Shiro sprite sheets.")
    ap.add_argument("--tolerance", type=int, default=30,
                    help="per-channel tolerance around detected bg color (default 30)")
    ap.add_argument("--no-floodfill", action="store_true",
                    help="key ALL bg-colored pixels, not just border-connected ones")
    ap.add_argument("--no-feather", action="store_true",
                    help="disable 1px alpha edge softening")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would happen, write nothing")
    ap.add_argument("--map", type=Path, default=None,
                    help="TOML file with state = \"IMG_....jpg\" overrides")
    args = ap.parse_args()

    mapping = load_mapping(args.map)
    print(f"Processing {len(mapping)} sprite sheets in {SPRITES_DIR}"
          + (" (dry run)" if args.dry_run else ""))

    ok = 0
    missing = []
    for fname, state in mapping.items():
        src = SPRITES_DIR / fname
        if not src.exists():
            missing.append(fname)
            print(f"  !! missing source: {fname} (for state '{state}')")
            continue
        dst = SPRITES_DIR / f"{state}.png"
        if process_one(src, dst, args.tolerance, not args.no_floodfill,
                       not args.no_feather, args.dry_run):
            ok += 1

    print(f"\nDone: {ok}/{len(mapping)} sheets processed"
          + (f", {len(missing)} missing source(s)" if missing else "")
          + (" (dry run — nothing written)" if args.dry_run else ""))
    return 0 if ok == len(mapping) else 1


if __name__ == "__main__":
    sys.exit(main())
