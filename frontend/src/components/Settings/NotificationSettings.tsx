import { forwardRef, useState } from "react";
import { errorMessage } from "@/api/client";
import { Alert, Loading } from "@/components/Common";
import { useAsync } from "@/hooks/useAsync";
import type { NotificationPreferences } from "@/types";

interface Api {
  preferences: () => Promise<NotificationPreferences>;
  update: (body: Partial<NotificationPreferences>) => Promise<NotificationPreferences>;
}

const OPTIONAL: { key: "email_purchase_confirmations" | "email_weekly_summary" | "email_milestones"; label: string; help: string }[] = [
  { key: "email_purchase_confirmations", label: "Purchase confirmations", help: "One email per completed buy (and its withdrawal)." },
  { key: "email_weekly_summary", label: "Weekly summary", help: "Monday digest of what your automation did." },
  { key: "email_milestones", label: "Milestones", help: "First purchase and invested-amount milestones." },
];

/** Which emails the user gets. Security and failure emails are always sent. */
const NotificationSettings = forwardRef<HTMLElement, { api: Api; highlight?: boolean }>(function NotificationSettings({ api, highlight }, ref) {
  const prefs = useAsync(() => api.preferences());
  const [msg, setMsg] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [saving, setSaving] = useState<string | null>(null);

  async function toggle(key: (typeof OPTIONAL)[number]["key"], value: boolean) {
    setMsg(null); setSaving(key);
    try {
      prefs.setData(await api.update({ [key]: value }));
      setMsg({ kind: "success", text: value ? "Email switched on." : "Email switched off. Security and failure alerts are still sent." });
    } catch (e) { setMsg({ kind: "error", text: errorMessage(e, "Could not save your preference") }); }
    finally { setSaving(null); }
  }

  return (
    <section ref={ref} id="notifications" className={`card space-y-3 ${highlight ? "ring-2 ring-brand-500" : ""}`} aria-labelledby="notifications-title">
      <h2 id="notifications-title" className="font-semibold">Email notifications</h2>
      {prefs.error && <Alert kind="error">{prefs.error}</Alert>}
      {prefs.loading && !prefs.data ? <Loading /> : prefs.data && (
        <>
          <ul className="space-y-2">
            {OPTIONAL.map((o) => (
              <li key={o.key} className="flex items-start gap-3">
                <input id={`notif-${o.key}`} type="checkbox" className="mt-1 h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500"
                       checked={prefs.data![o.key]} disabled={saving !== null} onChange={(e) => void toggle(o.key, e.target.checked)} />
                <label htmlFor={`notif-${o.key}`} className="text-sm"><span className="font-medium text-slate-900">{o.label}</span><span className="block text-slate-500">{o.help}</span></label>
              </li>
            ))}
          </ul>
          <p className="text-xs text-slate-500" data-testid="always-on">
            Always sent, cannot be switched off: {prefs.data.always_on_emails.join(", ")}.
          </p>
        </>
      )}
      {msg && <Alert kind={msg.kind}>{msg.text}</Alert>}
    </section>
  );
});

export default NotificationSettings;
