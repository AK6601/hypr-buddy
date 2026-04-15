#!/usr/bin/env python3
"""
Placeholder Sprite Generator
==============================

Generates kawaii-style placeholder sprite sheets for each emotional state.
Each state is a 4-frame horizontal strip PNG (1024x256) with slight
animation variations.

Uses cairosvg to render SVGs and Pillow to compose the final sprite sheets.

States: idle, talking, happy, sad, surprised, thinking, sleeping, waving, angry
"""

from __future__ import annotations

import io
import math
import sys
from pathlib import Path

try:
    import cairosvg
    from PIL import Image
except ImportError:
    print("ERROR: Install dependencies first: pip install cairosvg pillow")
    sys.exit(1)

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "assets" / "sprites"
FRAME_SIZE = 256
FRAMES_PER_STATE = 4

# Color palette
BODY_COLOR = "#FFB7C5"       # Soft pink
FACE_COLOR = "#FFFFFF"        # White
CHEEK_COLOR = "#FF8FA3"      # Pink cheeks
EYE_COLOR = "#2D2D2D"        # Dark eyes
MOUTH_COLOR = "#2D2D2D"      # Dark mouth
HIGHLIGHT_COLOR = "#FFE5EC"  # Light pink highlight
SPARKLE_COLOR = "#FFD700"    # Gold sparkle


def svg_header(size: int = 256) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}">'


def svg_footer() -> str:
    return "</svg>"


def base_face(body_scale: float = 1.0, y_offset: float = 0.0) -> str:
    """Render the base circular body with face area."""
    cy = 140 + y_offset
    r = 80 * body_scale
    return f"""
    <!-- Body -->
    <circle cx="128" cy="{cy}" r="{r}" fill="{BODY_COLOR}" stroke="#E0909E" stroke-width="2"/>
    <!-- Face highlight -->
    <ellipse cx="128" cy="{cy - 10}" rx="{r * 0.7}" ry="{r * 0.6}" fill="{FACE_COLOR}" opacity="0.6"/>
    <!-- Cheeks -->
    <ellipse cx="{128 - r * 0.5}" cy="{cy + 8}" rx="12" ry="8" fill="{CHEEK_COLOR}" opacity="0.5"/>
    <ellipse cx="{128 + r * 0.5}" cy="{cy + 8}" rx="12" ry="8" fill="{CHEEK_COLOR}" opacity="0.5"/>
    """


def eyes_normal(y_offset: float = 0.0, blink: bool = False) -> str:
    cy = 132 + y_offset
    if blink:
        return f"""
        <line x1="105" y1="{cy}" x2="121" y2="{cy}" stroke="{EYE_COLOR}" stroke-width="3" stroke-linecap="round"/>
        <line x1="135" y1="{cy}" x2="151" y2="{cy}" stroke="{EYE_COLOR}" stroke-width="3" stroke-linecap="round"/>
        """
    return f"""
    <ellipse cx="113" cy="{cy}" rx="8" ry="10" fill="{EYE_COLOR}"/>
    <ellipse cx="143" cy="{cy}" rx="8" ry="10" fill="{EYE_COLOR}"/>
    <circle cx="116" cy="{cy - 3}" r="3" fill="white"/>
    <circle cx="146" cy="{cy - 3}" r="3" fill="white"/>
    """


def eyes_happy(y_offset: float = 0.0) -> str:
    cy = 132 + y_offset
    return f"""
    <path d="M105 {cy} Q113 {cy - 12} 121 {cy}" stroke="{EYE_COLOR}" stroke-width="3" fill="none" stroke-linecap="round"/>
    <path d="M135 {cy} Q143 {cy - 12} 151 {cy}" stroke="{EYE_COLOR}" stroke-width="3" fill="none" stroke-linecap="round"/>
    """


def eyes_sad(y_offset: float = 0.0) -> str:
    cy = 132 + y_offset
    return f"""
    <ellipse cx="113" cy="{cy}" rx="7" ry="9" fill="{EYE_COLOR}"/>
    <ellipse cx="143" cy="{cy}" rx="7" ry="9" fill="{EYE_COLOR}"/>
    <circle cx="115" cy="{cy - 2}" r="3" fill="white"/>
    <circle cx="145" cy="{cy - 2}" r="3" fill="white"/>
    <!-- Eyebrows tilted sad -->
    <line x1="100" y1="{cy - 18}" x2="120" y2="{cy - 14}" stroke="{EYE_COLOR}" stroke-width="2" stroke-linecap="round"/>
    <line x1="156" y1="{cy - 18}" x2="136" y2="{cy - 14}" stroke="{EYE_COLOR}" stroke-width="2" stroke-linecap="round"/>
    """


