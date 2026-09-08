// Run against a local API: npx tsx examples/basic.ts  (or compile with tsc)
import { AutoBuyClient, ApiError } from "@autobuy/ts-client";

const client = new AutoBuyClient({
  baseUrl: process.env.AUTOBUY_API_URL ?? "http://localhost:8000",
  onTokensChanged: (t) => console.log("tokens changed:", t ? "logged in" : "logged out"),
});

async function main() {
  const email = `demo-${Date.now()}@example.com`;
  const password = "Str0ng!Passw0rd";

  await client.register(email, password);
  await client.login(email, password);
  console.log("me:", await client.me());

  try {
    const purchase = await client.createPurchase({ crypto: "BTC", amount_usdt: 50, frequency: "weekly" });
    console.log("created purchase", purchase.id);
  } catch (e) {
    // Unverified KYC / missing exchange key surface as ApiError with a readable detail
    if (e instanceof ApiError) console.log("purchase rejected:", e.status, e.detail);
    else throw e;
  }

  const txs = await client.transactions.listTransactions({ limit: 10 });
  console.log(`transactions: ${txs.total}`);
  console.log("stats:", await client.dashboard.quickStats());

  await client.refreshTokens(); // explicit refresh (normally automatic on 401)
  await client.logout();
}

main().catch((e) => { console.error(e); process.exit(1); });
