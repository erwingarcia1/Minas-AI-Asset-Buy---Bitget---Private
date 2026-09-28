import { Link } from "react-router-dom";
import { BetaTicker } from "@/components/Common/BetaTicker";

/** Placeholder route while the private beta runs. TODO(owner): legal text. */
export default function Privacy() {
  return (
    <><BetaTicker /><main className="mx-auto max-w-2xl px-4 py-16">
      <h1 className="text-2xl font-semibold text-brand-ink">Privacy</h1>
      {/* TODO(owner): legal text. Placeholder wording only; no legal text is written here. */}
      <p className="mt-4 text-slate-700" data-testid="legal-todo">
        The privacy policy for AutoBuy is being finalised for the private beta. Email support with any question.
      </p>
      <p className="mt-6 text-sm"><Link className="text-brand-600 underline" to="/">Back to AutoBuy</Link></p>
    </main></>
  );
}
