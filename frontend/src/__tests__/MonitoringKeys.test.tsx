import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { AxiosError } from "axios";
import { beforeEach, describe, expect, it, vi } from "vitest";
import MonitoringKeys from "@/components/Settings/MonitoringKeys";

vi.mock("@/api/endpoints", () => ({
  guardianAPI: { accounts: vi.fn(), account: vi.fn(), addAccount: vi.fn(), replaceKey: vi.fn(), updateAccount: vi.fn(), disable: vi.fn(), enable: vi.fn(), remove: vi.fn() },
}));
import { guardianAPI } from "@/api/endpoints";

/** What axios hands back for a FastAPI error: the real errorMessage() must surface `detail` untouched. */
function http(status: number, detail: string) {
  return new AxiosError("Request failed", "ERR_BAD_REQUEST", undefined, undefined, { status, statusText: "", headers: {}, config: {} as never, data: { detail } });
}

const base = { label: "main-bitget", account_mode: null, exchange_uid: "123456", sms_alerts_enabled: false, alert_phone: null, alert_email_secondary: null,
  alert_phone_secondary: null, telegram_handle: null, created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z" };
const WATCHING = { ...base, id: 1, name: "Main Bitget", exchange: "bitget", topology: "independent", status: "active", baseline_status: "complete", last_success_at: "2026-10-19T14:03:27Z" };
const SEEDING = { ...base, id: 2, name: "OKX savings", exchange: "okx", topology: "sub_account", status: "active", baseline_status: "seeding", last_success_at: null };
const PAUSED = { ...base, id: 3, name: "Bybit", exchange: "bybit", topology: "independent", status: "disabled", baseline_status: "complete", last_success_at: "2026-10-18T09:30:00+00:00" };
const SUSPECT = { ...base, id: 4, name: "Binance", exchange: "binance", topology: "independent", status: "tamper_suspected", baseline_status: "complete", last_success_at: "2026-10-19T08:00:00Z" };

/** The claim shapes the backend guard (tests/guardian/test_no_prevention_claims.py) refuses in customer copy. */
const PREVENTION_CLAIMS = new RegExp([
  String.raw`\bprevent(s|ed|ing|ion)?\b`,
  String.raw`\bstop(s|ped|ping)? (an?|the|any|your) (attack|theft|hack|withdrawal|intruder)`,
  String.raw`\bblock(s|ed|ing)? (an?|the|any|your) (attack|theft|hack|withdrawal|intruder)`,
  String.raw`\bfreez(e|es|ing) (your|the) (account|funds|withdrawal)`,
  String.raw`\b(protect|secure|safeguard)(s|ed|ing)? your (funds|money|assets|account)\b`,
  String.raw`\bkeep(s|ing)? your (funds|money|assets) safe\b`,
  String.raw`\bcancel(s|led|ling)? (an?|the) (withdrawal|transfer|trade)`,
  String.raw`\breverse(s|d)? (an?|the) (withdrawal|transfer|transaction)`,
].join("|"), "i");

function fillBitget(key = "bg_read_only_key", secret = "bg_secret_value", phrase = "phrase-1", name = " Savings ") {
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: name } });
  fireEvent.change(screen.getByLabelText("Read-only API key"), { target: { value: key } });
  fireEvent.change(screen.getByLabelText("API secret"), { target: { value: secret } });
  fireEvent.change(screen.getByLabelText("Passphrase"), { target: { value: phrase } });
}

