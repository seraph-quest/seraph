# Seraph Village Map Editor

Standalone tile map editor for painting Seraph's 16-bit RPG village. Outputs Tiled-compatible JSON maps consumed by the game's Phaser tilemap system.

## Quick Start

```bash
cd editor
npm install
npm run dev
# Opens at http://localhost:3001
```

The editor loads tileset and sprite images from `../frontend/public/assets/` via a Vite middleware proxy — make sure the frontend assets are in place first.

## Architecture

```
editor/
├── src/
│   ├── main.tsx                    # Entry point
│   ├── App.tsx                     # Root layout (3-column: toolbar | canvas | panels)
│   ├── components/
│   │   ├── MapCanvas.tsx           # Canvas render loop (requestAnimationFrame)
│   │   ├── ToolBar.tsx             # Vertical tool palette (6 tools + 3 view toggles)
│   │   ├── LayerPanel.tsx          # 5 tile layers with visibility toggles
│   │   ├── TilesetPanel.tsx        # Tileset browser with category filter + tile selection
│   │   ├── ObjectPanel.tsx         # Object catalog + placed objects list
│   │   ├── NPCBrowser.tsx          # Character (212) & Enemy (53) sprite gallery
│   │   ├── AnimationDefiner.tsx    # Tile animation editor (single/multi-tile modes)
│   │   ├── MenuBar.tsx             # File operations (New, Load, Save, Export)
│   │   └── Tooltip.tsx             # Portal-based hover tooltips
│   ├── hooks/
│   │   ├── useCanvasInteraction.ts # Mouse/keyboard: paint, pan, zoom, object drag
│   │   └── useKeyboardShortcuts.ts # Global keyboard shortcuts
│   ├── stores/
│   │   ├── editorStore.ts          # Map data, layers, viewport, tools, undo/redo
│   │   └── tilesetStore.ts         # Tilesets, tile selection, animations, sprite cache
│   ├── lib/
│   │   ├── canvas-renderer.ts      # Map + tileset rendering with animation support
│   │   ├── flood-fill.ts           # Stack-based flood fill algorithm
│   │   ├── map-io.ts               # Tiled JSON serialization/deserialization
│   │   ├── sprite-registry.ts      # Static registry of 212 characters + 53 enemies
│   │   ├── stamps.ts               # Multi-tile stamp templates (placeholder)
│   │   ├── tileset-loader.ts       # Async tileset loading with progress
│   │   └── undo.ts                 # Undo/redo stack (max 100 levels)
│   └── types/
│       ├── editor.ts               # EditorTool, MapObject, TileAnimationGroup, etc.
│       └── map.ts                  # Tiled JSON format types
├── vite.config.ts                  # Vite + asset proxy middleware
└── package.json
```

**Stack:** React 19, Vite 6, TypeScript 5.6, Tailwind CSS 3, Zustand 5

## Layout

The editor uses a 3-column layout with resizable panels:

```
┌──────────────────────────────────────────────────────────────┐
│  MenuBar  (New | Load | Save | Export)                       │
├──┬───────────────────────────────────┬───────────────────────┤
│  │                                   │  LayerPanel (140px)   │
│T │                                   ├───────────────────────┤
│o │                                   │  ObjectPanel (160px)  │
│o │        MapCanvas                  ├───────────────────────┤
│l │     (tile + object rendering)     │  NPCBrowser (200px)   │
│b │                                   ├───────────────────────┤
│a │                                   │                       │
│r │                                   │  TilesetPanel (flex)  │
│  │                                   │                       │
└──┴───────────────────────────────────┴───────────────────────┘
```

Right panel width is draggable (200–800px). Section heights within the right panel are independently resizable.

## Tools

| Key | Tool | Description |
|-----|------|-------------|
| P | Hand | Pan the viewport (also: Space+drag or middle-click drag) |
| B | Brush | Paint selected tile(s) on the active layer |
| E | Eraser | Clear tiles (set to empty) |
| G | Fill | Flood fill connected area with the selected tile |
| O | Object | Select and drag-to-move placed objects (snap to tile grid) |
| W | Walkability | Toggle walkability for tile types on the map or tileset panel |

### View Toggles

| Key | Toggle | Description |
|-----|--------|-------------|
| H | Grid | Show/hide 16px grid overlay |
| — | Walkability | Show green (walkable) / red (blocked) overlay |
| — | Animate | Play/pause tile animations and NPC walk cycles |

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| 1–5 | Switch active layer |
| Ctrl+Z | Undo |
| Ctrl+Shift+Z | Redo |
| Space+drag | Pan viewport |
| Middle-click drag | Pan viewport |
| Scroll wheel | Zoom in/out (0.5x–8x) |