def eyes_surprised(y_offset: float = 0.0) -> str:
    cy = 132 + y_offset
    return f"""
    <circle cx="113" cy="{cy}" r="12" fill="{EYE_COLOR}"/>
    <circle cx="143" cy="{cy}" r="12" fill="{EYE_COLOR}"/>
    <circle cx="115" cy="{cy - 3}" r="4" fill="white"/>
    <circle cx="145" cy="{cy - 3}" r="4" fill="white"/>
    """


def eyes_thinking(y_offset: float = 0.0, spiral: bool = False) -> str:
    cy = 132 + y_offset
    if spiral:
        return f"""
        <circle cx="113" cy="{cy}" r="9" fill="none" stroke="{EYE_COLOR}" stroke-width="2"/>
        <path d="M113 {cy - 9} A4 4 0 0 1 113 {cy - 1} A2 2 0 0 0 113 {cy + 3}" fill="none" stroke="{EYE_COLOR}" stroke-width="1.5"/>
        <ellipse cx="143" cy="{cy}" rx="8" ry="5" fill="{EYE_COLOR}"/>
        """
    return f"""
    <ellipse cx="113" cy="{cy}" rx="7" ry="9" fill="{EYE_COLOR}"/>
    <circle cx="115" cy="{cy - 2}" r="3" fill="white"/>
    <ellipse cx="143" cy="{cy - 2}" rx="8" ry="5" fill="{EYE_COLOR}"/>
    """


def eyes_sleeping(y_offset: float = 0.0) -> str:
    cy = 132 + y_offset
    return f"""
    <line x1="105" y1="{cy}" x2="121" y2="{cy}" stroke="{EYE_COLOR}" stroke-width="2.5" stroke-linecap="round"/>
    <line x1="135" y1="{cy}" x2="151" y2="{cy}" stroke="{EYE_COLOR}" stroke-width="2.5" stroke-linecap="round"/>
    """


def eyes_angry(y_offset: float = 0.0) -> str:
    cy = 132 + y_offset
    return f"""
    <ellipse cx="113" cy="{cy}" rx="8" ry="9" fill="{EYE_COLOR}"/>
    <ellipse cx="143" cy="{cy}" rx="8" ry="9" fill="{EYE_COLOR}"/>
    <circle cx="115" cy="{cy - 2}" r="2.5" fill="white"/>
    <circle cx="145" cy="{cy - 2}" r="2.5" fill="white"/>
    <!-- Angry eyebrows -->
    <line x1="98" y1="{cy - 14}" x2="122" y2="{cy - 20}" stroke="{EYE_COLOR}" stroke-width="3" stroke-linecap="round"/>
    <line x1="158" y1="{cy - 14}" x2="134" y2="{cy - 20}" stroke="{EYE_COLOR}" stroke-width="3" stroke-linecap="round"/>
    """


def mouth_smile(y_offset: float = 0.0, width: float = 1.0) -> str:
    cy = 155 + y_offset
    w = 15 * width
    return f'<path d="M{128 - w} {cy} Q128 {cy + 14 * width} {128 + w} {cy}" stroke="{MOUTH_COLOR}" stroke-width="2.5" fill="none" stroke-linecap="round"/>'


def mouth_open(y_offset: float = 0.0, size: float = 1.0) -> str:
    cy = 155 + y_offset
    rx = 10 * size
    ry = 7 * size
    return f'<ellipse cx="128" cy="{cy}" rx="{rx}" ry="{ry}" fill="{MOUTH_COLOR}"/>'


def mouth_frown(y_offset: float = 0.0) -> str:
    cy = 158 + y_offset
    return f'<path d="M113 {cy + 5} Q128 {cy - 5} 143 {cy + 5}" stroke="{MOUTH_COLOR}" stroke-width="2.5" fill="none" stroke-linecap="round"/>'


def mouth_flat(y_offset: float = 0.0) -> str:
    cy = 157 + y_offset
    return f'<line x1="118" y1="{cy}" x2="138" y2="{cy}" stroke="{MOUTH_COLOR}" stroke-width="2.5" stroke-linecap="round"/>'


def mouth_o(y_offset: float = 0.0) -> str:
    cy = 157 + y_offset
    return f'<circle cx="128" cy="{cy}" r="8" fill="{MOUTH_COLOR}"/>'


