import { useEffect, useRef, useState, type FormEvent } from "react";
import { useLocation } from "react-router-dom";
import { authAPI, notificationAPI, publicAPI, userAPI } from "@/api/endpoints";
import { errorMessage } from "@/api/client";
import { Alert, CopyButton, Loading, SecretInput, StatusBadge } from "@/components/Common";
import { NO_AUTOFILL } from "@/utils/noAutofill";
import Integrations from "@/components/Settings/Integrations";
import Reports from "@/components/Settings/Reports";
import { useAsync } from "@/hooks/useAsync";
import { useAuth } from "@/hooks/useAuth";
import { passwordProblem } from "@/utils/validation";
import { capitalize, fmtDate } from "@/utils/format";
import NotificationSettings from "@/components/Settings/NotificationSettings";
import WalletHoldNotice from "@/components/Common/WalletHoldNotice";

type Msg = { kind: "error" | "success"; text: string } | null;

/** Bitget permission codes AutoBuy recognises but does not need (confirmed from Bitget's "Edit API key" screen). */
const PERMISSION_LABELS: Record<string, string> = {
  smow: "Margin → Trade", wtow: "Wallet → Transfer (internal)", taxw: "Taxation → Search", p2pw: "P2P → Search", transfer: "Transfer (internal)",
};

