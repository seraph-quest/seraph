import { useRef, useEffect, useCallback, useState } from "react";
import { useTilesetStore } from "../stores/tilesetStore";
import { renderTilesetPreview } from "../lib/canvas-renderer";
import { getTileSourceRect } from "../lib/tileset-loader";
import type { TileAnimationGroup, TileAnimationEntry, LoadedTileset } from "../types/editor";

const TILESET_SCALE = 2;

type Mode = "single" | "multi";

interface DraftGroup {
  name: string;
  frameDuration: number;
  mode: Mode;
  isMagicEffect: boolean;
  /** Single-tile mode: frames per entry (one entry = one anchor) */
  singleFrames: number[];
  /** Multi-tile mode: each element is a rectangular selection of local IDs */
  multiSelections: { localIds: number[]; cols: number; rows: number }[];
}

function newDraft(): DraftGroup {
  return { name: "", frameDuration: 200, mode: "single", isMagicEffect: false, singleFrames: [], multiSelections: [] };
}

export function AnimationDefiner() {
  const { tilesets, activeTilesetIndex, animationGroups } = useTilesetStore();
  const tileset = tilesets[activeTilesetIndex];

  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<DraftGroup | null>(null);

  const handleBack = () => {
    useTilesetStore.getState().setAnimDefinerOpen(false);
  };

  const handleNew = () => {
    setEditingId(null);
    setDraft(newDraft());
  };

  const handleEdit = (group: TileAnimationGroup) => {
    setEditingId(group.id);
    // Reconstruct draft from existing group
    if (group.entries.length > 0 && group.entries[0].frames.length > 0) {
      // Check if it's multi-tile (multiple entries with same frame count)
      const isMulti = group.entries.length > 1;
      if (isMulti) {
        // Reconstruct multi selections from entries
        const frameCount = group.entries[0].frames.length;
        const multiSelections: DraftGroup["multiSelections"] = [];
        for (let f = 0; f < frameCount; f++) {
          const localIds = group.entries.map((e) => e.frames[f]);
          // Guess dimensions — we store as flat list, assume square-ish
          const count = localIds.length;
          const cols = Math.ceil(Math.sqrt(count));
          const rows = Math.ceil(count / cols);
          multiSelections.push({ localIds, cols, rows });
        }
        setDraft({
          name: group.name,
          frameDuration: group.frameDuration,
          mode: "multi",
          isMagicEffect: group.isMagicEffect ?? false,
          singleFrames: [],
          multiSelections,
        });
      } else {
        setDraft({
          name: group.name,
          frameDuration: group.frameDuration,
          mode: "single",
          isMagicEffect: group.isMagicEffect ?? false,
          singleFrames: [...group.entries[0].frames],
          multiSelections: [],
        });
      }
    } else {
      setDraft({ name: group.name, frameDuration: group.frameDuration, mode: "single", isMagicEffect: group.isMagicEffect ?? false, singleFrames: [], multiSelections: [] });
    }
  };

  const handleDelete = (id: string) => {
    useTilesetStore.getState().removeAnimationGroup(id);
    if (editingId === id) {
      setEditingId(null);
      setDraft(null);
    }
  };

  const handleSave = () => {
    if (!draft || !tileset) return;
    const name = draft.name.trim() || "Untitled";
    let entries: TileAnimationEntry[];

    if (draft.mode === "single") {
      if (draft.singleFrames.length < 2) return;
      entries = [{ anchorLocalId: draft.singleFrames[0], frames: [...draft.singleFrames] }];
    } else {
      if (draft.multiSelections.length < 2) return;
      const first = draft.multiSelections[0];
      const tileCount = first.localIds.length;
      entries = [];
      for (let t = 0; t < tileCount; t++) {
        const anchorLocalId = first.localIds[t];
        const frames = draft.multiSelections.map((sel) => sel.localIds[t]);
        entries.push({ anchorLocalId, frames });
      }
    }

    const group: TileAnimationGroup = {
      id: editingId ?? crypto.randomUUID(),
      name,
      tilesetIndex: activeTilesetIndex,
      frameDuration: draft.frameDuration,
      entries,
      ...(draft.isMagicEffect ? { isMagicEffect: true } : {}),
    };

    const store = useTilesetStore.getState();
    if (editingId) {
      store.updateAnimationGroup(editingId, group);
    } else {
      store.addAnimationGroup(group);
    }

    setEditingId(null);
    setDraft(null);

    // Auto-close definer so user can select & paint tiles
    store.setAnimDefinerOpen(false);
  };

  const handleCancel = () => {
    setEditingId(null);
    setDraft(null);
  };

  if (!tileset) return null;

  // Filter groups for this tileset
  const groups = animationGroups.filter((g) => g.tilesetIndex === activeTilesetIndex);

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="flex items-center justify-between px-2 py-1 border-b border-gray-700 flex-shrink-0">
        <span className="text-xs font-bold text-gray-300">Animation Definer</span>
        <button onClick={handleBack} className="text-[10px] text-blue-400 hover:text-blue-300">
          Back to Tileset
        </button>
      </div>

      {/* Existing groups list */}
      {!draft && (
        <div className="flex-1 overflow-auto">
          <div className="p-1 space-y-1">
            {groups.length === 0 && (
              <div className="text-[10px] text-gray-500 p-2">No animations defined for this tileset.</div>
            )}
            {groups.map((group) => (
              <GroupRow
                key={group.id}
                group={group}
                tileset={tileset}
                onEdit={() => handleEdit(group)}
                onDelete={() => handleDelete(group.id)}
              />
            ))}
          </div>
          <div className="p-1 border-t border-gray-700 flex-shrink-0">
            <button
              onClick={handleNew}
              className="w-full py-1 text-[10px] bg-green-700 hover:bg-green-600 text-white rounded"
            >
              + New Animation
            </button>
          </div>
        </div>
      )}

      {/* Create/Edit form */}
      {draft && (
        <DraftEditor
          draft={draft}
          setDraft={setDraft}
          tileset={tileset}
          onSave={handleSave}
          onCancel={handleCancel}
          isEditing={editingId !== null}
        />
      )}
    </div>
  );
}