def sparkle(x: float, y: float, size: float = 6) -> str:
    s = size
    return f"""
    <line x1="{x}" y1="{y - s}" x2="{x}" y2="{y + s}" stroke="{SPARKLE_COLOR}" stroke-width="2" stroke-linecap="round"/>
    <line x1="{x - s}" y1="{y}" x2="{x + s}" y2="{y}" stroke="{SPARKLE_COLOR}" stroke-width="2" stroke-linecap="round"/>
    <line x1="{x - s*0.6}" y1="{y - s*0.6}" x2="{x + s*0.6}" y2="{y + s*0.6}" stroke="{SPARKLE_COLOR}" stroke-width="1.5" stroke-linecap="round"/>
    <line x1="{x + s*0.6}" y1="{y - s*0.6}" x2="{x - s*0.6}" y2="{y + s*0.6}" stroke="{SPARKLE_COLOR}" stroke-width="1.5" stroke-linecap="round"/>
    """


def tear(x: float, y: float, frame: int) -> str:
    drop_y = y + frame * 4
    return f"""
    <ellipse cx="{x}" cy="{drop_y}" rx="3" ry="5" fill="#88CCFF" opacity="0.7"/>
    """


def zzz(x: float, y: float, frame: int) -> str:
    offset = frame * 8
    elements = ""
    for i in range(min(frame + 1, 3)):
        sz = 8 + i * 3
        ox = x + i * 12
        oy = y - offset - i * 15
        elements += f'<text x="{ox}" y="{oy}" font-size="{sz}" fill="#8888CC" font-family="sans-serif" font-weight="bold" opacity="{0.8 - i * 0.2}">Z</text>'
    return elements


def wave_hand(frame: int) -> str:
    """A simple waving hand/arm."""
    angle = math.sin(frame * 1.2) * 20
    return f"""
    <g transform="rotate({angle}, 185, 130)">
        <line x1="175" y1="140" x2="200" y2="105" stroke="{BODY_COLOR}" stroke-width="8" stroke-linecap="round"/>
        <circle cx="200" cy="100" r="10" fill="{BODY_COLOR}" stroke="#E0909E" stroke-width="1.5"/>
    </g>
    """


# ---------------------------------------------------------------------------
# State frame generators
# ---------------------------------------------------------------------------

def generate_idle(frame: int) -> str:
    """Idle: gentle bobbing, occasional blink."""
    bob = math.sin(frame * 0.8) * 2
    blink = frame == 2  # Blink on frame 3
    svg = svg_header()
    svg += base_face(y_offset=bob)
    svg += eyes_normal(y_offset=bob, blink=blink)
    svg += mouth_smile(y_offset=bob, width=0.8)
    svg += svg_footer()
    return svg


def generate_talking(frame: int) -> str:
    """Talking: mouth opens and closes."""
    bob = math.sin(frame * 1.0) * 1.5
    mouth_sizes = [0.8, 1.2, 0.6, 1.0]
    svg = svg_header()
    svg += base_face(y_offset=bob)
    svg += eyes_normal(y_offset=bob)
    svg += mouth_open(y_offset=bob, size=mouth_sizes[frame])
    svg += svg_footer()
    return svg


def generate_happy(frame: int) -> str:
    """Happy: curved smile eyes, sparkles."""
    bob = math.sin(frame * 1.2) * 3
    svg = svg_header()
    svg += base_face(y_offset=bob, body_scale=1.02)
    svg += eyes_happy(y_offset=bob)
    svg += mouth_smile(y_offset=bob, width=1.3)
    # Rotating sparkle positions
    sparkle_positions = [
        (75, 90), (180, 85), (170, 180), (85, 175),
    ]
    sx, sy = sparkle_positions[frame]
    svg += sparkle(sx, sy, size=5 + frame)
    svg += svg_footer()
    return svg


def generate_sad(frame: int) -> str:
    """Sad: downturned mouth, tears."""
    bob = math.sin(frame * 0.5) * 1
    svg = svg_header()
    svg += base_face(y_offset=bob + 3)  # Slightly droopy
    svg += eyes_sad(y_offset=bob + 3)
    svg += mouth_frown(y_offset=bob + 3)
    if frame >= 1:
        svg += tear(155, 140 + bob, frame)
    svg += svg_footer()
    return svg


