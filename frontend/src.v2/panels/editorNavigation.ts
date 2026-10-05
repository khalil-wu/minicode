export interface EditorLocation { path: string; line: number; column: number }
export class EditorNavigation {
  entries: EditorLocation[] = [];
  index = -1;
  record(location: EditorLocation, explicitJump = false) {
    const current = this.entries[this.index];
    if (current?.path === location.path && current.line === location.line && current.column === location.column) return;
    if (!explicitJump && current?.path === location.path && Math.abs(current.line - location.line) < 10) this.entries[this.index] = location;
    else { this.entries = [...this.entries.slice(0, this.index + 1), location].slice(-100); this.index = this.entries.length - 1; }
  }
  go(delta: number) {
    const next = this.index + delta;
    if (next < 0 || next >= this.entries.length) return null;
    this.index = next;
    return this.entries[next];
  }
}
