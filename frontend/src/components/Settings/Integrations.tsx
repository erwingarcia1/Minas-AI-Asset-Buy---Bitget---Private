import { useState, type FormEvent } from "react";
import { integrationsAPI } from "@/api/endpoints";
import { errorMessage } from "@/api/client";
import { Alert, Loading } from "@/components/Common";
import { useAsync } from "@/hooks/useAsync";

export default function Integrations() {
  const { data, loading, error, reload } = useAsync(() =>
    Promise.all([integrationsAPI.available(), integrationsAPI.webhooks()]).then(([items, hooks]) => ({ items, hooks })));
  const [url, setUrl] = useState("");
  const [newSecret, setNewSecret] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function act(fn: () => Promise<unknown>) {
    setMsg(null); setBusy(true);
    try { await fn(); await reload(); } catch (err) { setMsg(errorMessage(err)); } finally { setBusy(false); }
  }

  async function addHook(e: FormEvent) {
    e.preventDefault();
    await act(async () => {
      const created = await integrationsAPI.addWebhook(url.trim());
      setNewSecret(created.secret);
      setUrl("");
    });
  }

  return (
    <section className="card space-y-4" aria-label="Integrations">
      <h2 className="font-semibold">Integrations</h2>
      {loading ? <Loading /> : error || !data ? <Alert kind="error">{error}</Alert> : (
        <>
          <div className="grid gap-2 sm:grid-cols-2">
            {data.items.map((i) => (
              <div key={i.name} className="rounded-md border border-slate-200 p-3 text-sm">
                <div className="flex items-center justify-between">
                  <b className="capitalize">{i.name}</b>
                  <span className={`badge ${i.status === "connected" ? "bg-green-100 text-green-800" : "bg-slate-100 text-slate-600"}`}>{i.status}</span>
                </div>
                <p className="mt-1 text-slate-500">{i.detail}</p>
              </div>
            ))}
          </div>

          <div>
            <h3 className="text-sm font-medium">Outbound webhooks</h3>
            <p className="text-xs text-slate-500">HMAC-signed POSTs on purchase events. HTTPS URLs only.</p>
          </div>
          {newSecret && (
            <Alert kind="warning">
              Signing secret (shown once, store it now): <code className="break-all font-mono text-xs">{newSecret}</code>
            </Alert>
          )}
          {msg && <Alert kind="error">{msg}</Alert>}
          <ul className="divide-y divide-slate-100 text-sm">
            {data.hooks.length === 0 && <li className="py-2 text-slate-500">No webhooks yet.</li>}
            {data.hooks.map((h) => (
              <li key={h.id} className="flex items-center gap-3 py-2">
                <span className="flex-1 truncate font-mono text-xs" title={h.url}>{h.url}</span>
                <span className="text-xs text-slate-500">{h.last_status ?? "never sent"}</span>
                <button className="btn-secondary !py-1" disabled={busy} onClick={() => void act(() => integrationsAPI.pingWebhook(h.id))}>Ping</button>
                <button className="btn-danger !py-1" disabled={busy} onClick={() => { if (confirm(`Remove webhook ${h.url}?`)) void act(() => integrationsAPI.removeWebhook(h.id)); }}>Remove</button>
              </li>
            ))}
          </ul>
          <form onSubmit={addHook} className="flex gap-2" noValidate>
            <label className="sr-only" htmlFor="wh-url">Webhook URL</label>
            <input id="wh-url" className="input" placeholder="https://your-server.example/hook" value={url} onChange={(e) => setUrl(e.target.value)} />
            <button className="btn-primary" type="submit" disabled={busy || !url.trim().startsWith("https://")}>Add</button>
          </form>
        </>
      )}
    </section>
  );
}