describe("Monitoring keys: the list", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("names each account in plain words and shows the last check on the UTC clock, labelled", async () => {
    vi.mocked(guardianAPI.accounts).mockResolvedValue([WATCHING, SEEDING, PAUSED, SUSPECT] as never);
    render(<MonitoringKeys />);
    const rows = await screen.findAllByTestId("monitored-account");
    expect(rows).toHaveLength(4);
    expect(rows[0]).toHaveTextContent("Main Bitget · Bitget · Own account");
    expect(within(rows[0]).getByTestId("account-state")).toHaveTextContent("Watching");
    expect(within(rows[0]).getByTestId("last-check")).toHaveTextContent("Last check: 2026-10-19 14:03 UTC");
    expect(within(rows[0]).getByRole("button", { name: "Pause" })).toBeInTheDocument();
    expect(rows[1]).toHaveTextContent("OKX savings · OKX · Sub-account");
    expect(within(rows[1]).getByTestId("account-state")).toHaveTextContent("Setting up");
    expect(within(rows[1]).getByTestId("last-check")).toHaveTextContent("Last check: not yet");
    expect(rows[2]).toHaveTextContent("Bybit · Bybit · Own account");
    expect(within(rows[2]).getByTestId("account-state")).toHaveTextContent("Paused");
    expect(within(rows[2]).getByTestId("last-check")).toHaveTextContent("2026-10-18 09:30 UTC");
    expect(within(rows[2]).getByRole("button", { name: "Resume" })).toBeInTheDocument();
    expect(within(rows[3]).getByTestId("account-state")).toHaveTextContent("Needs attention");
    for (const row of rows) expect(row.textContent).not.toMatch(/tamper_suspected|baseline|seeding|disabled|fingerprint|123456/);   // engine words and identifiers stay out
    expect(screen.getByTestId("monitoring-intro")).toHaveTextContent(/separate read-only API key/);
    expect(screen.getByTestId("monitoring-intro")).toHaveTextContent(/withdrawals, new withdrawal addresses, permission changes and balance drops/);
  });

  it("shows the server's message when the list cannot be loaded", async () => {
    vi.mocked(guardianAPI.accounts).mockRejectedValue(http(503, "Guardian is not reachable right now. Try again in a minute."));
    render(<MonitoringKeys />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Guardian is not reachable right now. Try again in a minute.");
  });
});

