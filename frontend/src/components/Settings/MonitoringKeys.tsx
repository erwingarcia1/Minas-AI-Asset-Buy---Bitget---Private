import { forwardRef, useState } from "react";
import { guardianAPI } from "@/api/endpoints";
import { errorMessage } from "@/api/client";
import { Alert, Loading, SecretInput } from "@/components/Common";
import { useAsync } from "@/hooks/useAsync";
import type { GuardianAccount, GuardianAccountCreate, GuardianExchange, GuardianTopology } from "@/types";
import { capitalize, fmtUtc } from "@/utils/format";
import { isEmail } from "@/utils/validation";

type Msg = { kind: "error" | "success" | "info"; text: string } | null;
type Panel = { id: number; kind: "replace" | "remove" } | null;

export const GUARDIAN_EXCHANGES: GuardianExchange[] = ["bitget", "bybit", "okx", "binance"];
/** Exchanges whose API keys come with a passphrase. */
const NEEDS_PASSPHRASE: ReadonlySet<string> = new Set(["bitget", "okx"]);

export const exchangeLabel = (exchange: string) => (exchange === "okx" ? "OKX" : capitalize(exchange));
export const topologyLabel = (topology: GuardianTopology | string) => (topology === "sub_account" ? "Sub-account" : "Own account");
const displayName = (a: GuardianAccount) => a.name || a.label;

/** The engine's state in plain words. A removed account keeps its row as disabled, so it reads "Paused" too. */
export function accountState(a: Pick<GuardianAccount, "status" | "baseline_status">): { label: string; tone: string } {
  if (a.status === "disabled") return { label: "Paused", tone: "bg-slate-100 text-slate-700" };
  if (a.status === "tamper_suspected") return { label: "Needs attention", tone: "bg-red-100 text-red-800" };
  if (a.baseline_status === "seeding") return { label: "Setting up", tone: "bg-amber-100 text-amber-800" };
  return { label: "Watching", tone: "bg-green-100 text-green-800" };
}

/** Guardian's monitored exchange accounts: the read-only keys it watches, and the actions on each one.
 *  Guardian detects and tells; nothing here claims it can act on an account. */
