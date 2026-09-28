import { Link } from "react-router-dom";

/** Placeholder route. TODO(owner): legal text. Nothing here is legal advice or a real agreement. */
export default function Terms() {
  return (
    <main className="mx-auto max-w-2xl px-4 py-16">
      <h1 className="text-2xl font-semibold text-brand-ink">Terms of use</h1>
      <p className="mt-4 rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900" data-testid="legal-todo">
        TODO(owner): legal text. This page is a placeholder and carries no terms yet.
      </p>
      <p className="mt-6 text-sm"><Link className="text-brand-600 underline" to="/">Back to AutoBuy</Link></p>
    </main>
  );
}
