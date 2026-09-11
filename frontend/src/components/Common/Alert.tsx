const styles = {
  error: "border-red-200 bg-red-50 text-red-800",
  success: "border-green-200 bg-green-50 text-green-800",
  info: "border-brand-200 bg-brand-50 text-brand-800",
  warning: "border-amber-200 bg-amber-50 text-amber-800",
} as const;

export function Alert({ kind = "info", children }: { kind?: keyof typeof styles; children: React.ReactNode }) {
  if (!children) return null;
  return <div className={`rounded-md border px-4 py-3 text-sm ${styles[kind]}`} role={kind === "error" ? "alert" : "status"}>{children}</div>;
}
