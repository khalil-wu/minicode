import { describe, expect, it } from "vitest";
import { collectConversationSources } from "./conversationSources";
import { buildActivitySidebarState } from "../shell/activitySidebarState";
import type { ChatMessage } from "../stores/types";

describe("shared conversation sources", () => {
  it.each([
    ["failed", "ok"], ["blocked", "ok"], ["timeout", "ok"], ["cancelled", "ok"], ["success", "failed"],
  ] as const)("does not promote %s/%s Fetch outcomes into successful sources", (status, extractionStatus) => {
    const message: ChatMessage = {
      id: "failed-fetch", role: "assistant", timestamp: 1, content: "No usable job data", artifacts: [],
      blocks: [{ type: "tool_call", record: {
        id: "fetch", name: "web_fetch", args: { url: "https://example.test/jobs" }, status,
        extractionStatus, evidenceType: "fetched",
      } }],
    };
    expect(collectConversationSources([message])).toEqual([]);
  });

  it("retains the limitation when partial page content has an explicit prose link", () => {
    const message: ChatMessage = {
      id: "partial-fetch", role: "assistant", timestamp: 1, artifacts: [], content: "[Jobs](https://example.test/jobs)",
      blocks: [{ type: "tool_call", record: {
        id: "fetch", name: "web_fetch", args: { url: "https://example.test/jobs" }, status: "partial",
        extractionStatus: "partial", evidenceType: "fetched",
      } }],
    };
    expect(collectConversationSources([message])).toEqual([{
      id: "source:https://example.test/jobs", url: "https://example.test/jobs", label: "Jobs",
      detail: "example.test · 内容不完整", messageId: "partial-fetch",
    }]);
  });
  it("keeps literal code links and numeric examples out of both source views", () => {
    const message: ChatMessage = {
      id: "examples", role: "assistant", timestamp: 1, artifacts: [],
      content: "`[1]`\n\n```md\n[Fake](https://example.test/fake)\n```\n\n[**Real article**](https://example.test/article_(one)) and [Reference][ref].\n\n[ref]: https://example.test/reference",
      citations: [{ source: "https://example.test/uncited", range: [0, 0] }],
    };
    const sources = collectConversationSources([message]);
    expect(sources.map((source) => source.url)).toEqual(["https://example.test/reference", "https://example.test/article_(one)"]);
    expect(sources.find((source) => source.url?.includes("article"))?.label).toBe("Real article");
  });

  it("uses the first reference definition and actual prose link order", () => {
    const sources = collectConversationSources([{
      id: "references", role: "assistant", timestamp: 1, artifacts: [],
      content: "[First][ref] before [Second](https://example.test/second).\n\n[ref]: https://example.test/first\n[ref]: https://example.test/ignored",
    }]);
    expect(sources.map((source) => source.url)).toEqual(["https://example.test/second", "https://example.test/first"]);
  });
  it("retains every source and uses readable article labels for repeated domains in both views", () => {
    const messages: ChatMessage[] = Array.from({ length: 12 }, (_, index) => ({
      id: `answer-${index}`, role: "assistant", timestamp: index + 1, artifacts: [],
      content: `[文章 ${index}](https://news.example/articles/${index})`,
      citations: [{ source: `https://news.example/articles/${index}`, label: "news.example", range: [0, 0] }],
    }));
    messages.push({ ...messages[0], id: "same-url", content: "补充资料 [1]", citations: [{ source: "https://news.example/articles/0", title: "完整文章标题", range: [0, 0] }] });
    const sources = collectConversationSources(messages);
    expect(sources).toHaveLength(12);
    expect(sources.find((source) => source.url?.endsWith("/0"))).toMatchObject({ label: "完整文章标题", detail: "news.example", messageId: "same-url" });
    expect(sources.find((source) => source.url?.endsWith("/8"))?.label).toBe("文章 8");
    const sidebar = buildActivitySidebarState({ conversationId: "owner", isStreaming: false, messages, todos: [], plan: null, agentProgress: [], livePreviewUrl: null, previewArtifact: null, previewVerification: null, previewServers: [], previewLaunchProcesses: [] });
    expect(sidebar.sources.map((source) => source.label)).toEqual(sources.map((source) => source.label));
  });
});