## Layers

5 tile layers (bottom to top) + 1 object layer:

| # | Layer | Purpose |
|---|-------|---------|
| 1 | Ground | Base terrain (grass, dirt, water) |
| 2 | Terrain | Path edges, fences, transitions |
| 3 | Buildings | Walls, roofs, doors |
| 4 | Decorations | Props, flowers, signs |
| 5 | Treetops | Tree canopies (renders above characters) |

Active layer is painted by brush/eraser/fill. Non-active layers are dimmed to 0.7 opacity on the canvas. Toggle layer visibility with the eye icon.

## Tilesets

33 tilesets (16px tiles) organized by category:

| Category | Count | Tilesets |
|----------|-------|----------|
| Village | 9 | Field, Village, Forest, Houses A/B/C, Harbor, Village House, Village Inn |
| World | 4 | Mountains, Winter, Desert Outside, Desert Inside |
| Dungeon | 6 | Castle, Castle New, Dark Castle, Dungeon, Dungeon Entrance, Caves |
| Interior | 1 | Interior custom |
| Animations | 13 | Field, Forest, Harbor, Castle (×3), Desert, Dungeon (×3), Magical, Mountains (×2) |

Use the category buttons above the tileset tabs to filter. Animation tilesets show with an "(anim)" suffix.

### Tile Selection

- **Single click** — Select one tile
- **Click+drag** — Select a rectangular region (stamps as a block when painting)
- Selection dimensions shown below the tileset canvas

### Tile Animations

Open the animation definer from the tileset panel to create animated tiles:

- **Single-tile mode** — Click tiles in sequence to define frames (e.g., water ripple)
- **Multi-tile mode** — Drag rectangles for each frame; all tiles animate in sync (e.g., waterfall)
- Configurable frame duration (ms) with live preview
- Exported as Tiled `tiles[].animation` fields

## Objects

Objects live on a separate layer and define game-logic entities.

### Spawn Points

Starting positions for `agent_spawn` and `user_spawn`. Spawn points can be assigned a character sprite via the inline sprite picker.

### NPCs (Characters & Enemies)

Open the NPC Browser panel to browse and place NPCs:

- **Characters** — 53 sprite sheets × 4 characters each = 212 total (24×24 frames, 4-frame walk cycle × 4 directions)
- **Enemies** — 53 sprite sheets (24×24 frames, 3-frame walk cycle × 4 directions)

Click a sprite thumbnail to place an NPC at the map center, then use the Object tool to drag it into position.

### Drag-to-Move

Select the **Object tool (O)**, then click and drag any placed object. The cursor changes to a grab hand when hovering over a movable object. Objects snap to the 16px tile grid on release.

## Sprite Rendering

Character and enemy sprite sheets use 24×24 pixel frames, but the actual character art occupies roughly 16×16 pixels within each frame (with transparent margins). The editor crops a 16×16 region from each frame at offset (4px left, 8px top) so sprites render at exactly one tile size on the grid — pixel-perfect with no scaling artifacts.

## Walkability

Each tile type has a global walkability flag:
- **Green** = walkable, **Red** = blocked
- Toggle via the Walkability tool (click tiles on the map or tileset panel)
- The game builds an A* collision grid from these flags
- Saved in Tiled JSON as tile properties: `{name: "walkable", type: "bool", value: false}`

## Save / Load

| Action | Description |
|--------|-------------|
| **New** | Create blank 64×40 map (warns about unsaved changes) |
| **Load** | Import an existing Tiled JSON map (restores tiles, objects, animations, walkability) |
| **Save** | Download `village.json` (Tiled-compatible format) |
| **Export** | Download for manual copy to `frontend/public/maps/village.json` |

## Map Format

Output is standard Tiled JSON (`version: "1.10"`, `tiledversion: "1.10.2"`). The game's `VillageScene.ts` loads it via `this.load.tilemapTiledJSON()`.

- Map dimensions: 64×40 tiles (1024×640px at 16px per tile)
- 5 tile layers + 1 object layer
- Walkability as tile properties
- Animation frames in tileset tile definitions

## Undo / Redo

- **Ctrl+Z** / **Ctrl+Shift+Z** — up to 100 undo levels
- Continuous brush/eraser strokes are batched into a single undo entry
- Fill operations are a single undo entry

## Asset Directories

The editor proxies assets from the main frontend:

```
frontend/public/assets/
├── tilesets/       20 tileset PNGs (16px tiles)
├── animations/     13 animation sheet PNGs (16px tiles)
├── characters/     53 character sprite sheets (384×96, 24×24 frames)
├── enemies/        53 enemy sprite sheets (72×96, 24×24 frames)
└── icons/          6 icon sheet PNGs
```
