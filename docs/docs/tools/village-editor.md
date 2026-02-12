---
sidebar_position: 1
---

# Village Map Editor

Standalone tile map editor for painting Seraph's 16-bit RPG village. Outputs Tiled-compatible JSON maps consumed by the game's Phaser tilemap system (`VillageScene.ts`).

## Quick Start

```bash
cd editor
npm install
npm run dev
# Opens at http://localhost:3001
```

The editor loads tileset and sprite images from `../frontend/public/assets/` via a Vite middleware proxy — make sure the frontend assets are in place first.

## Layout

3-column layout with resizable panels:

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

Right panel width is draggable (200-800px). Section heights within the right panel are independently resizable.

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
| --- | Walkability | Show green (walkable) / red (blocked) overlay |
| --- | Animate | Play/pause tile animations and NPC walk cycles |

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

### Tile Stacking

Each cell in each layer holds a **CellStack** (`number[]`) — multiple tiles can be stacked in the same cell on the same layer. When painting:

- Brush pushes a tile onto the stack (if different from the current top)
- Eraser pops the top tile from the stack
- Stacks are serialized as Tiled sublayers with a `__N` suffix (e.g. `terrain__2`, `terrain__3`)

This allows layering details like a flower on top of grass within a single layer, without consuming a separate layer slot.

## Tilesets

33 tilesets (16px tiles) organized by category:

| Category | Count | Tilesets |
|----------|-------|----------|
| Village | 9 | Field, Village, Forest, Houses A/B/C, Harbor, Village House, Village Inn |
| World | 4 | Mountains, Winter, Desert Outside, Desert Inside |
| Dungeon | 6 | Castle, Castle New, Dark Castle, Dungeon, Dungeon Entrance, Caves |
| Interior | 1 | Interior custom |
| Animations | 13 | Field, Forest, Harbor, Castle (x3), Desert, Dungeon (x3), Magical, Mountains (x2) |

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
- Optional `isMagicEffect` flag to include the animation in the spell effect pool
- Exported as Tiled `tiles[].animation` fields

## Objects

Objects live on a separate layer and define game-logic entities.

### Tool Stations

Where the agent avatar walks when using a tool:

| Station | Tool Key | Animation State |
|---------|----------|-----------------|
| Well | web_search | at-well |
| Scroll-desk | read_file | at-signpost |
| Shrine | view_soul | at-bench |
| Anvil | shell_execute | at-forge |
| Telescope-tower | browse_webpage | at-tower |
| Sundial | calendar | at-clock |
| Pigeon-post | email | at-mailbox |

### Spawn Points

Starting positions for `agent_spawn` and `user_spawn`. Spawn points can be assigned a character sprite via the inline sprite picker (format: `Character_XXX_Y` where XXX = sheet number, Y = character 1-4).

### NPCs (Characters & Enemies)

Open the NPC Browser panel to browse and place NPCs:

- **Characters** — 53 sprite sheets x 4 characters each = 212 total (24x24 frames, 4-frame walk cycle x 4 directions)
- **Enemies** — 53 sprite sheets (24x24 frames, 3-frame walk cycle x 4 directions)

Click a sprite thumbnail to place an NPC at the map center, then use the Object tool to drag it into position.

## Building Interiors

Buildings support multi-floor interiors edited directly in the editor:

- Define a building zone (rectangular region on the map)
- Each building has one or more floors, each with its own 5-layer tile grid
- Place **portals** to define entry points and staircases (`entry`, `stairs_up`, `stairs_down`)
- In-game, `VillageScene` hides the exterior zone and renders the interior when the agent steps on an entry portal

## Walkability

Each tile type has a global walkability flag:

- **Green** = walkable, **Red** = blocked
- Toggle via the Walkability tool (click tiles on the map or tileset panel)
- The game builds an A* collision grid from these flags
- Saved in Tiled JSON as tile properties: `{name: "walkable", type: "bool", value: false}`

## Save / Load

| Action | Description |
|--------|-------------|
| **New** | Create blank 64x40 map (warns about unsaved changes) |
| **Load** | Import an existing Tiled JSON map (restores tiles, objects, animations, walkability) |
| **Save** | Download `village.json` (Tiled-compatible format) |
| **Export** | Download for manual copy to `frontend/public/maps/village.json` |

## Map Format

Output is standard Tiled JSON (`version: "1.10"`, `tiledversion: "1.10.2"`). The game's `VillageScene.ts` loads it via `this.load.tilemapTiledJSON()`.

- Map dimensions: configurable (default 64x40 tiles, 1024x640px at 16px per tile)
- 5 tile layers + sublayers for stacked cells + 1 object layer
- Walkability as tile properties
- Animation frames in tileset tile definitions
- Custom properties: `buildings` (JSON array of BuildingDef), `magic_effects` (animation pool)

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| 1-5 | Switch active layer |
| P / B / E / G / O / W | Switch tool |
| H | Toggle grid |
| Ctrl+Z | Undo |
| Ctrl+Shift+Z | Redo |
| Space+drag | Pan viewport |
| Middle-click drag | Pan viewport |
| Scroll wheel | Zoom in/out (0.5x-8x) |

## Architecture

```
editor/
├── src/
│   ├── main.tsx                    # Entry point
│   ├── App.tsx                     # Root layout (3-column)
│   ├── components/
│   │   ├── MapCanvas.tsx           # Canvas render loop (requestAnimationFrame)
│   │   ├── ToolBar.tsx             # Vertical tool palette
│   │   ├── LayerPanel.tsx          # 5 tile layers with visibility toggles
│   │   ├── TilesetPanel.tsx        # Tileset browser with category filter
│   │   ├── ObjectPanel.tsx         # Object catalog + placed objects list
│   │   ├── NPCBrowser.tsx          # Character & Enemy sprite gallery
│   │   ├── AnimationDefiner.tsx    # Tile animation editor
│   │   ├── BuildingPanel.tsx       # Building interior editor
│   │   ├── MenuBar.tsx             # File operations
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
│   │   ├── stamps.ts               # Multi-tile stamp templates
│   │   ├── tileset-loader.ts       # Async tileset loading with progress
│   │   └── undo.ts                 # Undo/redo stack (max 100 levels)
│   └── types/
│       ├── editor.ts               # CellStack, BuildingDef, TileAnimationGroup, etc.
│       └── map.ts                  # Tiled JSON format types
├── vite.config.ts                  # Vite + asset proxy middleware
└── package.json
```

**Stack:** React 19, Vite 6, TypeScript 5.6, Tailwind CSS 3, Zustand 5

## Asset Directories

The editor proxies assets from the main frontend:

```
frontend/public/assets/
├── tilesets/       20 tileset PNGs (16px tiles)
├── animations/     13 animation sheet PNGs (16px tiles)
├── characters/     53 character sprite sheets (384x96, 24x24 frames)
├── enemies/        53 enemy sprite sheets (72x96, 24x24 frames)
└── icons/          6 icon sheet PNGs
```
