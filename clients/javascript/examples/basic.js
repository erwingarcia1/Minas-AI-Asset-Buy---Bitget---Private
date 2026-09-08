// Run against a local API: node examples/basic.js
import { AutoBuyClient, ApiError } from "@autobuy/js-client";

const client = new AutoBuyClient({
  baseUrl: process.env.AUTOBUY_API_URL ?? "http://localhost:8000",
  onTokensChanged: (t) => console.log("tokens changed:", t ? "logged in" : "logged out"),
});

const email = `demo-${Date.now()}@example.com`;
const password = "Str0ng!Passw0rd";

await client.register(email, password);
await client.login(email, password);
console.log("me:", await client.me());

try {
  const purchase = await client.createPurchase({ crypto: "BTC", amount_usdt: 50, frequency: "weekly" });
  console.log("created purchase", purchase.id);
} catch (e) {
  if (e instanceof ApiError) console.log("purchase rejected:", e.status, e.detail);
  else throw e;
}

const txs = await client.transactions.listTransactions({ limit: 10 });
console.log(`transactions: ${txs.total}`);
console.log("stats:", await client.dashboard.quickStats());

await client.refreshTokens();
await client.logout();