export default function Settings() {
  const { user, logout } = useAuth();
  const location = useLocation();
  const notificationsRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    // the "unsubscribe" link in emails lands on /settings/notifications
    if (location.pathname.endsWith("/notifications") && typeof notificationsRef.current?.scrollIntoView === "function") notificationsRef.current.scrollIntoView({ block: "start" });
  }, [location.pathname]);
  const keys = useAsync(() => userAPI.exchangeKeys());
  const supported = useAsync(() => userAPI.supportedExchanges().catch(() => ["bitget"]));
  const meta = useAsync(() => publicAPI.meta().catch(() => null));
  const [pwMsg, setPwMsg] = useState<Msg>(null);
  const [mailMsg, setMailMsg] = useState<Msg>(null);
  const [keyMsg, setKeyMsg] = useState<Msg>(null);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [exchange, setExchange] = useState("bitget");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [verifying, setVerifying] = useState<string | null>(null);   // must sit above the early return: hooks run in the same order on every render

  if (!user) return <Loading />;

  async function changePassword(e: FormEvent) {
    e.preventDefault();
    const p = passwordProblem(next);
    if (p) return setPwMsg({ kind: "error", text: p });
    try {
      await authAPI.changePassword(current, next);
      setPwMsg({ kind: "success", text: "Password changed. Every other session has been signed out." });
      setCurrent(""); setNext("");
      setTimeout(() => void logout(), 1500);
    } catch (err) { setPwMsg({ kind: "error", text: errorMessage(err) }); }
  }

  async function sendVerification() {
    setMailMsg(null);
    try {
      const r = await userAPI.sendVerification(user!.email);
      setMailMsg({ kind: "success", text: r.sent ? "Verification email sent. Check your inbox." : "Email was not sent." });
    } catch (err) { setMailMsg({ kind: "error", text: errorMessage(err) }); }
  }


  async function verifyKey(id: string) {
    setKeyMsg(null); setVerifying(id);
    try {
      const v = await userAPI.verifyExchangeKey(id);
      setKeyMsg({ kind: v.verified ? "success" : "error", text: v.detail });
    } catch (err) { setKeyMsg({ kind: "error", text: errorMessage(err, "Verification request failed") }); }
    finally { setVerifying(null); await keys.reload(); }
  }

  async function addKey() {
    // no <form>: the key fields are never submitted natively, so the browser
    // has no submission to attach a "save password" prompt to
    setKeyMsg(null);
    try {
      const k = await userAPI.addExchangeKey({ exchange, api_key: apiKey, api_secret: apiSecret, passphrase: passphrase || undefined });
      setApiKey(""); setApiSecret(""); setPassphrase("");
      await keys.reload();
      await verifyKey(k.id);   // the outcome (verified / failed + reason) is shown, never swallowed
    } catch (err) { setKeyMsg({ kind: "error", text: errorMessage(err) }); }
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Settings</h1>

      <section className="card space-y-3">
        <h2 className="font-semibold">Account</h2>
        <dl className="grid gap-2 text-sm sm:grid-cols-2">
          <div><dt className="text-slate-500">Email</dt><dd className="font-medium">{user.email}</dd></div>
          <div><dt className="text-slate-500">Verification</dt><dd><StatusBadge status={user.kyc_status} /></dd></div>
          <div><dt className="text-slate-500">Member since</dt><dd>{fmtDate(user.created_at)}</dd></div>
          <div><dt className="text-slate-500">User ID</dt><dd className="font-mono text-xs">{user.id}</dd></div>
        </dl>
        <WalletHoldNotice user={user} />
        {mailMsg && <Alert kind={mailMsg.kind}>{mailMsg.text}</Alert>}
        <button className="btn-secondary" onClick={() => void sendVerification()}>Send verification email</button>
      </section>

      <NotificationSettings ref={notificationsRef} highlight={location.pathname.endsWith("/notifications")} api={notificationAPI} />

      <section className="card space-y-4">
        <h2 className="font-semibold">Exchange API keys</h2>
        <p className="text-sm text-slate-600">Keys are encrypted at rest and verified against the exchange before purchases can execute.</p>
        <div className="rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-700" data-testid="key-guidance">
          <p className="font-medium">Which Bitget key permissions do I need?</p>
          <ul className="mt-1 list-disc space-y-1 pl-5">
            <li><span className="font-medium">Accumulate on exchange:</span> a key with <span className="font-medium">Read</span> and <span className="font-medium">Spot trade</span> only. Leave Withdraw off.</li>
            <li><span className="font-medium">Withdraw immediately (send each buy to your wallet):</span> the key also needs <span className="font-medium">Withdraw</span>, and Bitget requires two things before it will send anything:
              <ul className="mt-1 list-disc space-y-1 pl-5">
                <li>Whitelist our server's IP on the key.{" "}
                  {meta.data?.server_ip ? (
                    <span className="inline-flex flex-wrap items-center gap-2 align-middle">
                      <code className="rounded bg-slate-100 px-2 py-0.5 font-mono text-xs" data-testid="server-ip">{meta.data.server_ip}</code>
                      <CopyButton value={meta.data.server_ip} label="Copy IP" />
                    </span>
                  ) : <span className="font-mono text-xs" data-testid="server-ip-placeholder">our server IP will be shown here</span>}</li>
                <li>Add your wallet address to your Bitget address book (withdrawal whitelist) first — Bitget rejects API withdrawals to unknown addresses.</li>
              </ul>
            </li>
            <li data-testid="fee-note">Fees: Bitget's trading fee applies, taken from the coin you receive. AutoBuy adds no fee of its own.</li>
            <li>Bitget reports permissions as codes. Spot → Trade is required; Margin → Trade, Wallet → Transfer, Taxation → Search and P2P → Search are recognised as not needed (a warning, nothing blocked). Any code we have not confirmed yet is treated as "withdrawal possible": use such a key only where that is intended.</li>
          </ul>
        </div>
        {keys.loading ? <Loading /> : keys.error ? <Alert kind="error">{keys.error}</Alert> : (
          <ul className="divide-y divide-slate-100 text-sm">
            {keys.data?.length === 0 && <li className="py-2 text-slate-500">No keys on file.</li>}
            {keys.data?.map((k) => (
              <li key={k.id} className="py-2" data-testid="exchange-key">
                <div className="flex items-center justify-between gap-3">
                  <span><span className="font-medium capitalize">{k.exchange}</span> <span className="text-slate-500">{String(k.api_key_masked ?? "")}</span> <StatusBadge status={k.verification_status ?? (k.verified ? "verified" : "pending")} /></span>
                  <span className="space-x-2 whitespace-nowrap">
                    {k.verification_status !== "verified" && (
                      <button className="btn-secondary !py-1" disabled={verifying === k.id} onClick={() => void verifyKey(k.id)}>
                        {verifying === k.id ? "Verifying…" : k.verification_status === "failed" ? "Retry verification" : "Verify now"}
                      </button>
                    )}
                    <button className="btn-danger !py-1" onClick={() => void userAPI.deleteExchangeKey(k.id).then(() => keys.reload())}>Remove</button>
                  </span>
                </div>
                {k.verification_status === "failed" && k.verification_error && (
                  <p className="mt-1 text-xs text-red-600" role="alert">{k.verification_error}{k.last_checked_at ? ` (checked ${fmtDate(k.last_checked_at)})` : ""}</p>
                )}
                {k.verification_status === "verified" && k.withdrawal_enabled === true && (
                  <p className="mt-1 text-xs text-amber-700" role="status">This key can withdraw funds. Restrict it to spot trading only on {capitalize(k.exchange)}.</p>
                )}
                {k.verification_status === "verified" && k.withdrawal_enabled == null && (
                  <p className="mt-1 text-xs text-amber-700" role="status">Withdrawal permission could not be confirmed for this key — treat it as able to withdraw. Use it for accumulate purchases only unless you have disabled withdrawals on {capitalize(k.exchange)}.</p>
                )}
                {k.verification_status === "verified" && (k.extra_permissions?.length ?? 0) > 0 && (
                  <p className="mt-1 text-xs text-amber-700" data-testid="extra-permissions">This key has more permissions than AutoBuy needs ({k.extra_permissions!.map((c) => PERMISSION_LABELS[c] ?? c).join(", ")}). For safety, use a key with only Spot → Trade.</p>
                )}
                {k.verification_status === "verified" && k.verified_at && (
                  <p className="mt-1 text-xs text-slate-400">Verified {fmtDate(k.verified_at)}</p>
                )}
              </li>
            ))}
          </ul>
        )}
        {keyMsg && <Alert kind={keyMsg.kind}>{keyMsg.text}</Alert>}
        <div className="grid gap-3 sm:grid-cols-2" role="group" aria-label="Add an exchange API key" data-testid="exchange-key-fields">
          <div><label className="label" htmlFor="key-exchange">Exchange</label>
            <select id="key-exchange" className="input" value={exchange} onChange={(e) => setExchange(e.target.value)}>
              {(supported.data ?? ["bitget"]).map((x) => <option key={x} value={x}>{x === "okx" ? "OKX" : capitalize(x)}</option>)}
            </select></div>
          <div><label className="label" htmlFor="exchange_credential_phrase">Passphrase (Bitget/OKX)</label>
            <input id="exchange_credential_phrase" name="exchange_credential_phrase" className="input" type="text" value={passphrase} onChange={(e) => setPassphrase(e.target.value)} {...NO_AUTOFILL} /></div>
          <div><label className="label" htmlFor="exchange_credential_public">API key</label>
            <input id="exchange_credential_public" name="exchange_credential_public" className="input" type="text" value={apiKey} onChange={(e) => setApiKey(e.target.value)} {...NO_AUTOFILL} /></div>
          <div><label className="label" htmlFor="exchange_credential_private">API secret</label>
            <SecretInput id="exchange_credential_private" name="exchange_credential_private" label="API secret" value={apiSecret} onChange={setApiSecret} /></div>
          <div className="sm:col-span-2"><button className="btn-primary" type="button" disabled={!apiKey || !apiSecret} onClick={() => void addKey()}>Save key</button></div>
        </div>
      </section>

      <section className="card space-y-4">
        <h2 className="font-semibold">Change password</h2>
        {pwMsg && <Alert kind={pwMsg.kind}>{pwMsg.text}</Alert>}
        <form onSubmit={changePassword} className="grid gap-3 sm:grid-cols-2">
          <div><label className="label">Current password</label><input className="input" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} /></div>
          <div><label className="label">New password</label><input className="input" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} /></div>
          <div className="sm:col-span-2"><button className="btn-primary" type="submit" disabled={!current || !next}>Update password</button></div>
        </form>
      </section>

      <Reports />
      <Integrations />
    </div>
  );
}
