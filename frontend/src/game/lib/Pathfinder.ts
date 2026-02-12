import EasyStar from "easystarjs";

/**
 * A* pathfinding wrapper using easystarjs.
 * Operates on a grid of walkable/blocked tiles built from the tilemap.
 */
export class Pathfinder {
  private easystar: EasyStar.js;
  private grid: number[][];
  private mapWidth: number;
  private mapHeight: number;
  private tileSize: number;

  /**
   * @param collisionGrid 2D array: 0 = walkable, 1 = blocked
   * @param mapWidth Number of tile columns
   * @param mapHeight Number of tile rows
   * @param tileSize Pixel size of each tile (default 16)
   */
  constructor(
    collisionGrid: number[][],
    mapWidth: number,
    mapHeight: number,
    tileSize = 16
  ) {
    this.grid = collisionGrid;
    this.mapWidth = mapWidth;
    this.mapHeight = mapHeight;
    this.tileSize = tileSize;

    this.easystar = new EasyStar.js();
    this.easystar.setGrid(collisionGrid);
    this.easystar.setAcceptableTiles([0]);
    this.easystar.enableDiagonals();
    this.easystar.enableCornerCutting();
    this.easystar.setIterationsPerCalculation(1000);
  }

  /**
   * Replace the collision grid (used when entering/exiting buildings).
   */
  setGrid(collisionGrid: number[][]) {
    this.grid = collisionGrid;
    this.easystar.setGrid(collisionGrid);
  }

  /**
   * Find a path from pixel coords to pixel coords.
   * Returns array of pixel-center positions, or null if no path found.
   */
  findPath(
    fromX: number,
    fromY: number,
    toX: number,
    toY: number
  ): Promise<Array<{ x: number; y: number }> | null> {
    const startCol = Math.floor(fromX / this.tileSize);
    const startRow = Math.floor(fromY / this.tileSize);
    let endCol = Math.floor(toX / this.tileSize);
    let endRow = Math.floor(toY / this.tileSize);

    // Clamp to map bounds
    const clamp = (v: number, max: number) => Math.max(0, Math.min(max - 1, v));
    const sc = clamp(startCol, this.mapWidth);
    const sr = clamp(startRow, this.mapHeight);
    endCol = clamp(endCol, this.mapWidth);
    endRow = clamp(endRow, this.mapHeight);

    // If target is blocked, find nearest walkable tile
    if (this.grid[endRow]?.[endCol] !== 0) {
      const nearest = this.findNearestWalkable(toX, toY);
      if (!nearest) return Promise.resolve(null);
      endCol = Math.floor(nearest.x / this.tileSize);
      endRow = Math.floor(nearest.y / this.tileSize);
    }

    // If start is blocked, find nearest walkable
    if (this.grid[sr]?.[sc] !== 0) {
      const nearest = this.findNearestWalkable(fromX, fromY);
      if (!nearest) return Promise.resolve(null);
      // No path needed if start is blocked
      return Promise.resolve(null);
    }

    if (sc === endCol && sr === endRow) {
      return Promise.resolve([{ x: toX, y: toY }]);
    }

    return new Promise((resolve) => {
      this.easystar.findPath(sc, sr, endCol, endRow, (path) => {
        if (!path || path.length === 0) {
          resolve(null);
          return;
        }

        // Convert tile coords to pixel centers
        const pixelPath = path.map((p) => ({
          x: p.x * this.tileSize + this.tileSize / 2,
          y: p.y * this.tileSize + this.tileSize / 2,
        }));

        // Simplify: keep only direction changes to reduce tween count
        const simplified = this.simplifyPath(pixelPath);
        resolve(simplified);
      });
      this.easystar.calculate();
    });
  }

  /**
   * Simplify a path by removing intermediate points on straight lines.
   */
  private simplifyPath(
    path: Array<{ x: number; y: number }>
  ): Array<{ x: number; y: number }> {
    if (path.length <= 2) return path;

    const result = [path[0]];
    for (let i = 1; i < path.length - 1; i++) {
      const prev = path[i - 1];
      const curr = path[i];
      const next = path[i + 1];
      const dx1 = Math.sign(curr.x - prev.x);
      const dy1 = Math.sign(curr.y - prev.y);
      const dx2 = Math.sign(next.x - curr.x);
      const dy2 = Math.sign(next.y - curr.y);
      if (dx1 !== dx2 || dy1 !== dy2) {
        result.push(curr);
      }
    }
    result.push(path[path.length - 1]);
    return result;
  }

  /**
   * Get a random walkable tile position (pixel coords) within optional bounds.
   */
  getRandomWalkableTile(
    bounds?: { x: number; y: number; width: number; height: number }
  ): { x: number; y: number } | null {
    const candidates: Array<{ x: number; y: number }> = [];

    const minCol = bounds ? Math.floor(bounds.x / this.tileSize) : 0;
    const minRow = bounds ? Math.floor(bounds.y / this.tileSize) : 0;
    const maxCol = bounds
      ? Math.ceil((bounds.x + bounds.width) / this.tileSize)
      : this.mapWidth;
    const maxRow = bounds
      ? Math.ceil((bounds.y + bounds.height) / this.tileSize)
      : this.mapHeight;

    for (let r = Math.max(0, minRow); r < Math.min(this.mapHeight, maxRow); r++) {
      for (let c = Math.max(0, minCol); c < Math.min(this.mapWidth, maxCol); c++) {
        if (this.grid[r][c] === 0) {
          candidates.push({
            x: c * this.tileSize + this.tileSize / 2,
            y: r * this.tileSize + this.tileSize / 2,
          });
        }
      }
    }

    if (candidates.length === 0) return null;
    return candidates[Math.floor(Math.random() * candidates.length)];
  }

  /**
   * Find the nearest walkable tile to a given pixel position.
   */
  findNearestWalkable(
    px: number,
    py: number
  ): { x: number; y: number } | null {
    const col = Math.floor(px / this.tileSize);
    const row = Math.floor(py / this.tileSize);

    // Spiral outward search
    for (let radius = 0; radius < Math.max(this.mapWidth, this.mapHeight); radius++) {
      for (let dr = -radius; dr <= radius; dr++) {
        for (let dc = -radius; dc <= radius; dc++) {
          if (Math.abs(dr) !== radius && Math.abs(dc) !== radius) continue;
          const r = row + dr;
          const c = col + dc;
          if (r < 0 || r >= this.mapHeight || c < 0 || c >= this.mapWidth) continue;
          if (this.grid[r][c] === 0) {
            return {
              x: c * this.tileSize + this.tileSize / 2,
              y: r * this.tileSize + this.tileSize / 2,
            };
          }
        }
      }
    }
    return null;
  }

  /**
   * Expose the collision grid (0 = walkable, 1 = blocked).
   */
  getGrid(): readonly number[][] {
    return this.grid;
  }

  /**
   * Check if a pixel position is on a walkable tile.
   */
  isWalkable(px: number, py: number): boolean {
    const col = Math.floor(px / this.tileSize);
    const row = Math.floor(py / this.tileSize);
    if (col < 0 || col >= this.mapWidth || row < 0 || row >= this.mapHeight) return false;
    return this.grid[row][col] === 0;
  }
}
