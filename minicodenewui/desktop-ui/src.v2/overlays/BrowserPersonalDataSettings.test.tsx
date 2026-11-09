/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { BrowserPersonalDataSettings } from "./BrowserPersonalDataSettings";
import type { BrowserContact, BrowserSavedPassword } from "../desktop/runtime";

const { records, api } = vi.hoisted(() => {
  const records = { passwords: [] as BrowserSavedPassword[], contacts: [] as BrowserContact[], available: true, passwordAutofillEnabled: true, contactAutofillEnabled: true };
  const listPasswords = vi.fn(async () => ({ available: records.available, passwordAutofillEnabled: records.passwordAutofillEnabled,
    contactAutofillEnabled: records.contactAutofillEnabled, items: records.passwords.map(({ id, origin, username }) => ({ id, origin, username })) }));
  const api = { listPasswords,
    listContacts: vi.fn(async () => records.contacts),
    getPassword: vi.fn(async ({ id }: { id: string }) => ({ ...records.passwords.find(item => item.id === id)! })),
    savePassword: vi.fn(async (item: BrowserSavedPassword) => { records.passwords = [...records.passwords.filter(saved => saved.id !== item.id), { ...item, id: item.id || "added" }]; }),
    removePassword: vi.fn(async ({ id }: { id: string }) => { records.passwords = records.passwords.filter(item => item.id !== id); }),
    saveContact: vi.fn(async (item: BrowserContact) => { records.contacts = [...records.contacts.filter(saved => saved.id !== item.id), { ...item, id: item.id || "added" }]; }),
    removeContact: vi.fn(async ({ id }: { id: string }) => { records.contacts = records.contacts.filter(item => item.id !== id); }),
    setAutofillSettings: vi.fn(async (patch: { passwordAutofillEnabled?: boolean; contactAutofillEnabled?: boolean }) => { Object.assign(records, patch); return listPasswords(); }),
  };
  return { records, api };
});
vi.mock("../desktop/runtime", () => ({ desktop: () => ({ browserPersonalData: api }) }));
vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));

beforeEach(() => {
  vi.clearAllMocks();
  records.passwords = []; records.contacts = [];
  records.available = true; records.passwordAutofillEnabled = true; records.contactAutofillEnabled = true;
});
afterEach(cleanup);
const breadcrumb = (current: string) => <nav>{current}</nav>;

it("keeps passwords out of the list and fetches the secret only after the owner chooses Edit", async () => {
  records.passwords = [{ id: "saved", origin: "https://example.test", username: "fixture-user", password: "fixture-secret" }];
  render(<BrowserPersonalDataSettings section="passwords" active breadcrumb={breadcrumb} />);
  const entry = await screen.findByRole("button", { name: "example.test fixture-user" });
  expect(api.getPassword).not.toHaveBeenCalled();
  expect(screen.queryByDisplayValue("fixture-secret")).toBeNull();
  fireEvent.click(entry);
  const dialog = await screen.findByRole("dialog", { name: "编辑密码" });
  expect(api.getPassword).toHaveBeenCalledWith({ id: "saved" });
  expect(dialog.querySelector('input[type="password"]')).toHaveProperty("value", "fixture-secret");
  fireEvent.change(screen.getByLabelText("密码"), { target: { value: "updated-secret" } });
  fireEvent.click(screen.getByRole("button", { name: "保存", exact: true }));
  await waitFor(() => expect(api.savePassword).toHaveBeenCalledWith({ id: "saved", origin: "https://example.test", username: "fixture-user", password: "updated-secret" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});

it("retains the owner's password draft and actual storage error after a failed save", async () => {
  api.savePassword.mockRejectedValueOnce(new Error("OS encryption failed"));
  render(<BrowserPersonalDataSettings section="passwords" active breadcrumb={breadcrumb} />);
  fireEvent.click(await screen.findByRole("button", { name: "添加", exact: true }));
  fireEvent.change(screen.getByLabelText("网站"), { target: { value: "https://example.test" } });
  fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "fixture-user" } });
  fireEvent.change(screen.getByLabelText("密码"), { target: { value: "fixture-secret" } });
  fireEvent.click(screen.getByRole("button", { name: "保存", exact: true }));
  expect((await screen.findByRole("alert")).textContent).toContain("OS encryption failed");
  expect(screen.getByLabelText("密码")).toHaveProperty("value", "fixture-secret");
  expect(screen.getByRole("dialog", { name: "添加密码" })).toBeTruthy();
});

it("contact edits save all actual address fields and the autofill switch uses native settings", async () => {
  records.contacts = [{ id: "contact", fullName: "Fixture Contact", organization: "", addressLine1: "123 Test St", addressLine2: "", city: "Test City", region: "", postalCode: "12345", country: "US", email: "contact@example.test", phone: "123456789" }];
  render(<BrowserPersonalDataSettings section="contacts" active breadcrumb={breadcrumb} />);
  fireEvent.click(await screen.findByRole("switch", { name: "保存并填写地址" }));
  await waitFor(() => expect(api.setAutofillSettings).toHaveBeenCalledWith({ contactAutofillEnabled: false }));
  await waitFor(() => expect(screen.getByRole("switch", { name: "保存并填写地址" }).getAttribute("aria-checked")).toBe("false"));
  fireEvent.click(await screen.findByRole("button", { name: "Fixture Contact 123 Test St · Test City · contact@example.test · 123456789", exact: true }));
  await screen.findByRole("dialog", { name: "编辑地址" });
  fireEvent.change(screen.getByLabelText("城市"), { target: { value: "Updated City" } });
  fireEvent.click(screen.getByRole("button", { name: "保存", exact: true }));
  await waitFor(() => expect(api.saveContact).toHaveBeenCalledWith({ ...records.contacts[0], city: "Updated City" }));
});

it("unavailable OS encryption exposes an explicit unavailable state and never reads contacts or invents records", async () => {
  records.available = false;
  render(<BrowserPersonalDataSettings section="contacts" active breadcrumb={breadcrumb} />);
  await screen.findByText("操作系统安全存储不可用，无法管理浏览器个人数据。");
  expect(api.listContacts).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "添加", exact: true })).toBeNull();
});
