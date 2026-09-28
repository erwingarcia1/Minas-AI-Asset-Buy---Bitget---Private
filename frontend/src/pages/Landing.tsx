import { Link } from "react-router-dom";
import { publicAPI } from "@/api/endpoints";
import { BetaTicker, CopyButton } from "@/components/Common";
import { Faq, type FaqItem } from "@/components/Landing/Faq";
import { FlowAnimation } from "@/components/Landing/FlowAnimation";
import { SUPPORT_EMAIL_FALLBACK } from "@/config/beta";
import { useAsync } from "@/hooks/useAsync";
import { assetColor } from "@/utils/assetColors";
import { AUTOBUY_CRYPTOS } from "@/utils/exchangeNetworks";
import "./landing.css";

const FAQ: FaqItem[] = [
  { q: "What if the sub-account has no money?",
    a: "The buy fails with \"insufficient balance\". Each buy is tried three times, five minutes apart. A recurring purchase then skips that cycle and tries again next time; it stops after three missed cycles in a row. A one-time purchase stops after its three attempts." },
  { q: "Where are my coins?",
    a: "In your Bitget sub-account. Move them whenever you like. AutoBuy's holdings count only what it bought for you, so they won't reflect coins you move or add yourself." },
  { q: "Can the AutoBuy team trade on my account?",
    a: "The team cannot place new trades or change anything on your exchange. For support, an admin can re-run a purchase you created that failed." },
  { q: "Do I need Profit Guard or Equity Guard?", a: "No. Those belong to CoinTech, a separate product." },
  { q: "Can I stop a purchase?", a: "Yes. Pause or delete it at any time." },
  { q: "Can I connect a bank account?", a: "No, only your exchange, so AutoBuy never holds your funds." },
  { q: "Which exchanges are supported?", a: "Bitget only, for now." },
];

const SAFETY = [
  "AutoBuy never holds your money. Funds stay on Bitget.",
  "Trade-only key: no withdrawal permission needed for buys kept on Bitget; sending coins to your own wallet needs Withdraw permission.",
  "You restrict the key to our server's IP on Bitget, so it works from nowhere else.",
  "Keys are encrypted at rest.",
  "No bank account connection.",
  "You can pause or delete any purchase at any time.",
];

function Buttons({ mailto, size = "" }: { mailto: string; size?: string }) {
  return (
    <div className="flex flex-wrap gap-3">
      <a className={`btn-primary ${size}`} href={mailto}>Request access</a>
      <Link className={`btn-secondary ${size}`} to="/login">Log in</Link>
    </div>
  );
}

