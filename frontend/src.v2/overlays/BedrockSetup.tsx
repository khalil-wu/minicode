import { useEffect, useId, useRef, useState } from "react";
import { ModelBrandIcon } from "../components/ModelBrandIcon";
import { SelectMenu } from "../components/SelectMenu";
import { useI18n } from "../i18n";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { openExternal } from "../desktop/runtime";
import { LoaderCircle } from "../lib/icons";
import "./BedrockSetup.css";

type Profile = { name: string; region: string | null };
type Discovery = { profiles: Profile[]; environmentCredentials: Array<{ type: "accessKeys" | "bedrockApiKey"; region: string | null }> };
type DetectedCredentials = { profiles: Profile[]; activeProfile: string | null; environmentCredentialsAvailable: boolean;
  environmentRegion: string | null; region: string | null };
export type BedrockGuidance = "warning" | "unverified" | null;
export type BedrockSignIn =
  | { method: "profile"; profile: string; region: string }
  | { method: "environment"; region: string }
  | { method: "accessKeys"; accessKeyId: string; secretAccessKey: string; sessionToken: string | null; region: string }
  | { method: "apiKey"; apiKey: string; region: string };

export function BedrockSetup({ onBack, onSubmit, onComplete, onCheckGovCloud }: {
  onBack: () => void; onSubmit: (input: BedrockSignIn) => Promise<BedrockGuidance>;
  onComplete: () => void; onCheckGovCloud: () => Promise<BedrockGuidance>;
}) {
  const { t } = useI18n();
  const [detected, setDetected] = useState<DetectedCredentials | null>(null);
  const [detecting, setDetecting] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [method, setMethod] = useState<BedrockSignIn["method"] | null>(null);
  const [otherMethods, setOtherMethods] = useState(false);
  const [profile, setProfile] = useState("");
  const [region, setRegion] = useState("");
  const [accessKeyId, setAccessKeyId] = useState("");
  const [secretAccessKey, setSecretAccessKey] = useState("");
  const [sessionToken, setSessionToken] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [guidance, setGuidance] = useState<BedrockGuidance>(null);
  const [checkingGuidance, setCheckingGuidance] = useState(false);
  const discoveryController = useRef<AbortController | null>(null);
  const profileId = useId();
  const regionId = useId();
  const keyId = useId();
  const secretId = useId();
  const tokenId = useId();
  const selectedName = detected?.activeProfile ?? "default";
  const profiles = detected?.profiles.slice().sort((left, right) => Number(right.name === selectedName) - Number(left.name === selectedName)) ?? [];
  const selected = profiles.find(item => item.name === profile) ?? profiles[0];
  const hasDetectedCredentials = profiles.length > 0 || detected?.environmentCredentialsAvailable === true;

  const discover = async () => {
    discoveryController.current?.abort();
    const controller = new AbortController();
    discoveryController.current = controller;
    setDetecting(true); setError(null);
    try {
      const response = await fetchWithTimeout(`${apiBase()}/api/llm/bedrock/discover`, { headers: authHeaders(), signal: controller.signal });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      const result = await response.json() as Discovery;
      if (controller.signal.aborted) return;
      const first = result.profiles[0];
      const environmentRegion = result.environmentCredentials.find(item => item.region != null)?.region ?? null;
      setDetected({ profiles: result.profiles, activeProfile: first?.name ?? null,
        environmentCredentialsAvailable: result.environmentCredentials.length > 0, environmentRegion,
        region: first?.region ?? environmentRegion });
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      if (!controller.signal.aborted) setDetecting(false);
    }
  };
  useEffect(() => { void discover(); return () => discoveryController.current?.abort(); }, []);

  const choose = (next: BedrockSignIn["method"] | null) => { setError(null); setMethod(next); };
  const submit = async (input: BedrockSignIn) => {
    setSubmitting(true); setError(null);
    try {
      const result = await onSubmit(input);
      if (result) setGuidance(result);
      else onComplete();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally { setSubmitting(false); }
  };
  const submitForm = () => {
    switch (method) {
      case "profile": return submit({ method, profile: profile.trim(), region: region.trim() });
      case "environment": return submit({ method, region: region.trim() });
      case "accessKeys": return submit({ method, accessKeyId, secretAccessKey, sessionToken: sessionToken.trim() ? sessionToken : null, region: region.trim() });
      case "apiKey": return submit({ method, apiKey, region: region.trim() });
    }
  };
  const continueDisabled = submitting || !region.trim() || (method === "profile" && !profile.trim())
    || (method === "accessKeys" && (!accessKeyId.trim() || !secretAccessKey.trim())) || (method === "apiKey" && !apiKey.trim());

  if (guidance) return <div className="bedrock-setup-page"><section className="bedrock-setup-content" aria-label={t("AWS GovCloud 指引")}>
    <h3>{guidance === "unverified" ? t("AWS GovCloud 指引") : t("在 AWS GovCloud 中使用 MiniCode")}</h3>
    <p>{guidance === "unverified" ? t("无法完成 AWS GovCloud 检查。如果你使用 AWS GovCloud，请在继续前阅读应用配置和安全指引。")
      : t("你正在通过 AWS GovCloud 使用 MiniCode。请在继续前阅读应用配置和安全指引。")}{" "}
      <a href="https://learn.chatgpt.com/docs/enterprise/govcloud-configuration" onClick={event => {
        event.preventDefault(); void openExternal(event.currentTarget.href);
      }}>{t("应用配置和安全指引")}</a></p>
    {guidance === "unverified" && <button type="button" className="bedrock-setup-button" disabled={checkingGuidance} onClick={() => {
      setCheckingGuidance(true);
      void onCheckGovCloud().then(result => { if (result) setGuidance(result); else onComplete(); }).finally(() => setCheckingGuidance(false));
    }}>{checkingGuidance && <LoaderCircle size={16} className="animate-spin" />}{t("再次检查")}</button>}
    <button type="button" className="bedrock-setup-button" data-primary="true" disabled={checkingGuidance} onClick={onComplete}>{t("知悉")}</button>
  </section></div>;

  return <div className="bedrock-setup-page"><section className="bedrock-setup-content" aria-label={t("设置 Amazon Bedrock")}>
    <header className="bedrock-setup-heading"><ModelBrandIcon entity="provider" provider="Amazon Bedrock" size={40} /><h3>{t("设置 Amazon Bedrock")}</h3></header>
    <div className="bedrock-setup-actions">
      {error && <p role="alert">{error}</p>}
      {detecting && <p role="status">{t("正在检查 AWS 凭据…")}</p>}
      {!detecting && method != null && <form onChange={() => setError(null)} onSubmit={event => { event.preventDefault(); void submitForm(); }}>
        {method === "profile" && <label htmlFor={profileId}>{t("AWS 配置文件")}<input id={profileId} autoFocus value={profile} onChange={event => setProfile(event.target.value)} required disabled={submitting} autoComplete="off" spellCheck={false} /></label>}
        {method === "accessKeys" && <>
          <label htmlFor={keyId}>{t("AWS 访问密钥 ID")}<input id={keyId} autoFocus value={accessKeyId} onChange={event => setAccessKeyId(event.target.value)} required disabled={submitting} autoComplete="off" spellCheck={false} /></label>
          <label htmlFor={secretId}>{t("AWS 私密访问密钥")}<input id={secretId} type="password" value={secretAccessKey} onChange={event => setSecretAccessKey(event.target.value)} required disabled={submitting} autoComplete="off" spellCheck={false} /></label>
          <label htmlFor={tokenId}>{t("AWS 会话令牌（可选）")}<input id={tokenId} type="password" value={sessionToken} onChange={event => setSessionToken(event.target.value)} disabled={submitting} autoComplete="off" spellCheck={false} /></label>
        </>}
        {method === "environment" && <p>{t("已检测到 AWS 凭据。请输入 AWS 区域以继续。")}</p>}
        {method === "apiKey" && <label htmlFor={keyId}>{t("Bedrock API 密钥")}<input id={keyId} autoFocus type="password" value={apiKey} onChange={event => setApiKey(event.target.value)} required disabled={submitting} autoComplete="off" spellCheck={false} /></label>}
        <div className="bedrock-setup-field"><label htmlFor={regionId}>{t("AWS 区域")}<input id={regionId} aria-describedby={`${regionId}-help`} autoFocus={method === "environment"} value={region} onChange={event => setRegion(event.target.value)} required disabled={submitting} autoComplete="off" spellCheck={false} /></label><small id={`${regionId}-help`}>{t("例如 us-east-2")}</small></div>
        <div className="bedrock-setup-actions">
          <button type="submit" className="bedrock-setup-button" data-primary="true" disabled={continueDisabled}>{submitting && <LoaderCircle size={16} className="animate-spin" />}{t("继续")}</button>
          {method === "environment" && <button type="button" className="bedrock-setup-button" disabled={submitting} onClick={() => { choose(null); setOtherMethods(false); void discover(); }}>{t("再次检查凭据")}</button>}
          <button type="button" className="bedrock-setup-button" disabled={submitting} onClick={() => { choose(null); setOtherMethods(hasDetectedCredentials); }}>{t("选择其他登录方式")}</button>
        </div>
      </form>}
      {!detecting && method == null && hasDetectedCredentials && !otherMethods && <>
        {selected && <>
          <label>{t("AWS 配置文件")}</label>
          <SelectMenu ariaLabel={t("AWS 配置文件")} value={selected.name} disabled={submitting} onValueChange={value => { setError(null); setProfile(value); }}
            optionDescriptions={Object.fromEntries(profiles.flatMap(item => item.region ? [[item.name, t("区域：{region}", { region: item.region })]] : []))}>
            {profiles.map(item => <option value={item.name} key={item.name}>{item.name}</option>)}
          </SelectMenu>
          <button type="button" className="bedrock-setup-button" data-primary="true" disabled={submitting} onClick={() => {
            if (selected.region == null) { setProfile(selected.name); setRegion(""); choose("profile"); }
            else void submit({ method: "profile", profile: selected.name, region: selected.region });
          }}>{submitting && <LoaderCircle size={16} className="animate-spin" />}{t("继续")}</button>
        </>}
        {detected?.environmentCredentialsAvailable && <button type="button" className="bedrock-setup-button" data-primary={selected ? undefined : "true"} disabled={submitting} onClick={() => { setRegion(detected.environmentRegion ?? ""); choose("environment"); }}>{t("使用检测到的凭据继续")}</button>}
        <button type="button" className="bedrock-setup-button" disabled={submitting} onClick={() => { setError(null); setOtherMethods(true); }}>{t("其他 AWS 登录方式")}</button>
      </>}
      {!detecting && method == null && (!hasDetectedCredentials || otherMethods) && <>
        {detected && !error && !hasDetectedCredentials && <p>{t("未找到 AWS 凭据")}</p>}
        <button type="button" className="bedrock-setup-button" disabled={submitting} onClick={() => { setProfile(detected?.activeProfile ?? ""); setRegion(profiles.find(item => item.name === detected?.activeProfile)?.region ?? ""); choose("profile"); }}>{t("AWS 配置文件")}</button>
        <button type="button" className="bedrock-setup-button" disabled={submitting} onClick={() => { setRegion(detected?.region ?? ""); choose("accessKeys"); }}>{t("AWS 访问密钥")}</button>
        <button type="button" className="bedrock-setup-button" disabled={submitting} onClick={() => { setRegion(detected?.region ?? ""); choose("apiKey"); }}>{t("Bedrock API 密钥")}</button>
      </>}
      {method == null && <button type="button" className="bedrock-setup-button" disabled={submitting} onClick={() => {
        setError(null); if (otherMethods && hasDetectedCredentials) setOtherMethods(false); else onBack();
      }}>{t("返回")}</button>}
    </div>
  </section></div>;
}
