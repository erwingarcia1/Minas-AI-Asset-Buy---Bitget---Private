import { useState, type FormEvent } from "react";
import { authAPI, userAPI } from "@/api/endpoints";
import { errorMessage } from "@/api/client";
import { Alert, Loading, StatusBadge } from "@/components/Common";
import { useAsync } from "@/hooks/useAsync";
import { useAuth } from "@/hooks/useAuth";
import { passwordProblem } from "@/utils/validation";
import { fmtDate } from "@/utils/format";

type Msg = { kind: "error" | "success"; text: string } | null;

export default function Settings() {
  const { user, logout } = useAuth();
  const keys = useAsync(() => userAPI.exchangeKeys());
  const [pwMsg, setPwMsg] = useState<Msg>(null);
  const [mailMsg, setMailMsg] = useState<Msg>(null);
  const [keyMsg, setKeyMsg] = useState<Msg>(null);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [exchange, setExchange] = useState("binance");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [passphrase, setPassphrase] = useState("");

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

  async function addKey(e: FormEvent) {
    e.preventDefault();
    setKeyMsg(null);
    try {
      const k = await userAPI.addExchangeKey({ exchange, api_key: apiKey, api_secret: apiSecret, passphrase: passphrase || undefined });
      setApiKey(""); setApiSecret(""); setPassphrase("");
      await userAPI.verifyExchangeKey(k.id).catch(() => undefined);
      setKeyMsg({ kind: "success", text: "Key saved and verification requested." });
      await keys.reload();
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
        {mailMsg && <Alert kind={mailMsg.kind}>{mailMsg.text}</Alert>}
        <button className="btn-secondary" onClick={() => void sendVerification()}>Send verification email</button>
      </section>

      <section className="card space-y-4">
        <h2 className="font-semibold">Exchange API keys</h2>
        <p className="text-sm text-slate-600">Keys are encrypted at rest and verified against the exchange before purchases can execute. Use spot-trade-only keys with withdrawals disabled.</p>
        {keys.loading ? <Loading /> : keys.error ? <Alert kind="error">{keys.error}</Alert> : (
          <ul className="divide-y divide-slate-100 text-sm">
            {keys.data?.length === 0 && <li className="py-2 text-slate-500">No keys on file.</li>}
            {keys.data?.map((k) => (
              <li key={k.id} className="flex items-center justify-between py-2">
                <span><span className="font-medium capitalize">{k.exchange}</span> <span className="text-slate-500">{String(k.api_key_masked ?? "")}</span> <StatusBadge status={k.verified ? "verified" : "pending"} /></span>
                <button className="btn-danger !py-1" onClick={() => void userAPI.deleteExchangeKey(k.id).then(() => keys.reload())}>Remove</button>
              </li>
            ))}
          </ul>
        )}
        {keyMsg && <Alert kind={keyMsg.kind}>{keyMsg.text}</Alert>}
        <form onSubmit={addKey} className="grid gap-3 sm:grid-cols-2">
          <div><label className="label">Exchange</label>
            <select className="input" value={exchange} onChange={(e) => setExchange(e.target.value)}><option value="binance">Binance</option><option value="bitget">Bitget</option><option value="bybit">Bybit</option><option value="okx">OKX</option></select></div>
          <div><label className="label">Passphrase (Bitget/OKX)</label><input className="input" value={passphrase} onChange={(e) => setPassphrase(e.target.value)} /></div>
          <div><label className="label">API key</label><input className="input" value={apiKey} onChange={(e) => setApiKey(e.target.value)} autoComplete="off" /></div>
          <div><label className="label">API secret</label><input className="input" type="password" value={apiSecret} onChange={(e) => setApiSecret(e.target.value)} autoComplete="off" /></div>
          <div className="sm:col-span-2"><button className="btn-primary" type="submit" disabled={!apiKey || !apiSecret}>Save key</button></div>
        </form>
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
    </div>
  );
}