describe("Monitoring keys: adding an account", () => {
  beforeEach(() => { vi.clearAllMocks(); vi.mocked(guardianAPI.accounts).mockResolvedValue([] as never); });

  it("Bitget: posts the exact body with SMS off by default and the passphrase, adds the row and clears the fields", async () => {
    vi.mocked(guardianAPI.addAccount).mockResolvedValue({ ...WATCHING, id: 9, name: "Savings", baseline_status: "seeding", last_success_at: null } as never);
    render(<MonitoringKeys />);
    await screen.findByText("No monitored accounts yet.");
    const add = screen.getByRole("button", { name: "Add account" });
    expect(add).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: " Savings " } });
    fireEvent.change(screen.getByLabelText("Read-only API key"), { target: { value: "bg_read_only_key" } });
    fireEvent.change(screen.getByLabelText("API secret"), { target: { value: "bg_secret_value" } });
    expect(add).toBeDisabled();   // Bitget keys come with a passphrase
    fireEvent.change(screen.getByLabelText("Passphrase"), { target: { value: "phrase-1" } });
    expect(screen.getByLabelText(/Text me about critical alerts/)).not.toBeChecked();
    expect(add).toBeEnabled();
    fireEvent.click(add);
    await waitFor(() => expect(guardianAPI.addAccount).toHaveBeenCalledTimes(1));
    expect(guardianAPI.addAccount).toHaveBeenCalledWith({
      exchange: "bitget", api_key: "bg_read_only_key", api_secret: "bg_secret_value", passphrase: "phrase-1",
      name: "Savings", topology: "independent", sms_alerts_enabled: false,
    });
    const row = await screen.findByTestId("monitored-account");
    expect(row).toHaveTextContent("Savings");
    expect(within(row).getByTestId("account-state")).toHaveTextContent("Setting up");
    expect(screen.getByRole("status")).toHaveTextContent("Savings added. Guardian is taking its first snapshot and starts checking it once that is done.");
    for (const label of ["Name", "Read-only API key", "API secret", "Passphrase"]) expect(screen.getByLabelText(label)).toHaveValue("");
  });

  it("Bybit: no passphrase field and none in the body; account type, SMS and the contact fields travel when given", async () => {
    vi.mocked(guardianAPI.addAccount).mockResolvedValue({ ...PAUSED, id: 10, status: "active", baseline_status: "seeding", last_success_at: null } as never);
    render(<MonitoringKeys />);
    await screen.findByText("No monitored accounts yet.");
    fireEvent.change(screen.getByLabelText("Exchange"), { target: { value: "bybit" } });
    expect(screen.queryByLabelText("Passphrase")).toBeNull();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Bybit" } });
    fireEvent.change(screen.getByLabelText("Account type"), { target: { value: "sub_account" } });
    fireEvent.change(screen.getByLabelText("Read-only API key"), { target: { value: "by_key" } });
    fireEvent.change(screen.getByLabelText("API secret"), { target: { value: "by_secret" } });
    fireEvent.click(screen.getByLabelText(/Text me about critical alerts/));
    fireEvent.change(screen.getByLabelText("Mobile number for SMS (optional)"), { target: { value: " +1 555 010 0100 " } });
    fireEvent.change(screen.getByLabelText("Backup email (optional)"), { target: { value: "Backup@Example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Add account" }));
    await waitFor(() => expect(guardianAPI.addAccount).toHaveBeenCalledWith({
      exchange: "bybit", api_key: "by_key", api_secret: "by_secret", name: "Bybit", topology: "sub_account", sms_alerts_enabled: true,
      alert_phone: "+1 555 010 0100", alert_email_secondary: "backup@example.com",
    }));
    expect(vi.mocked(guardianAPI.addAccount).mock.calls[0][0]).not.toHaveProperty("passphrase");
    await waitFor(() => expect(screen.getByLabelText(/Text me about critical alerts/)).not.toBeChecked());   // back to the default
  });

  it.each([["okx", true], ["binance", false]])("%s: the passphrase field is shown only when the exchange has one (%s)", async (exchange, hasPhrase) => {
    render(<MonitoringKeys />);
    await screen.findByText("No monitored accounts yet.");
    fireEvent.change(screen.getByLabelText("Exchange"), { target: { value: exchange } });
    expect(screen.queryByLabelText("Passphrase") !== null).toBe(hasPhrase);
    expect(Array.from((screen.getByLabelText("Exchange") as HTMLSelectElement).options).map((o) => o.text)).toEqual(["Bitget", "Bybit", "OKX", "Binance"]);
  });

  it.each([
    [400, "That looks like one of your AutoBuy purchase keys. Guardian needs a separate read-only key: create one on the exchange with read permissions only."],
    [409, "This exchange account is already monitored as Main Bitget. Replace its key there instead of adding it again."],
  ])("a %s is shown word for word and nothing typed is lost", async (status, detail) => {
    vi.mocked(guardianAPI.addAccount).mockRejectedValue(http(status, detail));
    render(<MonitoringKeys />);
    await screen.findByText("No monitored accounts yet.");
    fillBitget();
    fireEvent.click(screen.getByRole("button", { name: "Add account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    expect(screen.getByLabelText("Read-only API key")).toHaveValue("bg_read_only_key");
    expect(screen.queryByTestId("monitored-account")).toBeNull();
  });
});

describe("Monitoring keys: actions on an account", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("replace key: an inline panel, the server's refusal word for word, then the exact body on success", async () => {
    vi.mocked(guardianAPI.accounts).mockResolvedValue([WATCHING] as never);
    vi.mocked(guardianAPI.replaceKey)
      .mockRejectedValueOnce(http(400, "That key belongs to a different exchange account. Create the new key on the same account as the old one."))
      .mockResolvedValueOnce({ ok: true, message: "Key replaced. Guardian is checking the account with the new key." } as never);
    render(<MonitoringKeys />);
    const row = await screen.findByTestId("monitored-account");
    fireEvent.click(within(row).getByRole("button", { name: "Replace key" }));
    const panel = screen.getByTestId("replace-key-panel");
    expect(panel).toHaveTextContent(/read-only and belongs to the same exchange account/);
    const replace = within(panel).getByRole("button", { name: "Replace" });
    expect(replace).toBeDisabled();
    fireEvent.change(within(panel).getByLabelText("New read-only API key"), { target: { value: "new_key" } });
    fireEvent.change(within(panel).getByLabelText("New API secret"), { target: { value: "new_secret" } });
    expect(replace).toBeDisabled();   // Bitget: the passphrase too
    fireEvent.change(within(panel).getByLabelText("New passphrase"), { target: { value: "new_phrase" } });
    fireEvent.click(replace);
    expect(await within(panel).findByRole("alert")).toHaveTextContent("That key belongs to a different exchange account. Create the new key on the same account as the old one.");
    expect(screen.getByTestId("replace-key-panel")).toBeInTheDocument();   // stays open so the key can be fixed
    fireEvent.click(within(panel).getByRole("button", { name: "Replace" }));
    await waitFor(() => expect(guardianAPI.replaceKey).toHaveBeenCalledTimes(2));
    expect(guardianAPI.replaceKey).toHaveBeenLastCalledWith(1, { api_key: "new_key", api_secret: "new_secret", passphrase: "new_phrase" });
    await waitFor(() => expect(screen.queryByTestId("replace-key-panel")).toBeNull());
    expect(screen.getByRole("status")).toHaveTextContent("Key replaced. Guardian is checking the account with the new key.");
    expect(screen.getByRole("status").textContent).not.toMatch(/reason_code|same_uid|read_only_required/);
  });

  it("replace key on an exchange without a passphrase sends none", async () => {
    vi.mocked(guardianAPI.accounts).mockResolvedValue([PAUSED] as never);
    vi.mocked(guardianAPI.replaceKey).mockResolvedValue({ ok: true, message: "Key replaced." } as never);
    render(<MonitoringKeys />);
    fireEvent.click(await screen.findByRole("button", { name: "Replace key" }));
    const panel = screen.getByTestId("replace-key-panel");
    expect(within(panel).queryByLabelText("New passphrase")).toBeNull();
    fireEvent.change(within(panel).getByLabelText("New read-only API key"), { target: { value: "k" } });
    fireEvent.change(within(panel).getByLabelText("New API secret"), { target: { value: "s" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Replace" }));
    await waitFor(() => expect(guardianAPI.replaceKey).toHaveBeenCalledWith(3, { api_key: "k", api_secret: "s" }));
  });

  it("pause and resume call disable/enable, take the server's row and say what changed", async () => {
    vi.mocked(guardianAPI.accounts).mockResolvedValue([WATCHING] as never);
    vi.mocked(guardianAPI.disable).mockResolvedValue({ ...WATCHING, status: "disabled" } as never);
    vi.mocked(guardianAPI.enable).mockResolvedValue({ ...WATCHING, status: "active" } as never);
    render(<MonitoringKeys />);
    const row = await screen.findByTestId("monitored-account");
    fireEvent.click(within(row).getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(guardianAPI.disable).toHaveBeenCalledWith(1));
    await waitFor(() => expect(screen.getByTestId("account-state")).toHaveTextContent("Paused"));
    expect(screen.getByRole("status")).toHaveTextContent("Main Bitget is paused. Guardian is not checking it until you resume.");
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(guardianAPI.enable).toHaveBeenCalledWith(1));
    await waitFor(() => expect(screen.getByTestId("account-state")).toHaveTextContent("Watching"));
    expect(screen.getByRole("status")).toHaveTextContent("Main Bitget resumed. Guardian is checking it again.");
    expect(guardianAPI.accounts).toHaveBeenCalledTimes(1);   // the answered row is enough; no re-read
  });

  it("remove asks first, says the key goes and the history stays, and only then calls the API", async () => {
    vi.mocked(guardianAPI.accounts).mockResolvedValue([WATCHING, SEEDING] as never);
    vi.mocked(guardianAPI.remove).mockResolvedValue(undefined as never);
    render(<MonitoringKeys />);
    const rows = await screen.findAllByTestId("monitored-account");
    fireEvent.click(within(rows[0]).getByRole("button", { name: "Remove" }));
    expect(guardianAPI.remove).not.toHaveBeenCalled();
    const confirm = screen.getByTestId("remove-confirm");
    expect(confirm).toHaveTextContent(/Remove Main Bitget\?/);
    expect(confirm).toHaveTextContent(/deletes this key/);
    expect(confirm).toHaveTextContent(/alert history is kept/);
    fireEvent.click(within(confirm).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByTestId("remove-confirm")).toBeNull();
    expect(guardianAPI.remove).not.toHaveBeenCalled();
    fireEvent.click(within(rows[0]).getByRole("button", { name: "Remove" }));
    fireEvent.click(within(screen.getByTestId("remove-confirm")).getByRole("button", { name: "Yes, remove" }));
    await waitFor(() => expect(guardianAPI.remove).toHaveBeenCalledWith(1));
    await waitFor(() => expect(screen.getAllByTestId("monitored-account")).toHaveLength(1));
    expect(screen.getByTestId("monitored-account")).toHaveTextContent("OKX savings");
    expect(screen.getByRole("status")).toHaveTextContent("Main Bitget removed. The key is deleted; the alert history is kept.");
  });

  it("a refused removal keeps the row and shows the reason", async () => {
    vi.mocked(guardianAPI.accounts).mockResolvedValue([WATCHING] as never);
    vi.mocked(guardianAPI.remove).mockRejectedValue(http(404, "Not found"));
    render(<MonitoringKeys />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));
    fireEvent.click(screen.getByRole("button", { name: "Yes, remove" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Not found");
    expect(screen.getAllByTestId("monitored-account")).toHaveLength(1);
  });
});

describe("Monitoring keys: credentials and copy", () => {
  beforeEach(() => { vi.clearAllMocks(); vi.mocked(guardianAPI.accounts).mockResolvedValue([WATCHING] as never); });

  it("credential fields stay out of autofill and password managers, with no form and no password input anywhere", async () => {
    const { container } = render(<MonitoringKeys />);
    await screen.findByTestId("monitored-account");
    fireEvent.click(screen.getByRole("button", { name: "Replace key" }));
    const fields = ["Read-only API key", "API secret", "Passphrase", "New read-only API key", "New API secret", "New passphrase"].map((l) => screen.getByLabelText(l));
    for (const el of fields) {
      expect(el).toHaveAttribute("type", "text");
      expect(el).toHaveAttribute("autocomplete", "one-time-code");
      expect(el).toHaveAttribute("data-lpignore", "true");
      expect(el).toHaveAttribute("data-1p-ignore");
      expect(el).toHaveAttribute("data-form-type", "other");
      expect(el.closest("form")).toBeNull();
      expect(el.id).not.toMatch(/email|user|login|password|pass$|secret/i);
      expect(el.getAttribute("name")).not.toMatch(/email|user|login|password|pass$|secret/i);
    }
    for (const label of ["Read-only API key", "API secret", "New read-only API key", "New API secret"]) expect(screen.getByLabelText(label)).toHaveClass("secret-masked");
    expect(screen.getByRole("button", { name: "Show API secret" })).toBeInTheDocument();
    expect(container.querySelectorAll("form")).toHaveLength(0);
    expect(container.querySelectorAll('input[type="password"]')).toHaveLength(0);
    for (const button of Array.from(container.querySelectorAll("button"))) expect(button).toHaveAttribute("type", "button");
  });

  it("never claims prevention: Guardian detects and tells", async () => {
    const { container } = render(<MonitoringKeys />);
    await screen.findByTestId("monitored-account");
    expect(container.textContent).toMatch(/It detects and tells/);
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(container.textContent).not.toMatch(PREVENTION_CLAIMS);
    fireEvent.click(screen.getByRole("button", { name: "Replace key" }));
    expect(container.textContent).not.toMatch(PREVENTION_CLAIMS);
    expect(container.textContent).not.toMatch(/\bsentinel\b/i);   // the old product name stays out of the customer copy
  });
});
