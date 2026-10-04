/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../protocol/api";
import { useAppStore } from "../stores";
import { readWorkspaceFile, readWorkspaceProjectIndex } from "../protocol/workspace";
import { useWorkspaceModelIndex } from "./useWorkspaceModelIndex";

const runtime = vi.hoisted(() => ({
  owners: [] as Array<{ root: string; applySnapshot: ReturnType<typeof vi.fn>; addConfigurations: ReturnType<typeof vi.fn>; noteFileChanges: ReturnType<typeof vi.fn>; dispose: ReturnType<typeof vi.fn> }>,
  configurationRequests: vi.fn(async (): Promise<string[]> => []),
  configurationDiagnostics: vi.fn(async (): Promise<Array<{ path: string; message: string }>> => []),
  addConfigurationFiles: vi.fn(async () => {}),
}));
vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }) });
});
vi.mock("../protocol/workspace", () => ({ readWorkspaceProjectIndex: vi.fn(), readWorkspaceFile: vi.fn() }));
vi.mock("./monacoLanguageServices", async (original) => ({
  ...await original<typeof import("./monacoLanguageServices")>(),
  syncWorkspaceTypeScriptModels: vi.fn(async () => ({ configurationRequests: runtime.configurationRequests, configurationDiagnostics: runtime.configurationDiagnostics, addConfigurationFiles: runtime.addConfigurationFiles })),
}));
vi.mock("./workspaceModelIndex", async (original) => ({
  ...await original<typeof import("./workspaceModelIndex")>(),
  WorkspaceModelIndex: class {
    owner;
    count = 0;
    constructor(_monaco: unknown, readonly workspaceRoot: string) {
      this.owner = { root: workspaceRoot, applySnapshot: vi.fn(async (snapshot) => { this.count = snapshot.files.length; return []; }), addConfigurations: vi.fn(), noteFileChanges: vi.fn(), dispose: vi.fn() };
      runtime.owners.push(this.owner);
    }
    applySnapshot = (...args: unknown[]) => this.owner.applySnapshot(...args);
    addConfigurations = (...args: unknown[]) => this.owner.addConfigurations(...args);
    noteFileChanges = (...args: unknown[]) => this.owner.noteFileChanges(...args);
    dispose = () => this.owner.dispose();
    resources = () => [];
    sourceCount = () => this.count;
    ownsModel = () => true;
    configurationPath = (uri: string) => decodeURIComponent(new URL(uri).pathname);
  },
}));

const snapshot = (root = "/project") => ({ workspace_root: root, complete: true, issues: [], files: [{ path: "src/main.ts", content: "export const value = 1", content_hash: "base", kind: "source" as const }] });
const Fixture = ({ root = "/project" }) => {
  const index = useWorkspaceModelIndex(root);
  return <>
    <button onClick={() => index.initialize({} as never)}>初始化</button>
    <button onClick={index.refresh}>刷新</button>
    <output>{index.status.phase}:{index.status.sourceCount}</output>
    {index.status.issues.map((issue) => <div key={`${issue.path}:${issue.message}`}>{issue.path}: {issue.message}</div>)}
    <span>{String(index.retainsModel("src/main.ts"))}:{String(index.retainsModel("notes.txt"))}</span>
  </>;
};