/** Row showing an existing animation group with mini preview */
function GroupRow({
  group,
  tileset,
  onEdit,
  onDelete,
}: {
  group: TileAnimationGroup;
  tileset: LoadedTileset;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const animRef = useRef<number>(0);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || group.entries.length === 0) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const entry = group.entries[0];
    const size = 32;
    canvas.width = size;
    canvas.height = size;

    const animate = (t: DOMHighResTimeStamp) => {
      const totalDuration = entry.frames.length * group.frameDuration;
      const elapsed = t % totalDuration;
      let accum = 0;
      let frameLocalId = entry.frames[0];
      for (const fId of entry.frames) {
        accum += group.frameDuration;
        if (elapsed < accum) {
          frameLocalId = fId;
          break;
        }
      }

      const { sx, sy, sw, sh } = getTileSourceRect(frameLocalId, tileset);
      ctx.imageSmoothingEnabled = false;
      ctx.clearRect(0, 0, size, size);
      ctx.drawImage(tileset.image, sx, sy, sw, sh, 0, 0, size, size);
      animRef.current = requestAnimationFrame(animate);
    };
    animRef.current = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(animRef.current);
  }, [group, tileset]);

  const frameCount = group.entries[0]?.frames.length ?? 0;
  const entryCount = group.entries.length;

  return (
    <div className="flex items-center gap-2 px-1 py-0.5 bg-gray-800 rounded">
      <canvas ref={canvasRef} className="flex-shrink-0" style={{ imageRendering: "pixelated", width: 32, height: 32 }} />
      <div className="flex-1 min-w-0">
        <div className="text-[10px] text-gray-200 truncate">
          {group.name}
          {group.isMagicEffect && (
            <span className="ml-1 px-1 py-0 text-[8px] bg-fuchsia-700 text-fuchsia-200 rounded">FX</span>
          )}
        </div>
        <div className="text-[9px] text-gray-500">
          {frameCount} frames, {entryCount} tile{entryCount > 1 ? "s" : ""}, {group.frameDuration}ms
        </div>
      </div>
      <button onClick={onEdit} className="text-[10px] text-blue-400 hover:text-blue-300 px-1">Edit</button>
      <button onClick={onDelete} className="text-[10px] text-red-400 hover:text-red-300 px-1">Del</button>
    </div>
  );
}

