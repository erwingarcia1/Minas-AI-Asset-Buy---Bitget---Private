import { useId, useState } from "react";

export interface FaqItem { q: string; a: string }

/** Accordion: real buttons (Enter/Space toggle, Tab moves), aria-expanded and
 *  aria-controls on each header, panels hidden when closed. One open at a time. */
export function Faq({ items }: { items: FaqItem[] }) {
  const [open, setOpen] = useState<number | null>(null);
  const base = useId();
  return (
    <div className="divide-y divide-slate-200 rounded-lg border border-slate-200 bg-white" data-testid="faq">
      {items.map((item, i) => {
        const isOpen = open === i;
        const panelId = `${base}-panel-${i}`;
        const btnId = `${base}-btn-${i}`;
        return (
          <div key={item.q}>
            <h3 className="m-0">
              <button type="button" id={btnId} aria-expanded={isOpen} aria-controls={panelId}
                      onClick={() => setOpen(isOpen ? null : i)}
                      className="flex w-full items-center justify-between gap-4 px-4 py-3 text-left text-sm font-medium text-slate-900 hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand-500">
                <span>{item.q}</span>
                <span aria-hidden className={`text-brand-600 transition-transform ${isOpen ? "rotate-45" : ""}`}>+</span>
              </button>
            </h3>
            <div id={panelId} role="region" aria-labelledby={btnId} hidden={!isOpen} className="px-4 pb-4 text-sm text-slate-600">
              {item.a}
            </div>
          </div>
        );
      })}
    </div>
  );
}
