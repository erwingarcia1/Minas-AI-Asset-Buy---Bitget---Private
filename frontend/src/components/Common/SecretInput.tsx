import { useId, useState } from "react";
import { NO_AUTOFILL } from "@/utils/noAutofill";

/** A masked text input for an exchange secret. Deliberately NOT
 *  type="password": Chrome's password manager only captures and autofills
 *  real password inputs, so a text input masked with CSS never triggers the
 *  "Save password?" prompt and never receives a saved login. The show/hide
 *  button is a real button (keyboard reachable) with an accessible label. */
export default function SecretInput({ id, name, value, onChange, label, className = "" }: {
  id: string; name: string; value: string; onChange: (v: string) => void; label: string; className?: string;
}) {
  const [shown, setShown] = useState(false);
  const hint = useId();
  return (
    <div className="relative">
      <input
        id={id} name={name} type="text" value={value} onChange={(e) => onChange(e.target.value)}
        className={`input pr-16 ${shown ? "" : "secret-masked"} ${className}`}
        aria-describedby={hint}
        {...NO_AUTOFILL}
      />
      <span id={hint} className="sr-only">{shown ? `${label} is shown as typed.` : `${label} is hidden while you type.`}</span>
      <button
        type="button"
        className="absolute inset-y-0 right-0 px-3 text-xs font-medium text-slate-600 hover:text-slate-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 rounded-r-md"
        aria-pressed={shown}
        aria-label={shown ? `Hide ${label}` : `Show ${label}`}
        onClick={() => setShown((s) => !s)}
      >
        {shown ? "Hide" : "Show"}
      </button>
    </div>
  );
}
