import { forwardRef, useEffect, useState, type FormEvent } from "react";
import QRCode from "qrcode";
import { twoFactorAPI } from "@/api/endpoints";
import { errorMessage } from "@/api/client";
import { Alert, CopyButton, Loading } from "@/components/Common";
import { useAsync } from "@/hooks/useAsync";
import { useAuth } from "@/hooks/useAuth";
import type { MfaFactor, TwoFactorEnrolment } from "@/types";
import { saveBlob } from "@/utils/download";
import { fmtDate } from "@/utils/format";
import { RECOVERY_CODE_MAX_LENGTH } from "@/utils/validation";

type Msg = { kind: "error" | "success" | "info"; text: string } | null;
type View = "idle" | "password" | "enrolling" | "codes" | "regenerate" | "disable" | "sign-out-everywhere";

/** Renders the otpauth:// URI as an inline SVG, client-side; the secret never leaves the page. */
function qrSvg(uri: string): Promise<string> {
  return QRCode.toString(uri, { type: "svg", margin: 1, width: 180 });
}

function factorFrom(code: string, useRecovery: boolean): MfaFactor {
  const typed = code.replace(/\s+/g, "");
  return useRecovery ? { recovery_code: typed } : { code: typed };
}

/** Two-factor authentication and "sign out everywhere". */
const SecuritySettings = forwardRef<HTMLElement, { highlight?: boolean }>(function SecuritySettings({ highlight }, ref) {
  const { signOutEverywhere } = useAuth();
  const status = useAsync(() => twoFactorAPI.status());
  const [view, setView] = useState<View>("idle");
  const [msg, setMsg] = useState<Msg>(null);
  const [busy, setBusy] = useState(false);
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [useRecovery, setUseRecovery] = useState(false);
  const [enrolment, setEnrolment] = useState<TwoFactorEnrolment | null>(null);
  const [qr, setQr] = useState<string | null>(null);
  const [codes, setCodes] = useState<string[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (enrolment) qrSvg(enrolment.otpauth_uri).then((svg) => { if (!cancelled) setQr(svg); }).catch(() => { if (!cancelled) setQr(null); });
    else setQr(null);
    return () => { cancelled = true; };
  }, [enrolment]);

  function reset(next: View = "idle") {
    setView(next); setPassword(""); setCode(""); setUseRecovery(false); setMsg(null);
  }

  async function run(action: () => Promise<void>) {
    setBusy(true); setMsg(null);
    try { await action(); }
    catch (err) { setMsg({ kind: "error", text: errorMessage(err) }); }
    finally { setBusy(false); }
  }

  function startEnrolment(e: FormEvent) {
    e.preventDefault();
    void run(async () => {
      setEnrolment(await twoFactorAPI.enrol(password));
      setPassword(""); setView("enrolling");
    });
  }

  function confirmEnrolment(e: FormEvent) {
    e.preventDefault();
    void run(async () => {
      const r = await twoFactorAPI.confirm(code.replace(/\s+/g, ""));
      setCodes(r.recovery_codes); setEnrolment(null); setCode(""); setView("codes");
      setMsg({ kind: "success", text: "Two-factor authentication is on. Save your recovery codes now: they are shown only once." });
      await status.reload();
    });
  }

  function regenerate(e: FormEvent) {
    e.preventDefault();
    void run(async () => {
      const r = await twoFactorAPI.regenerateCodes(code.replace(/\s+/g, ""));
      setCodes(r.recovery_codes); setCode(""); setView("codes");
      setMsg({ kind: "success", text: "New recovery codes. The previous ones no longer work." });
      await status.reload();
    });
  }

  function disable(e: FormEvent) {
    e.preventDefault();
    void run(async () => {
      await twoFactorAPI.disable(password, factorFrom(code, useRecovery));
      reset("idle");
      setMsg({ kind: "success", text: "Two-factor authentication is off. Signing in now needs only your password." });
      await status.reload();
    });
  }

  function downloadCodes() {
    if (!codes) return;
    saveBlob(new Blob([codes.join("\n") + "\n"], { type: "text/plain" }), "autobuy-recovery-codes.txt");
  }

  const s = status.data;
  const enabled = s?.enabled === true;

  return (
    <section ref={ref} id="security" className={`card space-y-4 ${highlight ? "ring-2 ring-brand-500" : ""}`} aria-labelledby="security-title">
      <h2 id="security-title" className="font-semibold">Security</h2>
      {status.error && <Alert kind="error">{status.error}</Alert>}
      {msg && <Alert kind={msg.kind}>{msg.text}</Alert>}
      {status.loading && !s ? <Loading /> : s && (
        <>
          <div className="text-sm" data-testid="two-factor-status">
            <p className="font-medium text-slate-900">Two-factor authentication: {enabled ? "on" : "off"}</p>
            <p className="text-slate-500">
              {enabled
                ? <>On since {s.enrolled_at ? fmtDate(s.enrolled_at) : "—"}. {s.recovery_codes_remaining} recovery code{s.recovery_codes_remaining === 1 ? "" : "s"} left.</>
                : "Signing in asks for a code from an authenticator app on top of your password. It protects your purchases and exchange keys if your password leaks."}
            </p>
          </div>

          {view === "idle" && (
            <div className="flex flex-wrap gap-2">
              {!enabled && <button className="btn-primary" type="button" onClick={() => reset("password")}>Set up two-factor authentication</button>}
              {enabled && <button className="btn-secondary" type="button" onClick={() => reset("regenerate")}>New recovery codes</button>}
              {enabled && <button className="btn-secondary" type="button" onClick={() => reset("disable")}>Turn off two-factor</button>}
              <button className="btn-secondary" type="button" onClick={() => reset("sign-out-everywhere")}>Sign out everywhere</button>
            </div>
          )}

          {view === "password" && (
            <form onSubmit={startEnrolment} className="space-y-3" data-testid="enrol-password">
              <p className="text-sm text-slate-600">Confirm your password to start. You will need an authenticator app (Google Authenticator, Authy, 1Password…).</p>
              <div><label className="label" htmlFor="tf-password">Current password</label>
                <input id="tf-password" className="input" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} /></div>
              <div className="flex gap-2">
                <button className="btn-primary" type="submit" disabled={busy || !password}>Continue</button>
                <button className="btn-secondary" type="button" onClick={() => reset()}>Cancel</button>
              </div>
            </form>
          )}

          {view === "enrolling" && enrolment && (
            <form onSubmit={confirmEnrolment} className="space-y-3" data-testid="enrol-confirm">
              <p className="text-sm text-slate-600">Scan the QR code with your authenticator app, or type the secret into it, then enter the six-digit code it shows.</p>
              <div className="flex flex-wrap items-start gap-4">
                {qr ? <div className="rounded border border-slate-200 bg-white p-2" aria-hidden="true" data-testid="qr" dangerouslySetInnerHTML={{ __html: qr }} />
                    : <div className="h-44 w-44 rounded border border-dashed border-slate-300" aria-hidden="true" />}
                <div className="space-y-2 text-sm">
                  <p className="text-slate-500">Secret (if you cannot scan)</p>
                  <p className="flex flex-wrap items-center gap-2"><code className="rounded bg-slate-100 px-2 py-1 font-mono text-xs" data-testid="totp-secret">{enrolment.secret}</code>
                    <CopyButton value={enrolment.secret} label="Copy secret" /></p>
                </div>
              </div>
              <div><label className="label" htmlFor="tf-confirm-code">Code from the app</label>
                <input id="tf-confirm-code" className="input font-mono tracking-widest" type="text" inputMode="numeric" autoComplete="one-time-code" maxLength={7}
                       value={code} onChange={(e) => setCode(e.target.value)} placeholder="123456" /></div>
              <div className="flex gap-2">
                <button className="btn-primary" type="submit" disabled={busy || code.replace(/\s+/g, "").length !== 6}>Turn on</button>
                <button className="btn-secondary" type="button" onClick={() => { setEnrolment(null); reset(); }}>Cancel</button>
              </div>
            </form>
          )}

          {view === "codes" && codes && (
            <div className="space-y-3" data-testid="recovery-codes">
              <p className="text-sm text-slate-600">Each code signs you in once if you lose your authenticator. Keep them somewhere safe; they cannot be shown again.</p>
              <ul className="grid grid-cols-2 gap-1 font-mono text-sm sm:grid-cols-5">
                {codes.map((c) => <li key={c} className="rounded bg-slate-100 px-2 py-1 text-center">{c}</li>)}
              </ul>
              <div className="flex flex-wrap gap-2">
                <CopyButton value={codes.join("\n")} label="Copy all" />
                <button className="btn-secondary !px-2 !py-0.5 text-xs" type="button" onClick={downloadCodes}>Download</button>
                <button className="btn-primary !px-3 !py-1 text-xs" type="button" onClick={() => { setCodes(null); reset(); }}>I have saved them</button>
              </div>
            </div>
          )}

          {view === "regenerate" && (
            <form onSubmit={regenerate} className="space-y-3" data-testid="regenerate-form">
              <p className="text-sm text-slate-600">Enter the current code from your authenticator app. The previous recovery codes stop working.</p>
              <div><label className="label" htmlFor="tf-regen-code">Authenticator code</label>
                <input id="tf-regen-code" className="input font-mono tracking-widest" type="text" inputMode="numeric" autoComplete="one-time-code" maxLength={7}
                       value={code} onChange={(e) => setCode(e.target.value)} /></div>
              <div className="flex gap-2">
                <button className="btn-primary" type="submit" disabled={busy || code.replace(/\s+/g, "").length !== 6}>Generate new codes</button>
                <button className="btn-secondary" type="button" onClick={() => reset()}>Cancel</button>
              </div>
            </form>
          )}

          {view === "disable" && (
            <form onSubmit={disable} className="space-y-3" data-testid="disable-form">
              <p className="text-sm text-slate-600">Confirm your password and a code to turn two-factor off. You will be emailed.</p>
              <div><label className="label" htmlFor="tf-disable-password">Current password</label>
                <input id="tf-disable-password" className="input" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} /></div>
              <div><label className="label" htmlFor="tf-disable-code">{useRecovery ? "Recovery code" : "Authenticator code"}</label>
                <input id="tf-disable-code" className="input font-mono tracking-widest" type="text" inputMode={useRecovery ? "text" : "numeric"} autoComplete="one-time-code"
                       maxLength={useRecovery ? RECOVERY_CODE_MAX_LENGTH : 7} value={code} onChange={(e) => setCode(e.target.value)} />
                <button type="button" className="mt-1 text-xs text-brand-600 hover:underline" onClick={() => { setUseRecovery((v) => !v); setCode(""); }}>
                  {useRecovery ? "Use my authenticator app instead" : "Use a recovery code instead"}
                </button></div>
              <div className="flex gap-2">
                <button className="btn-danger" type="submit" disabled={busy || !password || !code}>Turn off two-factor</button>
                <button className="btn-secondary" type="button" onClick={() => reset()}>Cancel</button>
              </div>
            </form>
          )}

          {view === "sign-out-everywhere" && (
            <div className="space-y-3" data-testid="sign-out-everywhere">
              <p className="text-sm text-slate-600">Every device signed in to this account is signed out, this one included. Use it if a session may have been left open somewhere.</p>
              <div className="flex gap-2">
                <button className="btn-danger" type="button" disabled={busy} onClick={() => void run(signOutEverywhere)}>Sign out everywhere</button>
                <button className="btn-secondary" type="button" onClick={() => reset()}>Cancel</button>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
});

export default SecuritySettings;
