// Module-level batch-ingestion store. It owns the queue AND the processing
// engine (preview + commit pools), so jobs keep running even when the user
// navigates away from the "Add several" page — the route component is just a
// view that subscribes to this store via useSyncExternalStore.
//
// Scope: survives in-app navigation for the session. A full page reload still
// clears it (in-flight HTTP requests can't be resumed, and queued File objects
// aren't serializable) — that's an explicit, documented limit.
import { useSyncExternalStore } from "react";
import { ingestCommit, ingestPreview, type PreviewInput } from "../api/endpoints";
import {
  buildOverrides,
  commitBlocked,
  initCuration,
  type Curation,
} from "../components/IngestReview";
import type { IngestPlan, IngestResult } from "../api/types";
import { queryClient } from "../queryClient";

export type BatchStatus =
  | "queued"
  | "previewing"
  | "ready"
  | "committing"
  | "committed"
  | "error";

export interface BatchItem {
  id: number;
  name: string;
  input: PreviewInput;
  status: BatchStatus;
  plan?: IngestPlan;
  curation?: Curation;
  result?: IngestResult;
  error?: string;
  expanded: boolean;
}

const PREVIEW_CONCURRENCY = 3;
const COMMIT_CONCURRENCY = 3;
// Read-side caches refreshed after each successful commit.
const INVALIDATE_KEYS = [["pages"], ["graph"], ["palette-graph"], ["sources"], ["reviews"]];

// localStorage key for the persisted queue (bump the suffix on a schema change).
const STORAGE_KEY = "mnesis.batch.queue.v1";

type Listener = () => void;

class BatchStore {
  private items: BatchItem[] = [];
  private listeners = new Set<Listener>();
  private nextId = 1;

