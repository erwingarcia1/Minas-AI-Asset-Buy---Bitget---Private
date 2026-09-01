import { useCallback, useEffect, useState } from "react";
import { api } from "../services/api";

interface Integration { name: string; kind: string; status: string; detail: string }
interface Webhook { id: string; url: string; enabled: boolean; last_status?: string | null }

export default function Integrations() {
  const [items, setItems] = useState<Integration[]>([]);
  const [hooks, setHooks] = useState<Webhook[]>([]);
  const [url, setUrl] = useState("");
  const [newSecret, setNewSecret] = useState<string | null>(null);

  const load = useCallback(() => {
    api.get<Integration[]>("/api/integrations/available").then((r) => setItems(r.data));
    api.get<Webhook[]>("/api/integrations/webhooks").then((r) => setHooks(r.data));
  }, []);
  useEffect(load, [load]);

  async function addHook() {
    const { data } = await api.post<Webhook & { secret: string }>(
      "/api/integrations/webhooks", { url });
    setNewSecret(data.secret);
    setUrl("");
    load();
  }

  return (
    <section className="rounded-lg bg-white p-4 shadow-sm">
      <h2 className="mb-3 font-medium">Integrations</h2>
      <div className="grid gap-2 sm:grid-cols-2">
        {items.map((i) => (
          <div key={i.name} className="rounded border p-3 text-sm">
            <div className="flex items-center justify-between">
              <b className="capitalize">{i.name}</b>
              <span className={`rounded-full px-2 text-xs ${
                i.status === "connected" ? "bg-teal-50 text-teal-700" : "bg-gray-100 text-gray-500"}`}>
                {i.status}
              </span>
            </div>
            <p className="mt-1 text-gray-500">{i.detail}</p>
          </div>
        ))}
      </div>

      <h3 className="mt-4 text-sm font-medium">Outbound webhooks</h3>
      {newSecret && (
        <p className="mt-1 rounded bg-amber-50 p-2 text-xs text-amber-800">
          Signing secret (shown once): <code className="break-all">{newSecret}</code>
        </p>
      )}
      <ul className="mt-2 space-y-1 text-sm">
        {hooks.map((h) => (
          <li key={h.id} className="flex items-center gap-2">
            <span className="flex-1 truncate">{h.url}</span>
            <span className="text-xs text-gray-500">{h.last_status ?? "never sent"}</span>
            <button className="text-xs text-blue-600"
                    onClick={() => api.post(`/api/integrations/webhooks/${h.id}/ping`).then(load)}>
              Ping
            </button>
            <button className="text-xs text-red-600"
                    onClick={() => api.delete(`/api/integrations/webhooks/${h.id}`).then(load)}>
              Remove
            </button>
          </li>
        ))}
      </ul>
      <div className="mt-2 flex gap-2">
        <input value={url} onChange={(e) => setUrl(e.target.value)}
               placeholder="https://your-server.example/hook"
               className="flex-1 rounded border p-2 text-sm" />
        <button onClick={addHook} disabled={!url.startsWith("https://")}
                className="rounded bg-blue-600 px-3 py-2 text-sm text-white disabled:opacity-50">
          Add
        </button>
      </div>
    </section>
  );
}
