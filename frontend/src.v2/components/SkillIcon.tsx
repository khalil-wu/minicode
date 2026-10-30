import { useAppStore } from "../stores";
import { BrandIcon } from "./BrandIcon";

export function SkillIcon({ value, iconUrl, iconAsset, size = 24 }: { value: string; iconUrl?: string; iconAsset?: string; size?: number }) {
  const theme = useAppStore(state => state.resolvedTheme);
  const fallback = new URL(`apps/skill-default-${theme === "dark" ? "dark" : "light"}.svg`, document.baseURI).href;
  if (iconAsset) return <img src={new URL(iconAsset, document.baseURI).href} alt="" width={size} height={size} />;
  return <BrandIcon value={value} iconUrl={iconUrl} inferBrand={false} size={size}
    fallbackIcon={<img src={fallback} alt="" width={size} height={size} />} />;
}
