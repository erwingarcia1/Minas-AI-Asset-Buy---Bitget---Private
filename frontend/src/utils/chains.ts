import type { ChainFamily } from "@/types";
import { detectChain, getAddressNetwork, isDenylisted, DENYLIST_REASON } from "@/utils/addressValidation";

/** Exchange chain name -> address family (mirrors api/utils/chains.py). */
export const CHAIN_FAMILIES: Record<string, ChainFamily> = {
  btc: "bitcoin",
  erc20: "ethereum", bep20: "ethereum", arbitrumone: "ethereum", arbitrum: "ethereum", optimism: "ethereum",
  polygon: "ethereum", base: "ethereum", "avaxc-chain": "ethereum", avaxc: "ethereum", zksyncera: "ethereum",
  linea: "ethereum", scroll: "ethereum", opbnb: "ethereum", mantle: "ethereum", morph: "ethereum", plasma: "ethereum",
  sol: "solana",
  trc20: "tron",
};

export const DEFAULT_CHAIN_FOR_COIN: Record<string, string> = { BTC: "BTC", ETH: "ERC20", USDT: "ERC20", USDC: "ERC20", SOL: "SOL" };

export const familyForChain = (chain: string | null | undefined): ChainFamily | null =>
  chain ? CHAIN_FAMILIES[chain.trim().toLowerCase()] ?? null : null;

const TRON_RE = /^T[1-9A-HJ-NP-Za-km-z]{33}$/;

/** Form helper: null while empty, otherwise whether the address fits the chain's format. */
export function addressVerdictForChain(address: string, chain: string): { ok: boolean; reason: string } | null {
  const a = address.trim();
  if (!a) return null;
  if (isDenylisted(a)) return { ok: false, reason: DENYLIST_REASON };
  const family = familyForChain(chain);
  if (!family) return { ok: false, reason: `${chain} is not supported for withdrawals by this platform yet` };
  const detected = detectChain(a);
  if (family === "bitcoin") {
    if (detected !== "bitcoin") return { ok: false, reason: "Not a valid Bitcoin address" };
    if (getAddressNetwork(a) !== "mainnet") return { ok: false, reason: "Bitcoin testnet addresses cannot receive exchange withdrawals" };
    return { ok: true, reason: "Valid Bitcoin mainnet address" };
  }
  if (family === "ethereum") return detected === "ethereum" ? { ok: true, reason: `Valid EVM address for ${chain}` } : { ok: false, reason: `${chain} needs a 0x… address` };
  if (family === "solana") return detected === "solana" ? { ok: true, reason: "Valid Solana address" } : { ok: false, reason: "Not a valid Solana address" };
  return TRON_RE.test(a) ? { ok: true, reason: "Valid Tron address" } : { ok: false, reason: "TRC20 needs a T… Tron address" };
}

export const fmtCoin = (n: number, coin: string, digits = 8) => `${n.toLocaleString(undefined, { maximumFractionDigits: digits })} ${coin}`;
