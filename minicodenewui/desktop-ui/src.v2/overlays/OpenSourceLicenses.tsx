import { useEffect, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { desktop } from "../desktop/runtime";
import { useI18n } from "../i18n";
import "./GeneralTab.css";

export const OpenSourceLicenses = ({ onBack }: { onBack: () => void }) => {
  const { t } = useI18n();
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    void desktop()!.preferences.getOpenSourceLicenses().then(value => { if (active) setText(value); })
      .catch(reason => { if (active) setError(reason instanceof Error ? reason.message : String(reason)); });
    return () => { active = false; };
  }, []);
  return <section className="settings-open-source-licenses">
    <button type="button" className="settings-open-source-back" onClick={onBack}><ArrowLeft size={15} aria-hidden="true" />{t("常规")}</button>
    <header className="settings-page-heading"><h2 id="settings-page-title">{t("开源许可证")}</h2></header>
    {error ? <p role="alert">{error}</p> : text === null ? <p role="status">{t("正在读取许可证…")}</p>
      : <pre className="settings-open-source-notices">{text}</pre>}
  </section>;
};
