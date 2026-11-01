/** Menu control glyphs shared by eager menus and lazy settings. */
export const menuControlGlyphs = {
  chevronDown: {
    viewBox: "0 0 20 21",
    paths: <>
      <path d="M15.2793 7.71101C15.539 7.45131 15.961 7.45131 16.2207 7.71101C16.4804 7.97071 16.4804 8.39272 16.2207 8.65242L10.4707 14.4024C10.211 14.6621 9.78902 14.6621 9.52932 14.4024L3.77932 8.65242L3.69436 8.54792C3.52385 8.28979 3.55205 7.93828 3.77932 7.71101C4.00659 7.48374 4.3581 7.45554 4.61623 7.62605L4.72073 7.71101L10 12.9903L15.2793 7.71101Z" fill="currentColor" stroke="currentColor" strokeWidth={0.6} />
    </>,
  },
  check: { // checkmark-lg-light-16
    viewBox: "0 0 16 16",
    paths: <>
      <path d="M12.7216 2.99666C12.8863 2.75824 13.2125 2.69837 13.4511 2.86287C13.6895 3.0274 13.75 3.35377 13.5858 3.59236L7.20011 12.8472C6.97922 13.1685 6.52057 13.2111 6.24405 12.9361L2.45987 9.18123C2.25451 8.97704 2.25305 8.64475 2.45694 8.43904C2.66115 8.23343 2.99433 8.23205 3.20011 8.43611L6.62296 11.8326L12.7216 2.99666Z" fill="currentColor" />
    </>,
  },
} as const;

export function MenuControlSourceIcon({ icon, size = 16 }: { icon: keyof typeof menuControlGlyphs; size?: number }) {
  const glyph = menuControlGlyphs[icon];
  return <svg width={size} height={size} viewBox={glyph.viewBox} fill="none" aria-hidden="true">{glyph.paths}</svg>;
}
