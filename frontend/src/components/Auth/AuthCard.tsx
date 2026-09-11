import type { ReactNode } from "react";

export function AuthCard({ title, subtitle, children, footer }: { title: string; subtitle?: string; children: ReactNode; footer?: ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 p-6">
      <div className="w-full max-w-md">
        <div className="mb-6 flex items-center justify-center gap-2 text-xl font-semibold text-slate-900">
          <img src="/favicon.svg" alt="" className="h-8 w-8" /> AutoBuy
        </div>
        <div className="card">
          <h1 className="text-lg font-semibold">{title}</h1>
          {subtitle && <p className="mt-1 text-sm text-slate-500">{subtitle}</p>}
          <div className="mt-6">{children}</div>
        </div>
        {footer && <p className="mt-4 text-center text-sm text-slate-600">{footer}</p>}
      </div>
    </div>
  );
}