const MonitoringKeys = forwardRef<HTMLElement, { highlight?: boolean }>(function MonitoringKeys({ highlight }, ref) {
  const accounts = useAsync(() => guardianAPI.accounts());
  const [msg, setMsg] = useState<Msg>(null);
  const [busy, setBusy] = useState(false);
  // the add form
  const [exchange, setExchange] = useState<GuardianExchange>("bitget");
  const [name, setName] = useState("");
  const [topology, setTopology] = useState<GuardianTopology>("independent");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [sms, setSms] = useState(false);
  const [backupEmail, setBackupEmail] = useState("");
  const [phone, setPhone] = useState("");
  // one open panel at a time (replace key / confirm removal) and its own message
  const [panel, setPanel] = useState<Panel>(null);
  const [panelMsg, setPanelMsg] = useState<Msg>(null);
  const [newKey, setNewKey] = useState("");
  const [newSecret, setNewSecret] = useState("");
  const [newPhrase, setNewPhrase] = useState("");

  const rows = accounts.data ?? [];
  const needsPassphrase = NEEDS_PASSPHRASE.has(exchange);
  // SMS alerts go to the mobile number, so ticking the box makes the number required
  const canAdd = !busy && name.trim().length > 0 && apiKey.length > 0 && apiSecret.length > 0 && (!needsPassphrase || passphrase.length > 0) && (!sms || phone.trim().length > 0);

  function openPanel(id: number, kind: "replace" | "remove") {
    setPanel({ id, kind }); setPanelMsg(null); setNewKey(""); setNewSecret(""); setNewPhrase("");
  }
  function closePanel() {
    setPanel(null); setPanelMsg(null); setNewKey(""); setNewSecret(""); setNewPhrase("");
  }
  /** Take the row the server answered with; without one, re-read the list. */
  async function absorb(updated: GuardianAccount | undefined) {
    if (updated && typeof updated === "object" && "id" in updated) accounts.setData((prev) => (prev ?? []).map((a) => (a.id === updated.id ? { ...a, ...updated } : a)));
    else await accounts.refresh();
  }

  async function add() {
    // no <form>: the key fields are never submitted natively, so the browser has no
    // submission to attach a "save password" prompt to (same as the exchange-key form)
    setMsg(null);
    const backup = backupEmail.trim().toLowerCase();
    if (backup && !isEmail(backup)) return setMsg({ kind: "error", text: "Enter a valid backup email address, or leave that field empty." });   // checked here so the server's field-level 422 never reaches the customer
    setBusy(true);
    const body: GuardianAccountCreate = {
      exchange, api_key: apiKey, api_secret: apiSecret,
      ...(needsPassphrase ? { passphrase } : {}),
      name: name.trim(), topology, sms_alerts_enabled: sms,
      ...(phone.trim() ? { alert_phone: phone.trim() } : {}),
      ...(backup ? { alert_email_secondary: backup } : {}),
    };
    try {
      const created = await guardianAPI.addAccount(body);
      accounts.setData((prev) => [...(prev ?? []), created]);
      setName(""); setApiKey(""); setApiSecret(""); setPassphrase(""); setSms(false); setBackupEmail(""); setPhone("");
      setMsg({ kind: "success", text: `${displayName(created)} added. Guardian is taking its first snapshot and starts checking it once that is done.` });
    } catch (err) { setMsg({ kind: "error", text: errorMessage(err) }); }
    finally { setBusy(false); }
  }

  async function replaceKey(a: GuardianAccount) {
    setPanelMsg(null); setBusy(true);
    try {
      const r = await guardianAPI.replaceKey(a.id, { api_key: newKey, api_secret: newSecret, ...(NEEDS_PASSPHRASE.has(a.exchange) ? { passphrase: newPhrase } : {}) });
      closePanel();
      setMsg({ kind: "success", text: r?.message || `Key replaced for ${displayName(a)}.` });
      await accounts.refresh();
    } catch (err) { setPanelMsg({ kind: "error", text: errorMessage(err) }); }   // the server's reason, verbatim
    finally { setBusy(false); }
  }

  async function setPaused(a: GuardianAccount, paused: boolean) {
    setMsg(null); setBusy(true);
    try {
      await absorb(paused ? await guardianAPI.disable(a.id) : await guardianAPI.enable(a.id));
      setMsg({ kind: "info", text: paused ? `${displayName(a)} is paused. Guardian is not checking it until you resume.` : `${displayName(a)} resumed. Guardian is checking it again.` });
    } catch (err) { setMsg({ kind: "error", text: errorMessage(err) }); }
    finally { setBusy(false); }
  }

  async function remove(a: GuardianAccount) {
    setPanelMsg(null); setBusy(true);
    try {
      await guardianAPI.remove(a.id);
      accounts.setData((prev) => (prev ?? []).filter((x) => x.id !== a.id));
      closePanel();
      setMsg({ kind: "info", text: `${displayName(a)} removed. The key is deleted; the alert history is kept.` });
    } catch (err) { setPanelMsg({ kind: "error", text: errorMessage(err) }); }
    finally { setBusy(false); }
  }

  return (
    <section ref={ref} id="monitoring" className={`card space-y-4 ${highlight ? "ring-2 ring-brand-500" : ""}`} aria-labelledby="monitoring-title">
      <h2 id="monitoring-title" className="font-semibold">Guardian: exchange monitoring</h2>
      <p className="text-sm text-slate-600" data-testid="monitoring-intro">
        Guardian watches your exchange account through a separate read-only API key and tells you about withdrawals, new withdrawal
        addresses, permission changes and balance drops. It detects and tells: it does not act on your account, and it never uses an
        AutoBuy purchase key. Times are UTC.
      </p>

      {accounts.loading && !accounts.data ? <Loading /> : accounts.error ? <Alert kind="error">{accounts.error}</Alert> : (
        <ul className="divide-y divide-slate-100 text-sm" aria-label="Monitored accounts">
          {rows.length === 0 && <li className="py-2 text-slate-500">No monitored accounts yet.</li>}
          {rows.map((a) => {
            const state = accountState(a);
            const open = panel?.id === a.id ? panel.kind : null;
            const paused = a.status === "disabled";
            return (
              <li key={a.id} className="py-3" data-testid="monitored-account">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <p>
                      <span className="font-medium">{displayName(a)}</span>
                      <span className="text-slate-500"> · {exchangeLabel(a.exchange)} · {topologyLabel(a.topology)} </span>
                      <span className={`badge ${state.tone}`} data-testid="account-state">{state.label}</span>
                    </p>
                    <p className="text-xs text-slate-500" data-testid="last-check">Last check: {a.last_success_at ? fmtUtc(a.last_success_at) : "not yet"}</p>
                  </div>
                  <span className="flex flex-wrap gap-2">
                    <button className="btn-secondary !py-1" type="button" disabled={busy} onClick={() => (open === "replace" ? closePanel() : openPanel(a.id, "replace"))}>Replace key</button>
                    {paused
                      ? <button className="btn-secondary !py-1" type="button" disabled={busy} onClick={() => void setPaused(a, false)}>Resume</button>
                      : <button className="btn-secondary !py-1" type="button" disabled={busy} onClick={() => void setPaused(a, true)}>Pause</button>}
                    <button className="btn-danger !py-1" type="button" disabled={busy} onClick={() => (open === "remove" ? closePanel() : openPanel(a.id, "remove"))}>Remove</button>
                  </span>
                </div>

                {open === "replace" && (
                  <div className="mt-3 grid gap-3 rounded-md border border-slate-200 bg-slate-50 p-3 sm:grid-cols-2" role="group" aria-label={`Replace the key for ${displayName(a)}`} data-testid="replace-key-panel">
                    <p className="text-sm text-slate-600 sm:col-span-2">
                      Create a new read-only key on {exchangeLabel(a.exchange)} for the same account and paste it here. Guardian checks that it is
                      read-only and belongs to the same exchange account before it replaces the old one.
                    </p>
                    <div><label className="label" htmlFor={`monitoring_replacement_public_${a.id}`}>New read-only API key</label>
                      <SecretInput id={`monitoring_replacement_public_${a.id}`} name={`monitoring_replacement_public_${a.id}`} label="New read-only API key" value={newKey} onChange={setNewKey} /></div>
                    <div><label className="label" htmlFor={`monitoring_replacement_private_${a.id}`}>New API secret</label>
                      <SecretInput id={`monitoring_replacement_private_${a.id}`} name={`monitoring_replacement_private_${a.id}`} label="New API secret" value={newSecret} onChange={setNewSecret} /></div>
                    {NEEDS_PASSPHRASE.has(a.exchange) && (
                      <div><label className="label" htmlFor={`monitoring_replacement_phrase_${a.id}`}>New passphrase</label>
                        <SecretInput id={`monitoring_replacement_phrase_${a.id}`} name={`monitoring_replacement_phrase_${a.id}`} label="New passphrase" value={newPhrase} onChange={setNewPhrase} /></div>
                    )}
                    {panelMsg && <div className="sm:col-span-2"><Alert kind={panelMsg.kind}>{panelMsg.text}</Alert></div>}
                    <div className="flex gap-2 sm:col-span-2">
                      <button className="btn-primary" type="button" disabled={busy || !newKey || !newSecret || (NEEDS_PASSPHRASE.has(a.exchange) && !newPhrase)} onClick={() => void replaceKey(a)}>Replace</button>
                      <button className="btn-secondary" type="button" onClick={closePanel}>Cancel</button>
                    </div>
                  </div>
                )}

                {open === "remove" && (
                  <div className="mt-3 space-y-3 rounded-md border border-red-200 bg-red-50 p-3" role="group" aria-label={`Remove ${displayName(a)}`} data-testid="remove-confirm">
                    <p className="text-sm text-slate-700">
                      Remove {displayName(a)}? Guardian deletes this key and no longer checks the account. The alert history is kept, so what
                      Guardian told you stays readable. You can add the account again with a new read-only key.
                    </p>
                    {panelMsg && <Alert kind={panelMsg.kind}>{panelMsg.text}</Alert>}
                    <div className="flex gap-2">
                      <button className="btn-danger" type="button" disabled={busy} onClick={() => void remove(a)}>Yes, remove</button>
                      <button className="btn-secondary" type="button" onClick={closePanel}>Cancel</button>
                    </div>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {msg && <Alert kind={msg.kind}>{msg.text}</Alert>}

      <div className="grid gap-3 sm:grid-cols-2" role="group" aria-label="Add a monitored account" data-testid="monitoring-fields">
        <p className="text-sm text-slate-600 sm:col-span-2">
          Add an account with a key created for Guardian alone: <b>read permissions only</b>, no trade, no withdraw, no transfer. Guardian
          refuses a key that can do more, and refuses your AutoBuy purchase keys.
        </p>
        <div><label className="label" htmlFor="monitoring-exchange">Exchange</label>
          <select id="monitoring-exchange" className="input" value={exchange} onChange={(e) => setExchange(e.target.value as GuardianExchange)}>
            {GUARDIAN_EXCHANGES.map((x) => <option key={x} value={x}>{exchangeLabel(x)}</option>)}
          </select></div>
        <div><label className="label" htmlFor="monitoring-name">Name</label>
          <input id="monitoring-name" className="input" type="text" maxLength={80} value={name} onChange={(e) => setName(e.target.value)} placeholder="Main Bitget account" /></div>
        <div><label className="label" htmlFor="monitoring-topology">Account type</label>
          <select id="monitoring-topology" className="input" value={topology} onChange={(e) => setTopology(e.target.value as GuardianTopology)}>
            <option value="independent">Own account</option>
            <option value="sub_account">Sub-account</option>
          </select></div>
        {needsPassphrase && (
          <div><label className="label" htmlFor="monitoring_credential_phrase">Passphrase</label>
            <SecretInput id="monitoring_credential_phrase" name="monitoring_credential_phrase" label="Passphrase" value={passphrase} onChange={setPassphrase} /></div>
        )}
        <div><label className="label" htmlFor="monitoring_credential_public">Read-only API key</label>
          <SecretInput id="monitoring_credential_public" name="monitoring_credential_public" label="Read-only API key" value={apiKey} onChange={setApiKey} /></div>
        <div><label className="label" htmlFor="monitoring_credential_private">API secret</label>
          <SecretInput id="monitoring_credential_private" name="monitoring_credential_private" label="API secret" value={apiSecret} onChange={setApiSecret} /></div>
        <div className="flex items-start gap-3 sm:col-span-2">
          <input id="monitoring-sms" type="checkbox" className="mt-1 h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500" checked={sms} onChange={(e) => setSms(e.target.checked)} />
          <label htmlFor="monitoring-sms" className="text-sm"><span className="font-medium text-slate-900">Text me about critical alerts (SMS)</span>
            <span className="block text-slate-500">Off by default. Needs the mobile number below.</span></label>
        </div>
        <div><label className="label" htmlFor="monitoring-phone">{sms ? "Mobile number for SMS (required)" : "Mobile number for SMS (optional)"}</label>
          <input id="monitoring-phone" className="input" type="tel" autoComplete="tel" aria-required={sms} value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+1 555 010 0100" /></div>
        <div><label className="label" htmlFor="monitoring-backup-email">Backup email (optional)</label>
          <input id="monitoring-backup-email" className="input" type="email" autoComplete="off" value={backupEmail} onChange={(e) => setBackupEmail(e.target.value)} /></div>
        <div className="sm:col-span-2"><button className="btn-primary" type="button" disabled={!canAdd} onClick={() => void add()}>Add account</button></div>
      </div>
    </section>
  );
});

export default MonitoringKeys;
