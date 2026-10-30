import { Box, Folder, Globe, MessageCircle } from "../lib/icons";
import { BrandIcon } from "../components/BrandIcon";
import { fileIcon } from "../lib/file-icons";
import { pluginAssetResourceUrlWithToken, skillAssetResourceUrlWithToken } from "../protocol/api";
import { conversationIdFromReference, type InlineComposerReference } from "./composer-references";

export function ComposerReferenceIcon({ reference, workspaceRoot, size = 16 }: { reference: InlineComposerReference; workspaceRoot: string; size?: number }) {
  if (reference.kind === "file") return fileIcon(reference.name, { size, className: "composer-context-icon-svg" });
  if (reference.kind === "folder") return <Folder size={size} aria-hidden="true" />;
  if (reference.kind === "url") return conversationIdFromReference(reference.path) ? <MessageCircle size={size} aria-hidden="true" /> : <Globe size={size} aria-hidden="true" />;
  if (reference.kind === "skill") return <BrandIcon value={reference.name}
    iconUrl={reference.path && reference.iconVariant ? skillAssetResourceUrlWithToken(reference.path, reference.iconVariant, undefined, workspaceRoot) : reference.icon}
    inferBrand={false} fallback="skill" size={size} fallbackIcon={<Box size={size} />} />;
  if (reference.kind === "plugin") {
    const iconUrl = reference.pluginPath && reference.iconVariant
      ? pluginAssetResourceUrlWithToken(reference.pluginPath, reference.iconVariant, undefined, workspaceRoot) : reference.iconUrl;
    return <BrandIcon value={reference.name} iconUrl={iconUrl} inferBrand={false} fallback="plugin" size={size} />;
  }
}
