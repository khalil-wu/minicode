/** Glyphs from the current Codex desktop menu assets; extraction is recorded in output/. */
export const MenuSourceIcon = ({ kind }: { kind: "help" | "settings" | "shortcuts" | "guide" | "whats-new" | "submenu" }) => {
  const url = `url('${import.meta.env.BASE_URL}apps/menu-${kind}.svg')`;
  return <span aria-hidden="true" data-menu-source-icon={kind} style={{ display: "inline-block", width: 16, height: 16, flexShrink: 0,
    background: "currentColor", maskImage: url, WebkitMaskImage: url, maskPosition: "center", maskRepeat: "no-repeat", maskSize: "contain" }} />;
};
