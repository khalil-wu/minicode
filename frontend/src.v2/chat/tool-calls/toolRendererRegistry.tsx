import type { ComponentType, Dispatch, SetStateAction } from "react";
import type { ToolCallRecord } from "../../lib/tool-call-reducer";
import type { ViewMode } from "../../stores/types";

export interface ToolRendererProps {
  record: ToolCallRecord;
  viewMode?: ViewMode;
  compact?: boolean;
  inputSummary?: string | null;
  resultSummary?: string;
  rawResultSummary?: string;
  outputExpanded?: boolean;
  setOutputExpanded?: Dispatch<SetStateAction<boolean>>;
}

export type ToolRenderer = ComponentType<ToolRendererProps>;

export interface ToolActivityDetail {
  label: string;
  target: string;
  targetKind: "file" | "url" | "text";
  lineInfo?: string;
}

export interface ToolActivityDetailContext {
  record: ToolCallRecord;
  args: Record<string, unknown>;
  name: string;
  developerMode?: boolean;
}

export type ToolActivityDetailProvider = (context: ToolActivityDetailContext) => ToolActivityDetail | undefined;

const registry = new Map<string, ToolRenderer>();
const activityDetailRegistry = new Map<string, ToolActivityDetailProvider>();

const normalizeToolName = (name: string): string => name.trim().toLowerCase();

export function registerToolRenderer(name: string, renderer: ToolRenderer): void {
  const key = normalizeToolName(name);
  if (!key) return;
  registry.set(key, renderer);
}

export function unregisterToolRenderer(name: string): void {
  registry.delete(normalizeToolName(name));
}

export function getToolRenderer(name: string): ToolRenderer | undefined {
  return registry.get(normalizeToolName(name));
}

export function registerToolActivityDetail(name: string, provider: ToolActivityDetailProvider): void {
  const key = normalizeToolName(name);
  if (!key) return;
  activityDetailRegistry.set(key, provider);
}

export function unregisterToolActivityDetail(name: string): void {
  activityDetailRegistry.delete(normalizeToolName(name));
}

export function getToolActivityDetail(name: string): ToolActivityDetailProvider | undefined {
  return activityDetailRegistry.get(normalizeToolName(name));
}
