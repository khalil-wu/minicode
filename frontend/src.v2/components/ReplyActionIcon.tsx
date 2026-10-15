import { replyActionIcons } from "../lib/reply-action-icons";

export function ReplyActionIcon({ name, size = 14 }: { name: keyof typeof replyActionIcons; size?: number }) {
  const icon = replyActionIcons[name];
  return <svg width={size} height={size} viewBox={icon.viewBox} fill="none" xmlns="http://www.w3.org/2000/svg"
    aria-hidden="true" data-reply-action-icon={name}><path d={icon.path} fill="currentColor" /></svg>;
}
