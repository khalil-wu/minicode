import { MessageList } from "./MessageList";
import { Composer } from "../composer/Composer";
import { InlineAgentPrompt } from "./InlineAgentPrompt";

export const ChatPane = () => {
  return (
    <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden", background: "var(--surface-base)", width: "100%" }}>
      <MessageList />
      <InlineAgentPrompt />
      <Composer />
    </div>
  );
};
