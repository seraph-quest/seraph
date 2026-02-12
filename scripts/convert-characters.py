#!/usr/bin/env python3
"""Convert Cute RPG World character sprites to Phaser atlas format.

Source format: 384×96px = 12 cols × 3 rows of 32×32 frames.
  - 4 skin tones × 3 walk frames per row
  - Row 0: facing down, Row 1: facing left, Row 2: facing up
  - Right direction = horizontal flip of left

Output format: 192×128px = 6 cols × 4 rows of 32×32 frames.
  - Row 0: down  (6 frames: cycle 0,1,2,1,0,1)
  - Row 1: left  (6 frames: cycle 0,1,2,1,0,1)
  - Row 2: right (6 frames: flipped left)
  - Row 3: up    (6 frames: cycle 0,1,2,1,0,1)
  + atlas JSON with named frames matching existing game animations
"""

import argparse
import json
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("Error: Pillow is required. Install with: pip install Pillow")
    sys.exit(1)

FRAME_W = 32
FRAME_H = 32
WALK_COLS = 6
DIRECTIONS = 4
CYCLE = [0, 1, 2, 1, 0, 1]  # 3 source frames -> 6 output frames

# Source rows: down=0, left=1, up=2
SRC_ROWS = {"down": 0, "left": 1, "up": 2}


def extract_character(src_path: Path, skin_tone: int = 0) -> Image.Image:
    """Extract one skin tone from source sprite sheet and build output atlas."""
    src = Image.open(src_path).convert("RGBA")

    # Validate dimensions
    if src.width != 384 or src.height != 96:
        print(f"Warning: unexpected size {src.width}x{src.height}, expected 384x96")

    out = Image.new("RGBA", (WALK_COLS * FRAME_W, DIRECTIONS * FRAME_H), (0, 0, 0, 0))

    # Column offset for this skin tone (each skin tone = 3 columns)
    col_offset = skin_tone * 3

    # Row mapping: output row -> (source row name, flip)
    row_map = [
        ("down", False),   # output row 0
        ("left", False),   # output row 1
        ("left", True),    # output row 2 (right = flipped left)
        ("up", False),     # output row 3
    ]

    for out_row, (direction, flip) in enumerate(row_map):
        src_row = SRC_ROWS[direction]
        for out_col, src_frame_idx in enumerate(CYCLE):
            src_col = col_offset + src_frame_idx
            # Crop source frame
            sx = src_col * FRAME_W
            sy = src_row * FRAME_H
            frame = src.crop((sx, sy, sx + FRAME_W, sy + FRAME_H))
            if flip:
                frame = frame.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            # Paste into output
            dx = out_col * FRAME_W
            dy = out_row * FRAME_H
            out.paste(frame, (dx, dy))

    return out


def generate_atlas_json(name: str) -> dict:
    """Generate Phaser atlas JSON matching existing frame naming convention."""
    directions = ["down", "left", "right", "up"]
    frames = []

    for row, direction in enumerate(directions):
        # Walk frames: direction-walk.000 through direction-walk.005
        for col in range(WALK_COLS):
            frames.append({
                "filename": f"{direction}-walk.{col:03d}",
                "frame": {"w": FRAME_W, "h": FRAME_H, "x": col * FRAME_W, "y": row * FRAME_H},
                "anchor": {"x": 0.5, "y": 0.5},
            })
        # Idle frame: just the direction name, points to col 0
        frames.append({
            "filename": direction,
            "frame": {"w": FRAME_W, "h": FRAME_H, "x": 0, "y": row * FRAME_H},
            "anchor": {"x": 0.5, "y": 0.5},
        })

    return {
        "frames": frames,
        "meta": {
            "description": f"{name} sprite - converted from Cute RPG World",
            "size": {"w": WALK_COLS * FRAME_W, "h": DIRECTIONS * FRAME_H},
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Convert Cute RPG World characters to Phaser atlas")
    parser.add_argument("source", help="Source character PNG (e.g., Character_001.png)")
    parser.add_argument("output_name", help="Output name (e.g., 'agent' or 'user-avatar')")
    parser.add_argument("--skin", type=int, default=0, choices=[0, 1, 2, 3],
                        help="Skin tone index (0-3, default: 0)")
    parser.add_argument("--output-dir", type=str, default=None,
                        help="Output directory (default: frontend/public/assets/)")
    args = parser.parse_args()

    src_path = Path(args.source)
    if not src_path.exists():
        print(f"Error: source file not found: {src_path}")
        sys.exit(1)

    if args.output_dir:
        out_dir = Path(args.output_dir)
    else:
        out_dir = Path(__file__).parent.parent / "frontend" / "public" / "assets"

    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Converting {src_path.name} (skin tone {args.skin}) -> {args.output_name}")

    # Generate sprite sheet
    atlas_img = extract_character(src_path, args.skin)
    png_path = out_dir / f"{args.output_name}.png"
    atlas_img.save(png_path)
    print(f"  Wrote {png_path}")

    # Generate atlas JSON
    atlas_json = generate_atlas_json(args.output_name)
    json_path = out_dir / f"{args.output_name}-atlas.json"
    with open(json_path, "w") as f:
        json.dump(atlas_json, f, indent=4)
    print(f"  Wrote {json_path}")


if __name__ == "__main__":
    main()
