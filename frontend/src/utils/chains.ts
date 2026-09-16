import { fmtAmount } from "@/utils/format";
import type { ChainFamily } from "@/types";
import { detectChain, getAddressNetwork, isDenylisted, sha256, DENYLIST_REASON } from "@/utils/addressValidation";

/** Exchange chain name -> address family (mirrors api/utils/chains.py). */
export const CHAIN_FAMILIES: Record<string, ChainFamily> = {
  btc: "bitcoin",
  erc20: "ethereum", bep20: "ethereum", arbitrumone: "ethereum", arbitrum: "ethereum", optimism: "ethereum",
  polygon: "ethereum", base: "ethereum", "avaxc-chain": "ethereum", avaxc: "ethereum", zksyncera: "ethereum",
  linea: "ethereum", scroll: "ethereum", opbnb: "ethereum", mantle: "ethereum", morph: "ethereum", plasma: "ethereum",
  sol: "solana",
  trc20: "tron",
  xrp: "xrpl",
  stellarlumens: "stellar",
};

/** Chains whose exchange deposits need a second field, and what it is called (mirrors TAG_CHAINS). */
export const TAG_CHAINS: Partial<Record<ChainFamily, string>> = { xrpl: "destination tag", stellar: "memo" };
export const tagLabelForChain = (chain: string | null | undefined): string | null => {
  const f = familyForChain(chain);
  return f ? TAG_CHAINS[f] ?? null : null;
};

export const DEFAULT_CHAIN_FOR_COIN: Record<string, string> = { BTC: "BTC", ETH: "ERC20", USDC: "ERC20", SOL: "SOL", XRP: "XRP", XLM: "StellarLumens" };

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
  if (family === "xrpl") { const p = parseXrpAddress(a); return p.ok ? { ok: true, reason: p.reason } : { ok: false, reason: p.reason }; }
  if (family === "stellar") { const p = parseStellarAddress(a); return p.ok ? { ok: true, reason: p.reason } : { ok: false, reason: p.reason }; }
  return TRON_RE.test(a) ? { ok: true, reason: "Valid Tron address" } : { ok: false, reason: "TRC20 needs a T… Tron address" };
}

// ---- XRP Ledger (mirrors api/utils/chains.parse_xrp_address; checksums verified against the live ledger 2026-09-16)
const XRP_B58 = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz";
const XRP_B58_INDEX = new Map([...XRP_B58].map((c, i) => [c, i]));

function xrpB58Decode(s: string): Uint8Array | null {
  if (!s) return null;
  let n = 0n;
  for (const c of s) {
    const v = XRP_B58_INDEX.get(c);
    if (v === undefined) return null;
    n = n * 58n + BigInt(v);
  }
  const bytes: number[] = [];
  while (n > 0n) { bytes.unshift(Number(n & 0xffn)); n >>= 8n; }
  let pad = 0;
  for (const c of s) { if (c === "r") pad++; else break; }
  return new Uint8Array([...new Array(pad).fill(0), ...bytes]);
}

function xrpB58Encode(bytes: Uint8Array): string {
  let n = 0n;
  for (const b of bytes) n = (n << 8n) + BigInt(b);
  let out = "";
  while (n > 0n) { out = XRP_B58[Number(n % 58n)] + out; n /= 58n; }
  let pad = 0;
  for (const b of bytes) { if (b === 0) pad++; else break; }
  return "r".repeat(pad) + out;
}

export interface TaggedAddress { ok: boolean; reason: string; classic: string | null; tag: string | null }

/** Classic r… address (20-byte account id, version 0) or X-address (version 0x05 0x44, tag inside); Base58Check with double SHA-256. */
export function parseXrpAddress(a: string): TaggedAddress {
  const bad = (reason: string): TaggedAddress => ({ ok: false, reason, classic: null, tag: null });
  const raw = xrpB58Decode(a);
  if (!raw || raw.length < 5) return bad("Not a valid XRP Ledger address");
  const payload = raw.slice(0, -4), check = raw.slice(-4);
  const digest = sha256(sha256(payload));
  if (!check.every((b, i) => b === digest[i])) return bad("Not a valid XRP Ledger address (bad length or checksum)");
  if (payload.length === 21 && payload[0] === 0x00) return { ok: true, reason: "Valid XRP Ledger address", classic: a, tag: null };
  if (payload.length === 31 && payload[0] === 0x05 && payload[1] === 0x44) {
    const account = payload.slice(2, 22), flag = payload[22];
    let tag = 0n;
    for (let i = 7; i >= 0; i--) tag = (tag << 8n) + BigInt(payload[23 + i]);
    const classicPayload = new Uint8Array([0x00, ...account]);
    const classicCheck = sha256(sha256(classicPayload)).slice(0, 4);
    const classic = xrpB58Encode(new Uint8Array([...classicPayload, ...classicCheck]));
    if (flag === 0) return { ok: true, reason: "Valid X-address (no tag)", classic, tag: null };
    if (flag === 1) return { ok: true, reason: `Valid X-address with destination tag ${tag}`, classic, tag: String(tag) };
    return bad("X-address with an unsupported tag type");
  }
  if (payload[0] === 0x04 && payload[1] === 0x93) return bad("This is an XRP Ledger TESTNET X-address; exchange withdrawals go to mainnet");
  return bad("Not a valid XRP Ledger address");
}

