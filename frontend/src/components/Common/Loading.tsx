export function Loading({ label = "Loading…", full = false }: { label?: string; full?: boolean }) {
  const spinner = (
    <div className="flex items-center gap-3 text-slate-500" role="status" aria-live="polite">
      <span className="h-5 w-5 animate-spin rounded-full border-2 border-slate-300 border-t-brand-600" />
      <span className="text-sm">{label}</span>
    </div>
  );
  return full ? <div className="flex min-h-screen items-center justify-center">{spinner}</div> : <div className="py-10 flex justify-center">{spinner}</div>;
}