describe("workspace index request lifecycle", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    runtime.owners.length = 0;
    runtime.configurationRequests.mockResolvedValue([]);
    runtime.configurationDiagnostics.mockResolvedValue([]);
    runtime.addConfigurationFiles.mockResolvedValue(undefined);
    vi.mocked(readWorkspaceProjectIndex).mockResolvedValue(snapshot());
    useAppStore.setState({ workingDirectory: "/project", fileChanges: [], editorTabs: [] });
  });
  afterEach(() => cleanup());

  it("indexes dependencies once, refreshes source changes without rereading dependencies, and explicitly reloads on demand", async () => {
    render(<Fixture />);
    fireEvent.click(screen.getByText("初始化"));
    await screen.findByText("ready:1");
    expect(readWorkspaceProjectIndex).toHaveBeenLastCalledWith("/project", true, expect.any(AbortSignal));
    act(() => useAppStore.setState({ fileChanges: [{ path: "src/main.ts", event: "modified", timestamp: 1, workspaceRoot: "/project", sequence: 1 }] }));
    await waitFor(() => expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(2));
    expect(readWorkspaceProjectIndex).toHaveBeenLastCalledWith("/project", false, expect.any(AbortSignal));
    await screen.findByText("ready:1");
    fireEvent.click(screen.getByText("刷新"));
    await waitFor(() => expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(3));
    expect(readWorkspaceProjectIndex).toHaveBeenLastCalledWith("/project", true, expect.any(AbortSignal));
    expect(screen.getByText("true:false")).toBeTruthy();
  });

  it("loads actual JSONC extends candidates by URI and passes the exact URI back to both workers", async () => {
    runtime.configurationRequests.mockResolvedValueOnce(["file:///project/configs/base.json"]).mockResolvedValue([]);
    vi.mocked(readWorkspaceFile).mockResolvedValue({ path: "configs/base.json", content: '{"compilerOptions":{"strict":true}}', content_hash: "cfg" });
    render(<Fixture />);
    fireEvent.click(screen.getByText("初始化"));
    await screen.findByText("ready:1");
    expect(readWorkspaceFile).toHaveBeenCalledWith("/project/configs/base.json", "/project", expect.any(AbortSignal));
    expect(runtime.addConfigurationFiles).toHaveBeenCalledWith([{ filePath: "file:///project/configs/base.json", content: '{"compilerOptions":{"strict":true}}' }]);
  });

  it("treats a nonexistent native candidate as a missing candidate and reports actual config diagnostics", async () => {
    runtime.configurationRequests.mockResolvedValue(["file:///project/base.json"]);
    runtime.configurationDiagnostics.mockResolvedValue([{ path: "file:///project/tsconfig.json", message: "Cannot read base.json" }]);
    vi.mocked(readWorkspaceFile).mockRejectedValue(new ApiError(404, "Not found"));
    render(<Fixture />);
    fireEvent.click(screen.getByText("初始化"));
    await screen.findByText("partial:1");
    expect(readWorkspaceFile).toHaveBeenCalledTimes(1);
    expect(screen.getByText("/project/tsconfig.json: Cannot read base.json")).toBeTruthy();
  });

  it("exposes editor admission exclusions as partial indexing instead of reporting a complete index", async () => {
    render(<Fixture />);
    fireEvent.click(screen.getByText("初始化"));
    await screen.findByText("ready:1");
    runtime.owners[0].applySnapshot.mockResolvedValue([{ path: "src/generated.ts", message: "该文件包含 20,001 行，超过编辑器限制。" }]);
    fireEvent.click(screen.getByText("刷新"));
    await screen.findByText("partial:1");
    expect(screen.getByText("src/generated.ts: 该文件包含 20,001 行，超过编辑器限制。")).toBeTruthy();
  });

  it("keeps permission and server failures visible instead of treating them as absent configs", async () => {
    runtime.configurationRequests.mockResolvedValue(["file:///project/base.json"]);
    vi.mocked(readWorkspaceFile).mockRejectedValue(new ApiError(403, "Workspace access denied"));
    render(<Fixture />);
    fireEvent.click(screen.getByText("初始化"));
    await screen.findByText("error:1");
    expect(screen.getByText("/project: Workspace access denied")).toBeTruthy();
  });

  it("disposes the previous workspace and ignores its late response", async () => {
    let finish!: (value: ReturnType<typeof snapshot>) => void;
    vi.mocked(readWorkspaceProjectIndex).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const { rerender } = render(<Fixture />);
    fireEvent.click(screen.getByText("初始化"));
    await waitFor(() => expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(1));
    const owner = runtime.owners[0];
    act(() => useAppStore.setState({ workingDirectory: "/other", fileChanges: [] }));
    rerender(<Fixture root="/other" />);
    fireEvent.click(screen.getByText("初始化"));
    await screen.findByText("ready:1");
    await act(async () => finish(snapshot()));
    expect(owner.dispose).toHaveBeenCalledOnce();
    expect(owner.applySnapshot).not.toHaveBeenCalled();
    expect(readWorkspaceProjectIndex).toHaveBeenLastCalledWith("/other", true, expect.any(AbortSignal));
  });
});
