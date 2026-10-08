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
  it("shares native source ownership between the editor and Problems until the last consumer leaves", async () => {
    const Pair = ({ second = true }) => <><Fixture key="editor" />{second && <Fixture key="problems" />}</>;
    const { rerender, unmount } = render(<Pair />);
    screen.getAllByText("初始化").forEach((button) => fireEvent.click(button));
    await waitFor(() => expect(screen.getAllByText("ready:1")).toHaveLength(2));
    expect(runtime.owners).toHaveLength(1);
    expect(readWorkspaceProjectIndex).toHaveBeenCalledOnce();
    const owner = runtime.owners[0];
    rerender(<Pair second={false} />);
    expect(owner.dispose).not.toHaveBeenCalled();
    unmount();
    expect(owner.dispose).toHaveBeenCalledOnce();
  });
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

  it("shares one pending request and coalesces a hundred file notifications into one current-source refresh", async () => {
    let finishFirst!: (value: ReturnType<typeof snapshot>) => void;
    let finishSecond!: (value: ReturnType<typeof snapshot>) => void;
    vi.mocked(readWorkspaceProjectIndex)
      .mockImplementationOnce(() => new Promise((resolve) => { finishFirst = resolve; }))
      .mockImplementationOnce(() => new Promise((resolve) => { finishSecond = resolve; }));
    render(<><Fixture /><Fixture /></>);
    screen.getAllByText("初始化").forEach((button) => fireEvent.click(button));
    await waitFor(() => expect(readWorkspaceProjectIndex).toHaveBeenCalledOnce());
    const signal = vi.mocked(readWorkspaceProjectIndex).mock.calls[0][2]!;
    act(() => {
      for (let sequence = 1; sequence <= 100; sequence++) useAppStore.setState((state) => ({ fileChanges: [...state.fileChanges,
        { path: `src/source${sequence}.ts`, event: "modified", timestamp: sequence, workspaceRoot: "/project", sequence }] }));
    });
    expect(readWorkspaceProjectIndex).toHaveBeenCalledOnce();
    expect(signal.aborted).toBe(false);
    await act(async () => finishFirst(snapshot()));
    expect(screen.getAllByText("ready:1")).toHaveLength(2);
    await waitFor(() => expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(2));
    expect(readWorkspaceProjectIndex).toHaveBeenLastCalledWith("/project", false, signal);
    expect(runtime.owners[0].noteFileChanges.mock.calls.at(-1)![0]).toHaveLength(100);
    await act(async () => finishSecond(snapshot()));
    expect(screen.getAllByText("ready:1")).toHaveLength(2);
    expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(2);
    expect(runtime.owners[0].applySnapshot).toHaveBeenCalledTimes(2);
  });

  it("merges refreshes from both consumers while retaining dependency refresh requested during a source read", async () => {
    render(<><Fixture /><Fixture /></>);
    screen.getAllByText("初始化").forEach((button) => fireEvent.click(button));
    await waitFor(() => expect(screen.getAllByText("ready:1")).toHaveLength(2));
    let finish!: (value: ReturnType<typeof snapshot>) => void;
    vi.mocked(readWorkspaceProjectIndex).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    act(() => useAppStore.setState({ fileChanges: [{ path: "src/main.ts", event: "modified", timestamp: 1, workspaceRoot: "/project", sequence: 1 }] }));
    await waitFor(() => expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(2));
    const signal = vi.mocked(readWorkspaceProjectIndex).mock.calls[1][2]!;
    screen.getAllByText("刷新").forEach((button) => { fireEvent.click(button); fireEvent.click(button); });
    expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(2);
    expect(signal.aborted).toBe(false);
    await act(async () => finish(snapshot()));
    expect(screen.getAllByText("ready:1")).toHaveLength(2);
    await waitFor(() => expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(3));
    expect(readWorkspaceProjectIndex).toHaveBeenLastCalledWith("/project", true, signal);
    await waitFor(() => expect(screen.getAllByText("ready:1")).toHaveLength(2));
    expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(3);
  });

  it("keeps the latest real sequence per path while deletion and rename notifications outlive the global history", async () => {
    let finish!: (value: ReturnType<typeof snapshot>) => void;
    vi.mocked(readWorkspaceProjectIndex).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    render(<Fixture />);
    fireEvent.click(screen.getByText("初始化"));
    await waitFor(() => expect(readWorkspaceProjectIndex).toHaveBeenCalledOnce());
    act(() => {
      useAppStore.getState().addFileChange({ path: "src/old.ts", event: "deleted", timestamp: 1 });
      useAppStore.getState().addFileChange({ path: "src/new.ts", event: "moved", timestamp: 2 });
      for (let timestamp = 3; timestamp <= 202; timestamp++) useAppStore.getState().addFileChange({ path: "src/main.ts", event: "modified", timestamp });
    });
    expect(useAppStore.getState().fileChanges).toHaveLength(100);
    await act(async () => finish(snapshot()));
    await waitFor(() => expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(2));
    expect(runtime.owners[0].noteFileChanges.mock.calls.at(-1)![0]).toEqual([
      expect.objectContaining({ path: "src/old.ts", event: "deleted", sequence: 1 }),
      expect.objectContaining({ path: "src/new.ts", event: "moved", sequence: 2 }),
      expect.objectContaining({ path: "src/main.ts", event: "modified", sequence: 202 }),
    ]);
    await screen.findByText("ready:1");
    expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(2);
  });

  it("keeps a shared pending read alive when one consumer leaves and aborts it when the last owner leaves", async () => {
    vi.mocked(readWorkspaceProjectIndex).mockImplementationOnce(() => new Promise(() => {}));
    const Pair = ({ second = true }) => <><Fixture key="editor" />{second && <Fixture key="problems" />}</>;
    const { rerender, unmount } = render(<Pair />);
    screen.getAllByText("初始化").forEach((button) => fireEvent.click(button));
    await waitFor(() => expect(readWorkspaceProjectIndex).toHaveBeenCalledOnce());
    const signal = vi.mocked(readWorkspaceProjectIndex).mock.calls[0][2]!;
    rerender(<Pair second={false} />);
    expect(signal.aborted).toBe(false);
    expect(runtime.owners[0].dispose).not.toHaveBeenCalled();
    unmount();
    expect(signal.aborted).toBe(true);
    expect(runtime.owners[0].dispose).toHaveBeenCalledOnce();
  });

  it("publishes a failed source read to both consumers without automatically retrying it", async () => {
    vi.mocked(readWorkspaceProjectIndex).mockRejectedValueOnce(new Error("Project scan failed"));
    render(<><Fixture /><Fixture /></>);
    screen.getAllByText("初始化").forEach((button) => fireEvent.click(button));
    await waitFor(() => expect(screen.getAllByText("error:0")).toHaveLength(2));
    expect(screen.getAllByText("/project: Project scan failed")).toHaveLength(2);
    await new Promise((resolve) => window.setTimeout(resolve, 250));
    expect(readWorkspaceProjectIndex).toHaveBeenCalledOnce();
  });

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
    const signal = vi.mocked(readWorkspaceProjectIndex).mock.calls[0][2]!;
    act(() => useAppStore.setState({ fileChanges: [{ path: "src/main.ts", event: "modified", timestamp: 1, workspaceRoot: "/project", sequence: 1 }] }));
    act(() => useAppStore.setState({ workingDirectory: "/other", fileChanges: [] }));
    rerender(<Fixture root="/other" />);
    expect(signal.aborted).toBe(true);
    fireEvent.click(screen.getByText("初始化"));
    await screen.findByText("ready:1");
    await act(async () => finish(snapshot()));
    expect(owner.dispose).toHaveBeenCalledOnce();
    expect(owner.applySnapshot).not.toHaveBeenCalled();
    expect(readWorkspaceProjectIndex).toHaveBeenLastCalledWith("/other", true, expect.any(AbortSignal));
    expect(readWorkspaceProjectIndex).toHaveBeenCalledTimes(2);
  });
});