  private previewQueue: number[] = [];
  private previewActive = 0;
  private commitQueue: number[] = [];
  private commitActive = 0;
  private persistTimer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    this.rehydrate(); // restore a queue persisted before a reload, and resume work
  }

  // ── external-store contract (useSyncExternalStore) ───────────────────────
  subscribe = (cb: Listener): (() => void) => {
    this.listeners.add(cb);
    return () => {
      this.listeners.delete(cb);
    };
  };
  getSnapshot = (): BatchItem[] => this.items;

  private emit() {
    this.listeners.forEach((l) => l());
  }
  private setItems(next: BatchItem[]) {
    this.items = next; // new reference each mutation -> stable snapshot when unchanged
    this.emit();
    this.schedulePersist();
  }
  private patch(id: number, p: Partial<BatchItem>) {
    this.setItems(this.items.map((it) => (it.id === id ? { ...it, ...p } : it)));
  }

  // ── persistence (survives a full page reload) ────────────────────────────
  // Only text-bearing items are persisted: a dropped File is a live handle that
  // can't be reconstructed across a reload (the browser won't re-open it by
  // path), so file items are intentionally not restored. In-flight statuses are
  // normalized on restore and unfinished previews are re-issued.
  private schedulePersist() {
    if (typeof localStorage === "undefined") return;
    if (this.persistTimer) clearTimeout(this.persistTimer);
    this.persistTimer = setTimeout(() => {
      this.persistTimer = null;
      this.persist();
    }, 250); // coalesce rapid mutations (preview pump, curation edits)
  }

  private persist() {
    try {
      const items = this.items
        .filter((it) => it.input.text != null) // text-only; files can't be restored
        .map((it) => ({
          id: it.id,
          name: it.name,
          input: { text: it.input.text, sourceRef: it.input.sourceRef },
          status: it.status,
          plan: it.plan,
          curation: it.curation,
          result: it.result,
          error: it.error,
          expanded: it.expanded,
        }));
      if (items.length === 0) {
        localStorage.removeItem(STORAGE_KEY);
        return;
      }
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ v: 1, nextId: this.nextId, items }));
    } catch {
      // Quota exceeded / serialization issue — persistence is best-effort; the
      // in-session queue keeps working regardless.
    }
  }

  private rehydrate() {
    if (typeof localStorage === "undefined") return;
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const data = JSON.parse(raw);
      if (!data || data.v !== 1 || !Array.isArray(data.items)) return;

      const restored: BatchItem[] = data.items.map((s: BatchItem): BatchItem => {
        // In-flight statuses can't carry across a reload. A previewing/queued
        // item with a plan already finished -> ready; without one -> re-queue.
        // A committing item is left as ready so the user re-commits deliberately
        // (we never auto-re-commit, to avoid duplicate writes).
        let status: BatchStatus = s.status;
        if (status === "previewing" || status === "queued") status = s.plan ? "ready" : "queued";
        else if (status === "committing") status = s.plan && s.curation ? "ready" : "queued";
        return {
          id: s.id,
          name: s.name,
          input: { text: s.input?.text, sourceRef: s.input?.sourceRef },
          status,
          plan: s.plan,
          curation: s.curation,
          result: s.result,
          error: s.error,
          expanded: !!s.expanded,
        };
      });

      this.items = restored; // set directly; avoid an immediate re-persist
      this.nextId =
        typeof data.nextId === "number"
          ? data.nextId
          : Math.max(0, ...restored.map((i) => i.id)) + 1;

      // Re-issue previews that hadn't completed before the reload.
      restored.filter((it) => it.status === "queued").forEach((it) => this.previewQueue.push(it.id));
      this.pumpPreview();
    } catch {
      // Corrupt storage — start fresh rather than crash.
    }
  }

  // ── queue management ─────────────────────────────────────────────────────
  enqueue(entries: Array<{ name: string; input: PreviewInput }>) {
    if (!entries.length) return;
    const startEmpty = this.items.length === 0;
    const created: BatchItem[] = entries.map((e, idx) => ({
      id: this.nextId++,
      name: e.name,
      input: e.input,
      status: "queued",
      expanded: startEmpty && idx === 0, // auto-expand the first when starting fresh
    }));
    this.setItems([...this.items, ...created]);
    created.forEach((it) => this.previewQueue.push(it.id));
    this.pumpPreview();
  }

  remove(id: number) {
    this.previewQueue = this.previewQueue.filter((x) => x !== id);
    this.commitQueue = this.commitQueue.filter((x) => x !== id);
    this.setItems(this.items.filter((it) => it.id !== id));
  }

  clearFinished() {
    const done = new Set(
      this.items.filter((it) => it.status === "committed").map((it) => it.id),
    );
    if (!done.size) return;
    this.setItems(this.items.filter((it) => !done.has(it.id)));
  }

  updateCuration(id: number, patch: Partial<Curation>) {
    const it = this.items.find((i) => i.id === id);
    if (!it?.curation) return;
    this.patch(id, { curation: { ...it.curation, ...patch } });
  }

  toggleExpanded(id: number) {
    const it = this.items.find((i) => i.id === id);
    if (it) this.patch(id, { expanded: !it.expanded });
  }

  // ── preview pool (the heavy LLM-extraction step; globally throttled) ─────
  private pumpPreview() {
    while (this.previewActive < PREVIEW_CONCURRENCY && this.previewQueue.length) {
      const id = this.previewQueue.shift()!;
      if (!this.items.some((i) => i.id === id)) continue; // removed before it started
      this.previewActive++;
      void this.previewOne(id).finally(() => {
        this.previewActive--;
        this.pumpPreview();
      });
    }
  }
  private async previewOne(id: number) {
    const item = this.items.find((i) => i.id === id);
    if (!item) return;
    this.patch(id, { status: "previewing", error: undefined });
    try {
      const plan = await ingestPreview(item.input);
      this.patch(id, { status: "ready", plan, curation: initCuration(plan) });
    } catch (e) {
      this.patch(id, { status: "error", error: (e as Error).message });
    }
  }

  // ── commit pool ──────────────────────────────────────────────────────────
  commit(id: number) {
    const it = this.items.find((i) => i.id === id);
    if (!it || it.status === "committing" || it.status === "committed") return;
    if (!it.plan || !it.curation) return;
    if (!this.commitQueue.includes(id)) this.commitQueue.push(id);
    this.pumpCommit();
  }

  commitAll() {
    this.items
      .filter(
        (it) => it.status === "ready" && it.plan && it.curation && !commitBlocked(it.plan, it.curation),
      )
      .forEach((it) => this.commit(it.id));
  }

  private pumpCommit() {
    while (this.commitActive < COMMIT_CONCURRENCY && this.commitQueue.length) {
      const id = this.commitQueue.shift()!;
      const it = this.items.find((i) => i.id === id);
      if (!it || it.status === "committed" || !it.plan || !it.curation) continue;
      this.commitActive++;
      void this.commitOne(id).finally(() => {
        this.commitActive--;
        this.pumpCommit();
      });
    }
  }
  private async commitOne(id: number) {
    const it = this.items.find((i) => i.id === id);
    if (!it?.plan || !it.curation) return;
    this.patch(id, { status: "committing", error: undefined });
    try {
      const result = await ingestCommit(it.plan, buildOverrides(it.curation));
      this.patch(id, { status: "committed", result });
      INVALIDATE_KEYS.forEach((key) => queryClient.invalidateQueries({ queryKey: key }));
    } catch (e) {
      this.patch(id, { status: "error", error: (e as Error).message });
    }
  }
}

export const batchStore = new BatchStore();

/** Subscribe a component to the live batch queue. */
export function useBatchItems(): BatchItem[] {
  return useSyncExternalStore(batchStore.subscribe, batchStore.getSnapshot, batchStore.getSnapshot);
}

/** Count of items still being worked on in the background (queue + in-flight). */
export function activeBatchCount(items: BatchItem[]): number {
  return items.filter(
    (it) => it.status === "queued" || it.status === "previewing" || it.status === "committing",
  ).length;
}