/** Draft editor for creating/editing an animation group */
function DraftEditor({
  draft,
  setDraft,
  tileset,
  onSave,
  onCancel,
  isEditing,
}: {
  draft: DraftGroup;
  setDraft: React.Dispatch<React.SetStateAction<DraftGroup | null>>;
  tileset: LoadedTileset;
  onSave: () => void;
  onCancel: () => void;
  isEditing: boolean;
}) {
  const tilesetCanvasRef = useRef<HTMLCanvasElement>(null);
  const previewCanvasRef = useRef<HTMLCanvasElement>(null);
  const previewAnimRef = useRef<number>(0);

  // Multi-tile drag state
  const [multiDragStart, setMultiDragStart] = useState<{ col: number; row: number } | null>(null);
  const [multiDragEnd, setMultiDragEnd] = useState<{ col: number; row: number } | null>(null);

  // Render tileset preview
  const renderTileset = useCallback(() => {
    const canvas = tilesetCanvasRef.current;
    if (!canvas || !tileset) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    canvas.width = tileset.imageWidth * TILESET_SCALE;
    canvas.height = tileset.imageHeight * TILESET_SCALE;

    renderTilesetPreview(ctx, tileset, TILESET_SCALE, null, false);

    // Highlight already-selected frames
    const ts = tileset.tileWidth * TILESET_SCALE;
    ctx.strokeStyle = "#22d3ee";
    ctx.lineWidth = 2;

    const allLocalIds = draft.mode === "single"
      ? draft.singleFrames
      : draft.multiSelections.flatMap((s) => s.localIds);

    for (const localId of allLocalIds) {
      const col = localId % tileset.columns;
      const row = Math.floor(localId / tileset.columns);
      ctx.strokeRect(col * ts + 1, row * ts + 1, ts - 2, ts - 2);
    }

    // Highlight multi drag preview
    if (draft.mode === "multi" && multiDragStart && multiDragEnd) {
      const minCol = Math.min(multiDragStart.col, multiDragEnd.col);
      const maxCol = Math.max(multiDragStart.col, multiDragEnd.col);
      const minRow = Math.min(multiDragStart.row, multiDragEnd.row);
      const maxRow = Math.max(multiDragStart.row, multiDragEnd.row);
      ctx.fillStyle = "rgba(34, 211, 238, 0.2)";
      ctx.fillRect(minCol * ts, minRow * ts, (maxCol - minCol + 1) * ts, (maxRow - minRow + 1) * ts);
      ctx.strokeStyle = "#22d3ee";
      ctx.lineWidth = 2;
      ctx.strokeRect(minCol * ts, minRow * ts, (maxCol - minCol + 1) * ts, (maxRow - minRow + 1) * ts);
    }
  }, [tileset, draft, multiDragStart, multiDragEnd]);

  useEffect(() => {
    renderTileset();
  }, [renderTileset]);

  // Live preview animation
  useEffect(() => {
    const canvas = previewCanvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const frames =
      draft.mode === "single"
        ? draft.singleFrames
        : draft.multiSelections.length > 0
          ? draft.multiSelections.map((s) => s.localIds[0])
          : [];

    if (frames.length === 0) {
      canvas.width = 48;
      canvas.height = 48;
      ctx.clearRect(0, 0, 48, 48);
      return;
    }

    const previewSize = 48;

    if (draft.mode === "single") {
      canvas.width = previewSize;
      canvas.height = previewSize;

      const totalDuration = frames.length * draft.frameDuration;
      const animate = (t: DOMHighResTimeStamp) => {
        const elapsed = t % totalDuration;
        let accum = 0;
        let localId = frames[0];
        for (const fId of frames) {
          accum += draft.frameDuration;
          if (elapsed < accum) {
            localId = fId;
            break;
          }
        }
        const { sx, sy, sw, sh } = getTileSourceRect(localId, tileset);
        ctx.imageSmoothingEnabled = false;
        ctx.clearRect(0, 0, previewSize, previewSize);
        ctx.drawImage(tileset.image, sx, sy, sw, sh, 0, 0, previewSize, previewSize);
        previewAnimRef.current = requestAnimationFrame(animate);
      };
      previewAnimRef.current = requestAnimationFrame(animate);
    } else if (draft.multiSelections.length >= 2) {
      const first = draft.multiSelections[0];
      const tilePx = 16;
      const pw = first.cols * tilePx * 2;
      const ph = first.rows * tilePx * 2;
      canvas.width = pw;
      canvas.height = ph;

      const totalDuration = draft.multiSelections.length * draft.frameDuration;
      const animate = (t: DOMHighResTimeStamp) => {
        const elapsed = t % totalDuration;
        let accum = 0;
        let selIdx = 0;
        for (let i = 0; i < draft.multiSelections.length; i++) {
          accum += draft.frameDuration;
          if (elapsed < accum) {
            selIdx = i;
            break;
          }
        }
        const sel = draft.multiSelections[selIdx];
        ctx.imageSmoothingEnabled = false;
        ctx.clearRect(0, 0, pw, ph);
        for (let i = 0; i < sel.localIds.length; i++) {
          const lc = i % sel.cols;
          const lr = Math.floor(i / sel.cols);
          const { sx, sy, sw, sh } = getTileSourceRect(sel.localIds[i], tileset);
          ctx.drawImage(tileset.image, sx, sy, sw, sh, lc * tilePx * 2, lr * tilePx * 2, tilePx * 2, tilePx * 2);
        }
        previewAnimRef.current = requestAnimationFrame(animate);
      };
      previewAnimRef.current = requestAnimationFrame(animate);
    }

    return () => cancelAnimationFrame(previewAnimRef.current);
  }, [draft.singleFrames, draft.multiSelections, draft.frameDuration, draft.mode, tileset]);

  const getTileCoord = useCallback(
    (e: React.MouseEvent) => {
      if (!tileset || !tilesetCanvasRef.current) return null;
      const rect = tilesetCanvasRef.current.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const col = Math.floor(x / (tileset.tileWidth * TILESET_SCALE));
      const row = Math.floor(y / (tileset.tileHeight * TILESET_SCALE));
      if (col < 0 || col >= tileset.columns || row < 0 || row >= tileset.rows) return null;
      return { col, row };
    },
    [tileset]
  );

  const onTilesetMouseDown = useCallback(
    (e: React.MouseEvent) => {
      const coord = getTileCoord(e);
      if (!coord) return;

      if (draft.mode === "single") {
        const localId = coord.row * tileset.columns + coord.col;
        setDraft((d) => d ? { ...d, singleFrames: [...d.singleFrames, localId] } : d);
      } else {
        setMultiDragStart(coord);
        setMultiDragEnd(coord);
      }
    },
    [getTileCoord, draft.mode, tileset, setDraft]
  );

  const onTilesetMouseMove = useCallback(
    (e: React.MouseEvent) => {
      if (draft.mode === "multi" && multiDragStart) {
        const coord = getTileCoord(e);
        if (coord) setMultiDragEnd(coord);
      }
    },
    [draft.mode, multiDragStart, getTileCoord]
  );

  const onTilesetMouseUp = useCallback(() => {
    if (draft.mode === "multi" && multiDragStart && multiDragEnd) {
      const minCol = Math.min(multiDragStart.col, multiDragEnd.col);
      const maxCol = Math.max(multiDragStart.col, multiDragEnd.col);
      const minRow = Math.min(multiDragStart.row, multiDragEnd.row);
      const maxRow = Math.max(multiDragStart.row, multiDragEnd.row);
      const cols = maxCol - minCol + 1;
      const rows = maxRow - minRow + 1;

      // Validate dimensions match first selection
      if (draft.multiSelections.length > 0) {
        const first = draft.multiSelections[0];
        if (cols !== first.cols || rows !== first.rows) {
          setMultiDragStart(null);
          setMultiDragEnd(null);
          return; // Dimensions must match
        }
      }

      const localIds: number[] = [];
      for (let r = minRow; r <= maxRow; r++) {
        for (let c = minCol; c <= maxCol; c++) {
          localIds.push(r * tileset.columns + c);
        }
      }

      setDraft((d) =>
        d ? { ...d, multiSelections: [...d.multiSelections, { localIds, cols, rows }] } : d
      );
      setMultiDragStart(null);
      setMultiDragEnd(null);
    }
  }, [draft.mode, draft.multiSelections, multiDragStart, multiDragEnd, tileset, setDraft]);

  const removeFrame = (index: number) => {
    if (draft.mode === "single") {
      setDraft((d) => d ? { ...d, singleFrames: d.singleFrames.filter((_, i) => i !== index) } : d);
    } else {
      setDraft((d) => d ? { ...d, multiSelections: d.multiSelections.filter((_, i) => i !== index) } : d);
    }
  };

  const canSave =
    draft.mode === "single" ? draft.singleFrames.length >= 2 : draft.multiSelections.length >= 2;

  return (
    <div className="flex flex-col h-full min-h-0 overflow-hidden">
      {/* Form header */}
      <div className="px-2 py-1 space-y-1 border-b border-gray-700 flex-shrink-0">
        <div className="flex gap-1 items-center">
          <input
            value={draft.name}
            onChange={(e) => setDraft((d) => d ? { ...d, name: e.target.value } : d)}
            placeholder="Animation name"
            className="flex-1 bg-gray-800 text-gray-200 text-[10px] px-1 py-0.5 rounded border border-gray-600"
          />
          <label className="text-[9px] text-gray-400">ms:</label>
          <input
            type="number"
            value={draft.frameDuration}
            onChange={(e) =>
              setDraft((d) => d ? { ...d, frameDuration: Math.max(50, Number(e.target.value) || 200) } : d)
            }
            className="w-14 bg-gray-800 text-gray-200 text-[10px] px-1 py-0.5 rounded border border-gray-600"
          />
        </div>
        <div className="flex gap-1">
          <button
            onClick={() => setDraft((d) => d ? { ...d, mode: "single", multiSelections: [], singleFrames: [] } : d)}
            className={`px-2 py-0.5 text-[10px] rounded ${
              draft.mode === "single" ? "bg-cyan-700 text-white" : "bg-gray-700 text-gray-400"
            }`}
          >
            Single tile
          </button>
          <button
            onClick={() => setDraft((d) => d ? { ...d, mode: "multi", singleFrames: [], multiSelections: [] } : d)}
            className={`px-2 py-0.5 text-[10px] rounded ${
              draft.mode === "multi" ? "bg-cyan-700 text-white" : "bg-gray-700 text-gray-400"
            }`}
          >
            Multi-tile
          </button>
        </div>
        <label className="flex items-center gap-1 text-[10px] text-gray-300 cursor-pointer">
          <input
            type="checkbox"
            checked={draft.isMagicEffect}
            onChange={(e) => setDraft((d) => d ? { ...d, isMagicEffect: e.target.checked } : d)}
            className="accent-fuchsia-500"
          />
          Magic Effect (tool spell pool)
        </label>
      </div>

      {/* Tileset canvas for clicking */}
      <div className="flex-1 overflow-auto p-1">
        <div className="text-[9px] text-gray-500 mb-1">
          {draft.mode === "single"
            ? "Click tiles to add frames in sequence."
            : "Click-drag a rectangle for each frame. All selections must be the same size."}
        </div>
        <canvas
          ref={tilesetCanvasRef}
          className="cursor-pointer"
          style={{ imageRendering: "pixelated" }}
          onMouseDown={onTilesetMouseDown}
          onMouseMove={onTilesetMouseMove}
          onMouseUp={onTilesetMouseUp}
          onMouseLeave={() => {
            if (draft.mode === "multi") {
              setMultiDragStart(null);
              setMultiDragEnd(null);
            }
          }}
        />
      </div>

      {/* Frame strip */}
      <div className="border-t border-gray-700 px-1 py-1 flex-shrink-0">
        <div className="text-[9px] text-gray-400 mb-0.5">
          Frames ({draft.mode === "single" ? draft.singleFrames.length : draft.multiSelections.length}):
        </div>
        <div className="flex gap-1 overflow-x-auto pb-1">
          {draft.mode === "single" &&
            draft.singleFrames.map((localId, i) => (
              <FrameThumb key={i} localId={localId} tileset={tileset} index={i} onRemove={removeFrame} />
            ))}
          {draft.mode === "multi" &&
            draft.multiSelections.map((sel, i) => (
              <MultiFrameThumb key={i} selection={sel} tileset={tileset} index={i} onRemove={removeFrame} />
            ))}
        </div>
      </div>

      {/* Live preview + save/cancel */}
      <div className="flex items-center gap-2 px-2 py-1 border-t border-gray-700 flex-shrink-0">
        <canvas
          ref={previewCanvasRef}
          style={{ imageRendering: "pixelated", width: 48, height: 48, flexShrink: 0 }}
        />
        <div className="flex-1" />
        <button onClick={onCancel} className="px-2 py-0.5 text-[10px] bg-gray-700 text-gray-300 rounded hover:bg-gray-600">
          Cancel
        </button>
        <button
          onClick={onSave}
          disabled={!canSave}
          className={`px-2 py-0.5 text-[10px] rounded ${
            canSave ? "bg-green-700 text-white hover:bg-green-600" : "bg-gray-700 text-gray-500 cursor-not-allowed"
          }`}
        >
          {isEditing ? "Update" : "Save"}
        </button>
      </div>
    </div>
  );
}

