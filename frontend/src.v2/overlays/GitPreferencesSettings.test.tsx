/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { GitPreferencesSettings } from "./GitPreferencesSettings";

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it("saves prefix on blur or Ctrl+S and autosaves instruction edits after three seconds", async () => {
  let preferences = { branch_prefix: "codex/", always_force_push: false, create_draft_pull_requests: true,
    pull_request_merge_method: "merge", commit_instructions: "", pull_request_instructions: "", review_delivery: "inline" };
  const fetchMock = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "PUT") preferences = { ...preferences, ...JSON.parse(String(init.body)) };
    return new Response(JSON.stringify(preferences));
  });
  vi.stubGlobal("fetch", fetchMock);
  const { rerender } = render(<GitPreferencesSettings active={false} />);
  expect(fetchMock).not.toHaveBeenCalled();
  rerender(<GitPreferencesSettings active />);
  const prefix = await screen.findByRole("textbox", { name: "分支前缀" });
  await waitFor(() => expect((prefix as HTMLInputElement).value).toBe("codex/"));
  fireEvent.change(prefix, { target: { value: "feature/" } });
  fireEvent.blur(prefix);
  await waitFor(() => expect(preferences.branch_prefix).toBe("feature/"));
  await waitFor(() => expect((prefix as HTMLInputElement).disabled).toBe(false));
  fireEvent.change(prefix, { target: { value: "work/" } });
  fireEvent.keyDown(prefix, { key: "s", ctrlKey: true });
  await waitFor(() => expect(preferences.branch_prefix).toBe("work/"));
  await waitFor(() => expect((prefix as HTMLInputElement).disabled).toBe(false));
  vi.useFakeTimers();
  fireEvent.change(screen.getByRole("textbox", { name: "提交指令" }), { target: { value: "Use imperative Chinese" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Pull Request 指令" }), { target: { value: "Describe validation" } });
  await act(async () => { vi.advanceTimersByTime(2999); });
  expect(preferences.commit_instructions).toBe("");
  expect(preferences.pull_request_instructions).toBe("");
  await act(async () => { vi.advanceTimersByTime(1); });
  expect(preferences.commit_instructions).toBe("Use imperative Chinese");
  expect(preferences.pull_request_instructions).toBe("Describe validation");
  expect(fetchMock.mock.calls.filter(([, options]) => options?.method === "PUT")).toHaveLength(4);
});

it("keeps an acknowledged choice when an earlier settings read returns late", async () => {
  const preferences = { branch_prefix: "codex/", always_force_push: false, create_draft_pull_requests: true,
    pull_request_merge_method: "merge", commit_instructions: "", pull_request_instructions: "", review_delivery: "inline" };
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(preferences)));
  vi.stubGlobal("fetch", fetchMock);
  const { rerender } = render(<GitPreferencesSettings />);
  await waitFor(() => expect(screen.getByRole("button", { name: "合并", exact: true }).getAttribute("aria-pressed")).toBe("true"));
  rerender(<GitPreferencesSettings active={false} />);
  let completeRead!: (response: Response) => void;
  fetchMock.mockImplementationOnce(() => new Promise(resolve => { completeRead = resolve; }));
  rerender(<GitPreferencesSettings active />);
  await waitFor(() => expect(completeRead).toBeDefined());
  fireEvent.click(screen.getByRole("button", { name: "压缩合并", exact: true }));
  await waitFor(() => expect(screen.getByRole("button", { name: "压缩合并", exact: true }).getAttribute("aria-pressed")).toBe("true"));
  await act(async () => { completeRead(new Response(JSON.stringify(preferences))); });
  expect(screen.getByRole("button", { name: "压缩合并", exact: true }).getAttribute("aria-pressed")).toBe("true");
});