/** XRP destination tag: unsigned 32-bit integer written in digits. */
export function xrpTagVerdict(tag: string): { ok: boolean; reason: string } {
  const t = tag.trim();
  if (!/^\d{1,10}$/.test(t)) return { ok: false, reason: "A destination tag is a number of at most 10 digits" };
  if (BigInt(t) > 4294967295n) return { ok: false, reason: "A destination tag is at most 4294967295" };
  return { ok: true, reason: "ok" };
}

// ---- Stellar (mirrors api/utils/chains.parse_stellar_address): StrKey base32, CRC16-XModem little-endian
const B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
const B32_INDEX = new Map([...B32].map((c, i) => [c, i]));

function b32decode(s: string): Uint8Array | null {
  let bits = 0, value = 0;
  const out: number[] = [];
  for (const c of s) {
    const v = B32_INDEX.get(c);
    if (v === undefined) return null;
    value = (value << 5) | v; bits += 5;
    if (bits >= 8) { out.push((value >>> (bits - 8)) & 0xff); bits -= 8; }
  }
  return new Uint8Array(out);
}

function b32encode(bytes: Uint8Array): string {
  let bits = 0, value = 0, out = "";
  for (const b of bytes) {
    value = (value << 8) | b; bits += 8;
    while (bits >= 5) { out += B32[(value >>> (bits - 5)) & 31]; bits -= 5; }
  }
  if (bits > 0) out += B32[(value << (5 - bits)) & 31];
  return out;
}

function crc16xmodem(data: Uint8Array): number {
  let crc = 0;
  for (const b of data) {
    crc ^= b << 8;
    for (let i = 0; i < 8; i++) crc = crc & 0x8000 ? ((crc << 1) ^ 0x1021) & 0xffff : (crc << 1) & 0xffff;
  }
  return crc;
}

/** G… account (56 chars) or M… muxed account (69 chars, memo id inside). */
export function parseStellarAddress(a: string): TaggedAddress {
  const bad = (reason: string): TaggedAddress => ({ ok: false, reason, classic: null, tag: null });
  if (!/^[A-Z2-7]+$/.test(a)) return bad("Not a valid Stellar address");
  if (!((a.startsWith("G") && a.length === 56) || (a.startsWith("M") && a.length === 69))) return bad("Not a valid Stellar address (G… is 56 characters, M… is 69)");
  const raw = b32decode(a);
  if (!raw) return bad("Not a valid Stellar address");
  const payload = raw.slice(0, -2), check = raw.slice(-2);
  const crc = crc16xmodem(payload);
  if (check[0] !== (crc & 0xff) || check[1] !== (crc >>> 8)) return bad("Not a valid Stellar address (bad checksum)");
  if (payload[0] === 6 << 3 && payload.length === 33) return { ok: true, reason: "Valid Stellar address", classic: a, tag: null };
  if (payload[0] === 12 << 3 && payload.length === 41) {
    const key = payload.slice(1, 33);
    let id = 0n;
    for (let i = 0; i < 8; i++) id = (id << 8n) + BigInt(payload[33 + i]);
    const classicPayload = new Uint8Array([6 << 3, ...key]);
    const c = crc16xmodem(classicPayload);
    const classic = b32encode(new Uint8Array([...classicPayload, c & 0xff, c >>> 8]));
    return { ok: true, reason: `Valid muxed Stellar address with memo id ${id}`, classic, tag: String(id) };
  }
  return bad("Not a valid Stellar account address");
}

/** Stellar memo: id memo (digits, unsigned 64-bit) or text memo (up to 28 bytes of UTF-8). */
export function stellarMemoVerdict(memo: string): { ok: boolean; reason: string } {
  const m = memo.trim();
  if (!m) return { ok: false, reason: "Enter the memo" };
  if (/^\d+$/.test(m)) return BigInt(m) <= 18446744073709551615n ? { ok: true, reason: "ok" } : { ok: false, reason: "A memo id is at most 18446744073709551615" };
  return new TextEncoder().encode(m).length <= 28 ? { ok: true, reason: "ok" } : { ok: false, reason: "A text memo is at most 28 bytes" };
}

/** Address on a tag chain: the base address plus any tag embedded in it (X-address / M-address). */
export function parseTaggedAddress(address: string, chain: string): TaggedAddress | null {
  const family = familyForChain(chain);
  if (family === "xrpl") return parseXrpAddress(address.trim());
  if (family === "stellar") return parseStellarAddress(address.trim());
  return null;
}

export function tagVerdictForChain(tag: string, chain: string): { ok: boolean; reason: string } {
  const family = familyForChain(chain);
  if (family === "xrpl") return xrpTagVerdict(tag);
  if (family === "stellar") return stellarMemoVerdict(tag);
  return { ok: false, reason: `${chain} does not use a memo or tag` };
}

export const fmtCoin = (n: number, coin: string, digits = 8) => `${fmtAmount(n, digits)} ${coin}`;
