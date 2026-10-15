/** Exact installed Source ha/xIe and radio check assets; extraction is audited in output/. */
export const SidebarSortSourceIcon = ({ kind }: { kind: "options" | "sort" | "sort-check" }) => {
  const size = kind === "sort-check" ? 17 : 16;
  const url = `url('${import.meta.env.BASE_URL}apps/sidebar-${kind}.svg')`;
  return <span aria-hidden="true" data-sidebar-source-icon={kind} style={{ display: "inline-block", width: size, height: size, flexShrink: 0,
    background: "currentColor", maskImage: url, WebkitMaskImage: url, maskPosition: "center", maskRepeat: "no-repeat", maskSize: "contain" }} />;
};
