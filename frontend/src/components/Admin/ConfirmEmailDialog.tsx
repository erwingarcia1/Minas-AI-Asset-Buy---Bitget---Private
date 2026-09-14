import { useEffect, useState, type FormEvent, type ReactNode } from "react";

interface Props {
  title: string;
  description: string;
  email: string;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  error?: string | null;
  extra?: ReactNode;   // extra fields rendered above the email confirmation
  onConfirm: (typed: string) => void;
  onCancel: () => void;
}

/** Modal that requires the user's email to be typed exactly before a destructive admin action. */
export function ConfirmEmailDialog({ title, description, email, confirmLabel, danger = true, busy = false, error, extra, onConfirm, onCancel }: Props) {
  const [typed, setTyped] = useState("");
  const matches = typed.trim() === email;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onCancel(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  function submit(e: FormEvent) {
    e.preventDefault();
    if (matches && !busy) onConfirm(typed.trim());
  }

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4" onClick={onCancel}>
      <form role="dialog" aria-modal="true" aria-labelledby="confirm-title" className="card w-full max-w-md space-y-4" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <h2 id="confirm-title" className="text-lg font-semibold">{title}</h2>
        <p className="text-sm text-slate-600">{description}</p>
        {extra}
        <div>
          <label className="label" htmlFor="confirm-email">Type <span className="font-mono">{email}</span> to confirm</label>
          <input id="confirm-email" className="input font-mono" autoComplete="off" spellCheck={false} value={typed} onChange={(e) => setTyped(e.target.value)} autoFocus />
        </div>
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        <div className="flex justify-end gap-2">
          <button type="button" className="btn-secondary" onClick={onCancel} disabled={busy}>Cancel</button>
          <button type="submit" className={danger ? "btn-danger" : "btn-primary"} disabled={!matches || busy}>{busy ? "Working…" : confirmLabel}</button>
        </div>
      </form>
    </div>
  );
}
