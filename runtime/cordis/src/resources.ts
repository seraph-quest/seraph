/** Tracks cleanup independently from Cordis, which contains disposal errors. */
export class Resources {
  private owned = new Map<string, () => void | Promise<void>>();
  private uncertain = new Set<string>();
  track(id: string, cleanup: () => void | Promise<void>): void {
    if (this.owned.has(id) || this.uncertain.has(id) || this.owned.size >= 4096) throw new Error('resource ownership conflict');
    this.owned.set(id, cleanup); // Record ownership before starting/acquiring.
  }
  async release(id: string): Promise<void> {
    const cleanup = this.owned.get(id);
    if (!cleanup) return;
    try { await cleanup(); this.owned.delete(id); }
    catch { this.uncertain.add(id); throw new Error('owned cleanup remains unknown'); }
  }
  async dispose(): Promise<void> {
    for (const id of [...this.owned.keys()].reverse()) {
      try { await this.release(id); } catch { /* Liability retained in registry. */ }
    }
  }
  get remaining(): number { return this.owned.size; }
  get clean(): boolean { return this.owned.size === 0 && this.uncertain.size === 0; }
}
