import { beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "../stores";
import { __resetOpenWebInBrowserForTests, openWebInBrowser, isPreviewableHttpUrl, subscribeBrowserRequests } from "./openWebInBrowser";

beforeEach(() => { __resetOpenWebInBrowserForTests(); useAppStore.setState({ conversationId: "owner" }); });

describe("actual browser request input owner", () => {
  it("never queues a credential-bearing model link", () => {
    const requests: unknown[] = [];
    const unsubscribe = subscribeBrowserRequests((request) => requests.push(request));
    expect(isPreviewableHttpUrl("https://user:password@example.test")).toBe(false);
    expect(openWebInBrowser("https://user:password@example.test")).toBe(false);
    expect(requests).toEqual([]);
    unsubscribe();
  });

  it("retains distinct conversation owners when opening the same URL", () => {
    const requests: Array<{ conversationId: string }> = [];
    const unsubscribe = subscribeBrowserRequests((request) => requests.push(request));
    expect(openWebInBrowser("https://example.test")).toBe(true);
    useAppStore.setState({ conversationId: "second" });
    expect(openWebInBrowser("https://example.test")).toBe(true);
    expect(requests.map((request) => request.conversationId)).toEqual(["owner", "second"]);
    unsubscribe();
  });
});
