// Wallet address validation mirrored from api/utils/network_validator.py so the
// form can give instant feedback; the API re-validates on every request.

export type Chain = "bitcoin" | "ethereum" | "solana";
export type Network = "mainnet" | "testnet";

const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
const B58_INDEX = new Map([...B58].map((c, i) => [c, i]));
const BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l";

function b58decode(s: string): Uint8Array | null {
  if (!s) return null;
  let n = 0n;
  for (const c of s) {
    const v = B58_INDEX.get(c);
    if (v === undefined) return null;
    n = n * 58n + BigInt(v);
  }
  const bytes: number[] = [];
  while (n > 0n) { bytes.unshift(Number(n & 0xffn)); n >>= 8n; }
  let pad = 0;
  for (const c of s) { if (c === "1") pad++; else break; }
  return new Uint8Array([...new Array(pad).fill(0), ...bytes]);
}

// SHA-256 (sync, tiny) for Base58Check — avoids pulling in a crypto dependency.
export function sha256(data: Uint8Array): Uint8Array {
  const K = new Uint32Array([0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2]);
  const H = new Uint32Array([0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]);
  const len = data.length, bitLen = len * 8;
  const padded = new Uint8Array(((len + 9 + 63) >> 6) << 6);
  padded.set(data); padded[len] = 0x80;
  new DataView(padded.buffer).setUint32(padded.length - 4, bitLen >>> 0);
  new DataView(padded.buffer).setUint32(padded.length - 8, Math.floor(bitLen / 2 ** 32));
  const w = new Uint32Array(64);
  const rotr = (x: number, n: number) => (x >>> n) | (x << (32 - n));
  for (let i = 0; i < padded.length; i += 64) {
    const dv = new DataView(padded.buffer, i, 64);
    for (let t = 0; t < 16; t++) w[t] = dv.getUint32(t * 4);
    for (let t = 16; t < 64; t++) {
      const s0 = rotr(w[t - 15], 7) ^ rotr(w[t - 15], 18) ^ (w[t - 15] >>> 3);
      const s1 = rotr(w[t - 2], 17) ^ rotr(w[t - 2], 19) ^ (w[t - 2] >>> 10);
      w[t] = (w[t - 16] + s0 + w[t - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, h] = H;
    for (let t = 0; t < 64; t++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (h + S1 + ch + K[t] + w[t]) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (S0 + maj) >>> 0;
      h = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    H[0] += a; H[1] += b; H[2] += c; H[3] += d; H[4] += e; H[5] += f; H[6] += g; H[7] += h;
  }
  const out = new Uint8Array(32);
  new DataView(out.buffer).setUint32(0, H[0]); for (let i = 1; i < 8; i++) new DataView(out.buffer).setUint32(i * 4, H[i]);
  return out;
}

function b58checkPayload(s: string): Uint8Array | null {
  const raw = b58decode(s);
  if (!raw || raw.length < 5) return null;
  const data = raw.slice(0, -4), check = raw.slice(-4);
  const digest = sha256(sha256(data));
  for (let i = 0; i < 4; i++) if (digest[i] !== check[i]) return null;
  return data;
}

function bech32Polymod(values: number[]): number {
  const GEN = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3];
  let chk = 1;
  for (const v of values) {
    const top = chk >>> 25;
    chk = ((chk & 0x1ffffff) << 5) ^ v;
    for (let i = 0; i < 5; i++) if ((top >>> i) & 1) chk ^= GEN[i];
  }
  return chk >>> 0;
}

function bech32Decode(addr: string): { hrp: string; version: number; program: number[] } | null {
  if (addr.length < 14 || addr.length > 90) return null;
  if (addr !== addr.toLowerCase() && addr !== addr.toUpperCase()) return null;
  const a = addr.toLowerCase();
  const pos = a.lastIndexOf("1");
  if (pos < 1 || pos + 7 > a.length) return null;
  const hrp = a.slice(0, pos), body = a.slice(pos + 1);
  const data: number[] = [];
  for (const c of body) { const i = BECH32.indexOf(c); if (i < 0) return null; data.push(i); }
  const hrpExp = [...hrp].map((c) => c.charCodeAt(0) >> 5).concat([0], [...hrp].map((c) => c.charCodeAt(0) & 31));
  const constant = bech32Polymod(hrpExp.concat(data));
  const version = data[0], payload = data.slice(1, -6);
  if (version === 0 && constant !== 1) return null;
  if (version !== 0 && constant !== 0x2bc830a3) return null;
  if (version > 16) return null;
  let acc = 0, bits = 0; const out: number[] = [];
  for (const v of payload) { acc = (acc << 5) | v; bits += 5; while (bits >= 8) { bits -= 8; out.push((acc >> bits) & 0xff); } }
  if (bits >= 5 || ((acc << (8 - bits)) & 0xff)) return null;
  if (out.length < 2 || out.length > 40 || (version === 0 && out.length !== 20 && out.length !== 32)) return null;
  return { hrp, version, program: out };
}

function bitcoinNetwork(address: string): Network | null {
  if (!address) return null;
  const lower = address.toLowerCase();
  if (lower.startsWith("bc1") || lower.startsWith("tb1") || lower.startsWith("bcrt1")) {
    const d = bech32Decode(address);
    if (!d) return null;
    return d.hrp === "bc" ? "mainnet" : d.hrp === "tb" || d.hrp === "bcrt" ? "testnet" : null;
  }
  const data = b58checkPayload(address);
  if (!data || data.length !== 21) return null;
  if (data[0] === 0x00 || data[0] === 0x05) return "mainnet";
  if (data[0] === 0x6f || data[0] === 0xc4) return "testnet";
  return null;
}

export const validateBitcoinAddress = (a: string) => bitcoinNetwork(a) !== null;
export const validateEthereumAddress = (a: string) => /^0x[0-9a-fA-F]{40}$/.test(a);
export function validateSolanaAddress(a: string): boolean {
  if (!a || a.length < 32 || a.length > 44) return false;
  const raw = b58decode(a);
  return !!raw && raw.length === 32;
}

export function detectChain(address: string): Chain | null {
  if (validateEthereumAddress(address)) return "ethereum";
  if (validateBitcoinAddress(address)) return "bitcoin";
  if (validateSolanaAddress(address)) return "solana";
  return null;
}

/** Bitcoin encodes its network; Ethereum/Solana addresses are the same on every network. */
export function getAddressNetwork(address: string): Network | null {
  const chain = detectChain(address);
  if (chain === "bitcoin") return bitcoinNetwork(address);
  return chain ? "mainnet" : null;
}

/** Known unspendable / burn / placeholder addresses (mirrors api/utils/address_denylist.py). Always rejected. */
export const ADDRESS_DENYLIST = new Set([
  "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "1111111111111111111114oLvT2", "1BitcoinEaterAddressDontSendf59kuE",
  "1CounterpartyXXXXXXXXXXXXXXXUXCHtA",
  "0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead",
  "0x0000000000000000000000000000000000000001", "0xffffffffffffffffffffffffffffffffffffffff",
  "11111111111111111111111111111111", "1nc1nerator11111111111111111111111111111111",
]);
export const DENYLIST_REASON = "This is a known unspendable, burn or placeholder address; coins sent there are lost. Enter a wallet you control.";

export function isDenylisted(address: string): boolean {
  const a = address.trim();
  return ADDRESS_DENYLIST.has(a) || (a.toLowerCase().startsWith("0x") && ADDRESS_DENYLIST.has(a.toLowerCase()));
}
