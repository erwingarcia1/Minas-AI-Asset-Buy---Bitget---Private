import { useState } from "react";

/** Copies `value` to the clipboard and confirms inline for two seconds. */
export function CopyButton({ value, label = "Copy" }: { value: string; label?: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setState("copied");
    } catch {
      setState("failed");
    }
    window.setTimeout(() => setState("idle"), 2000);
  }
  return (
    <button type="button" onClick={() => void copy()} data-testid="copy-button"
            className="btn-secondary !px-2 !py-0.5 text-xs" aria-live="polite" aria-label={`${label} ${value}`}>
      {state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : label}
    </button>
  );
}