/** Single-tile frame thumbnail */
function FrameThumb({
  localId,
  tileset,
  index,
  onRemove,
}: {
  localId: number;
  tileset: LoadedTileset;
  index: number;
  onRemove: (i: number) => void;
}) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    canvas.width = 24;
    canvas.height = 24;
    const { sx, sy, sw, sh } = getTileSourceRect(localId, tileset);
    ctx.imageSmoothingEnabled = false;
    ctx.clearRect(0, 0, 24, 24);
    ctx.drawImage(tileset.image, sx, sy, sw, sh, 0, 0, 24, 24);
  }, [localId, tileset]);

  return (
    <div className="relative group flex-shrink-0">
      <canvas ref={ref} style={{ imageRendering: "pixelated", width: 24, height: 24 }} className="border border-gray-600 rounded" />
      <button
        onClick={() => onRemove(index)}
        className="absolute -top-1 -right-1 w-3 h-3 bg-red-600 text-white text-[7px] rounded-full leading-none hidden group-hover:flex items-center justify-center"
      >
        x
      </button>
      <div className="text-[7px] text-gray-500 text-center">{index + 1}</div>
    </div>
  );
}

/** Multi-tile frame thumbnail (shows a miniature of the rectangle) */
function MultiFrameThumb({
  selection,
  tileset,
  index,
  onRemove,
}: {
  selection: { localIds: number[]; cols: number; rows: number };
  tileset: LoadedTileset;
  index: number;
  onRemove: (i: number) => void;
}) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const tilePx = 8; // small preview
    canvas.width = selection.cols * tilePx;
    canvas.height = selection.rows * tilePx;
    ctx.imageSmoothingEnabled = false;
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    for (let i = 0; i < selection.localIds.length; i++) {
      const lc = i % selection.cols;
      const lr = Math.floor(i / selection.cols);
      const { sx, sy, sw, sh } = getTileSourceRect(selection.localIds[i], tileset);
      ctx.drawImage(tileset.image, sx, sy, sw, sh, lc * tilePx, lr * tilePx, tilePx, tilePx);
    }
  }, [selection, tileset]);

  return (
    <div className="relative group flex-shrink-0">
      <canvas
        ref={ref}
        style={{
          imageRendering: "pixelated",
          width: selection.cols * 8,
          height: selection.rows * 8,
        }}
        className="border border-gray-600 rounded"
      />
      <button
        onClick={() => onRemove(index)}
        className="absolute -top-1 -right-1 w-3 h-3 bg-red-600 text-white text-[7px] rounded-full leading-none hidden group-hover:flex items-center justify-center"
      >
        x
      </button>
      <div className="text-[7px] text-gray-500 text-center">F{index + 1}</div>
    </div>
  );
}
