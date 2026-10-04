import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { loadRevealMessage, revealConversationMessage } from "./revealConversationMessage";
import { loadEarlierConversationMessages } from "./historyPagination";

vi.mock("./historyPagination", () => ({ loadEarlierConversationMessages: vi.fn() }));
const target = { conversationId: "owner", messageId: "old-file-producer", requestId: "reveal-1" };
beforeEach(() => {
  vi.mocked(loadEarlierConversationMessages).mockReset();
  useAppStore.setState({ conversationId: "owner", messages: [], messageRevealTarget: target,
    conversationHistoryPages: { owner: { beforeMessageId: "page-3", hasMore: true, loading: false } } });
});

describe("return to producing message", () => {
  it("loads actual history pages until the requested message appears", async () => {
    vi.mocked(loadEarlierConversationMessages).mockImplementation(async () => {
      const page = useAppStore.getState().conversationHistoryPages.owner;
      useAppStore.setState({ conversationHistoryPages: { owner: { ...page, beforeMessageId: page.beforeMessageId === "page-3" ? "page-2" : "page-1", hasMore: false } },
        messages: [{ id: target.messageId, role: "assistant", content: "produced file", timestamp: 1, artifacts: [] }] });
    });
    expect(await loadRevealMessage(target)).toBe("found");
    expect(loadEarlierConversationMessages).toHaveBeenCalledWith("owner");
  });

  it("reports unavailable and failed history without retrying a stationary cursor", async () => {
    vi.mocked(loadEarlierConversationMessages).mockResolvedValue(undefined);
    expect(await loadRevealMessage(target)).toBe("failed");
    expect(loadEarlierConversationMessages).toHaveBeenCalledTimes(1);
    useAppStore.setState({ conversationHistoryPages: { owner: { beforeMessageId: "end", hasMore: false, loading: false } } });
    expect(await loadRevealMessage(target)).toBe("missing");
  });

  it("cancels when a newer user navigation replaces the destination", async () => {
    vi.mocked(loadEarlierConversationMessages).mockImplementation(async () => { useAppStore.setState({ messageRevealTarget: null }); });
    expect(await loadRevealMessage(target)).toBe("cancelled");
  });

  it("does not accept a cloned message from a different conversation after loading", async () => {
    vi.mocked(loadEarlierConversationMessages).mockImplementation(async () => {
      useAppStore.setState({ conversationId: "clone", messages: [{ id: target.messageId, role: "assistant", content: "copied", timestamp: 1, artifacts: [] }] });
    });
    expect(await loadRevealMessage(target)).toBe("cancelled");
  });

  it("requests the resource owner before showing its message instead of switching locally", () => {
    const request = vi.fn();
    const original = useAppStore.getState().requestConversationSwitch;
    useAppStore.setState({ conversationId: "other", requestConversationSwitch: request });
    revealConversationMessage("owner", "producer");
    expect(request).toHaveBeenCalledWith("owner");
    expect(useAppStore.getState().messageRevealTarget).toMatchObject({ conversationId: "owner", messageId: "producer" });
    expect(useAppStore.getState().conversationId).toBe("other");
    useAppStore.setState({ requestConversationSwitch: original });
  });
});