def generate_surprised(frame: int) -> str:
    """Surprised: big round eyes, O mouth."""
    jump = [0, -8, -4, -1][frame]  # Little jump
    svg = svg_header()
    svg += base_face(y_offset=jump)
    svg += eyes_surprised(y_offset=jump)
    svg += mouth_o(y_offset=jump)
    if frame == 1:
        # Exclamation marks
        svg += f'<text x="170" y="95" font-size="18" fill="{SPARKLE_COLOR}" font-family="sans-serif" font-weight="bold">!</text>'
        svg += f'<text x="78" y="100" font-size="14" fill="{SPARKLE_COLOR}" font-family="sans-serif" font-weight="bold">!</text>'
    svg += svg_footer()
    return svg


def generate_thinking(frame: int) -> str:
    """Thinking: looking up, thought bubble dots."""
    svg = svg_header()
    svg += base_face(y_offset=0)
    svg += eyes_thinking(y_offset=0, spiral=(frame >= 2))
    svg += mouth_flat(y_offset=0)
    # Thought bubble dots
    dots = min(frame + 1, 3)
    for i in range(dots):
        r = 3 + i * 2
        x = 170 + i * 12
        y = 90 - i * 14
        svg += f'<circle cx="{x}" cy="{y}" r="{r}" fill="#CCCCCC" opacity="0.6"/>'
    svg += svg_footer()
    return svg


def generate_sleeping(frame: int) -> str:
    """Sleeping: closed eyes, Zzz."""
    bob = math.sin(frame * 0.4) * 1.5
    svg = svg_header()
    svg += base_face(y_offset=bob + 2)
    svg += eyes_sleeping(y_offset=bob + 2)
    svg += mouth_flat(y_offset=bob + 2)
    svg += zzz(170, 100, frame)
    svg += svg_footer()
    return svg


def generate_waving(frame: int) -> str:
    """Waving: happy face with a waving hand."""
    bob = math.sin(frame * 1.0) * 2
    svg = svg_header()
    svg += base_face(y_offset=bob)
    svg += eyes_happy(y_offset=bob)
    svg += mouth_smile(y_offset=bob, width=1.1)
    svg += wave_hand(frame)
    svg += svg_footer()
    return svg


def generate_angry(frame: int) -> str:
    """Angry: furrowed brows, grumpy mouth."""
    shake = [0, 2, -2, 1][frame]  # Slight shake
    svg = svg_header()
    svg += f'<g transform="translate({shake}, 0)">'
    svg += base_face(y_offset=0)
    svg += eyes_angry(y_offset=0)
    svg += mouth_frown(y_offset=-2)
    # Steam puffs
    if frame in (1, 3):
        svg += f'<circle cx="80" cy="85" r="6" fill="#DDDDDD" opacity="0.5"/>'
        svg += f'<circle cx="175" cy="80" r="7" fill="#DDDDDD" opacity="0.5"/>'
    svg += "</g>"
    svg += svg_footer()
    return svg


# Map state names to generators
STATE_GENERATORS = {
    "idle": generate_idle,
    "talking": generate_talking,
    "happy": generate_happy,
    "sad": generate_sad,
    "surprised": generate_surprised,
    "thinking": generate_thinking,
    "sleeping": generate_sleeping,
    "waving": generate_waving,
    "angry": generate_angry,
}


def svg_to_png(svg_str: str, size: int = 256) -> Image.Image:
    """Convert an SVG string to a PIL Image."""
    png_data = cairosvg.svg2png(
        bytestring=svg_str.encode("utf-8"),
        output_width=size,
        output_height=size,
    )
    return Image.open(io.BytesIO(png_data))


def generate_sprite_sheet(state: str) -> None:
    """Generate a 4-frame horizontal strip for the given state."""
    generator = STATE_GENERATORS[state]
    frames: list[Image.Image] = []

    for i in range(FRAMES_PER_STATE):
        svg = generator(i)
        img = svg_to_png(svg, FRAME_SIZE)
        frames.append(img)

    # Compose into horizontal strip
    sheet = Image.new("RGBA", (FRAME_SIZE * FRAMES_PER_STATE, FRAME_SIZE), (0, 0, 0, 0))
    for i, frame in enumerate(frames):
        sheet.paste(frame, (i * FRAME_SIZE, 0))

    output_path = OUTPUT_DIR / f"{state}.png"
    sheet.save(output_path, "PNG")
    print(f"  Generated: {output_path} ({sheet.width}x{sheet.height})")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Generating placeholder sprites in {OUTPUT_DIR}/")
    print(f"Frame size: {FRAME_SIZE}x{FRAME_SIZE}, Frames per state: {FRAMES_PER_STATE}")
    print()

    for state in STATE_GENERATORS:
        generate_sprite_sheet(state)

    print(f"\nDone! Generated {len(STATE_GENERATORS)} sprite sheets.")


if __name__ == "__main__":
    main()