export default function Landing() {
  const meta = useAsync(() => publicAPI.meta().catch(() => null));
  const supportEmail = meta.data?.support_email || SUPPORT_EMAIL_FALLBACK;
  const mailto = `mailto:${supportEmail}?subject=${encodeURIComponent("AutoBuy access request")}`;
  const coins = meta.data?.coins?.length ? meta.data.coins : [...AUTOBUY_CRYPTOS];
  const serverIp = meta.data?.server_ip ?? "";
  const year = new Date().getFullYear();

  return (
    <div className="min-h-screen bg-white text-slate-900">
      <BetaTicker />
      <header className="sticky top-0 z-20 border-b border-slate-200 bg-white/95 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 py-3">
          <Link to="/" className="flex items-center gap-2 font-semibold text-brand-ink" aria-label="AutoBuy home">
            <img src="/icon-64.png" alt="" width={28} height={28} className="h-7 w-7" data-testid="brand-mark" /> AutoBuy
          </Link>
          <nav className="hidden items-center gap-5 text-sm text-slate-700 md:flex" aria-label="Page sections">
            <a className="hover:text-brand-600" href="#how-it-works">How it works</a>
            <a className="hover:text-brand-600" href="#safety">Safety</a>
            <a className="hover:text-brand-600" href="#faq">FAQ</a>
          </nav>
          <div className="flex items-center gap-2">
            <Link className="btn-secondary !px-3 !py-1.5" to="/login">Log in</Link>
            <a className="btn-primary !px-3 !py-1.5" href={mailto}>Request access</a>
          </div>
        </div>
      </header>

      <main>
        <section className="mx-auto max-w-6xl px-4 pb-12 pt-14 md:pt-20" aria-labelledby="hero-title">
          <span className="inline-flex items-center rounded-full border border-brand-200 bg-brand-50 px-3 py-1 text-xs font-medium text-brand-800" data-testid="beta-badge">
            Private beta · by invitation
          </span>
          <h1 id="hero-title" className="mt-4 max-w-3xl text-3xl font-semibold leading-tight text-brand-ink md:text-5xl" style={{ textWrap: "balance" }}>
            Buy crypto on a schedule, on your own Bitget account.
          </h1>
          <p className="mt-4 max-w-2xl text-lg text-slate-600">
            Your coins never leave your exchange. AutoBuy places the orders; Bitget holds the funds.
          </p>
          <div className="mt-8"><Buttons mailto={mailto} size="!px-5 !py-2.5 text-base" /></div>
        </section>

        <section id="how-it-works" className="border-t border-slate-100 bg-slate-50 py-14" aria-labelledby="how-title">
          <div className="mx-auto max-w-6xl px-4">
            <h2 id="how-title" className="text-2xl font-semibold text-brand-ink">How it works</h2>
            <div className="mt-8 rounded-xl border border-slate-200 bg-white p-4 md:p-6">
              <FlowAnimation />
            </div>
            <ol className="mt-8 grid gap-6 md:grid-cols-3">
              {[
                "Connect your Bitget sub-account with a trade-only API key.",
                "Choose the coin, amount and schedule: one-time, daily or weekly.",
                "AutoBuy places the buy on time, and the coins land in your sub-account.",
              ].map((step, i) => (
                <li key={step} className="flex gap-4">
                  <span aria-hidden className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand-600 text-sm font-semibold text-white">{i + 1}</span>
                  <p className="text-slate-700">{step}</p>
                </li>
              ))}
            </ol>
          </div>
        </section>

        <section id="safety" className="py-14" aria-labelledby="safety-title">
          <div className="mx-auto max-w-6xl px-4">
            <h2 id="safety-title" className="text-2xl font-semibold text-brand-ink">Safety: non-custodial by design</h2>
            <ul className="mt-6 grid gap-4 md:grid-cols-2">
              {SAFETY.map((line) => (
                <li key={line} className="flex gap-3 rounded-lg border border-slate-200 p-4">
                  <span aria-hidden className="mt-1 h-2.5 w-2.5 shrink-0 rounded-full bg-brand-600" />
                  <p className="text-slate-700">{line}</p>
                </li>
              ))}
            </ul>
          </div>
        </section>

        <section className="border-t border-slate-100 bg-slate-50 py-14" aria-labelledby="coins-title">
          <div className="mx-auto grid max-w-6xl gap-10 px-4 md:grid-cols-2">
            <div>
              <h2 id="coins-title" className="text-2xl font-semibold text-brand-ink">Supported coins</h2>
              <ul className="mt-5 flex flex-wrap gap-2" data-testid="coin-list">
                {coins.map((c) => (
                  <li key={c} className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-sm font-medium">
                    <span aria-hidden className="h-3 w-3 rounded-full" style={{ background: assetColor(c) }} />{c}
                  </li>
                ))}
              </ul>
              <p className="mt-3 text-xs text-slate-500">Exchanges: {(meta.data?.exchanges?.length ? meta.data.exchanges : ["bitget"]).map((e) => e === "bitget" ? "Bitget" : e).join(", ")}</p>
            </div>
            <div>
              <h2 className="text-2xl font-semibold text-brand-ink">Fees</h2>
              <p className="mt-5 text-slate-700">
                AutoBuy uses your Bitget account, so Bitget's own fees apply: a trading fee on each buy, taken in the coin you receive,
                and a network fee if you withdraw to your own wallet. Minimum order sizes are set by Bitget.
              </p>
            </div>
          </div>
        </section>

        <section id="setup" className="py-14" aria-labelledby="setup-title">
          <div className="mx-auto max-w-6xl px-4">
            <h2 id="setup-title" className="text-2xl font-semibold text-brand-ink">Before you start</h2>
            <ol className="mt-6 space-y-4">
              {[
                <>Use an existing Bitget sub-account or create one.</>,
                <>Create an API key on that sub-account with <b>Spot → Trade</b> only.</>,
                <>Passphrase: letters and numbers only, no spaces or special characters, and meet Bitget's minimum length.</>,
                <>Whitelist our IP on the key:{" "}
                  {serverIp ? (
                    <span className="inline-flex flex-wrap items-center gap-2 align-middle">
                      <code className="rounded bg-slate-100 px-2 py-0.5 font-mono text-sm" data-testid="server-ip">{serverIp}</code>
                      <CopyButton value={serverIp} label="Copy IP" />
                    </span>
                  ) : (
                    <span className="text-slate-500" data-testid="server-ip-missing">the IP will be shown here once configured.</span>
                  )}
                </>,
                <>Move USDT to the sub-account's <b>spot</b> wallet (not futures).</>,
              ].map((step, i) => (
                <li key={i} className="flex gap-4">
                  <span aria-hidden className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-brand-300 text-sm font-semibold text-brand-700">{i + 1}</span>
                  <p className="text-slate-700">{step}</p>
                </li>
              ))}
            </ol>
          </div>
        </section>

        <section id="faq" className="border-t border-slate-100 bg-slate-50 py-14" aria-labelledby="faq-title">
          <div className="mx-auto max-w-3xl px-4">
            <h2 id="faq-title" className="text-2xl font-semibold text-brand-ink">Questions</h2>
            <div className="mt-6"><Faq items={FAQ} /></div>
          </div>
        </section>

        <section className="py-14" aria-labelledby="cta-title">
          <div className="mx-auto max-w-6xl px-4">
            <h2 id="cta-title" className="text-2xl font-semibold text-brand-ink">Ready to automate your buying?</h2>
            <div className="mt-6"><Buttons mailto={mailto} /></div>
          </div>
        </section>
      </main>

      <footer className="border-t border-slate-200 bg-slate-50 py-10 text-sm text-slate-600">
        <div className="mx-auto max-w-6xl space-y-4 px-4">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
            <span>Support: <a className="text-brand-600 underline" href={`mailto:${supportEmail}`}>{supportEmail}</a></span>
            <Link className="text-brand-600 underline" to="/terms">Terms</Link>
            <Link className="text-brand-600 underline" to="/privacy">Privacy</Link>
            <span>A Minas Academy product</span>
          </div>
          <p className="max-w-3xl text-xs text-slate-500">
            Crypto prices are volatile and you can lose money. AutoBuy is a tool for placing orders on your own account, not financial advice. Private beta.
          </p>
          <p className="text-xs text-slate-500">© {year} Minas Academy</p>
        </div>
      </footer>
    </div>
  );
}
