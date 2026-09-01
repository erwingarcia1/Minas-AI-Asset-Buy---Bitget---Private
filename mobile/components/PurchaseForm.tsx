import { useState } from "react";
import { Pressable, Text, TextInput, View } from "react-native";
import { api, enqueueForSync } from "../services/api";
import { validateAmount } from "../services/logic";
import { card, colors, input } from "./theme";

const CRYPTOS = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "LTC"];
const FREQUENCIES = ["daily", "weekly", "biweekly", "monthly"] as const;

export default function PurchaseForm({ onDone }: { onDone: () => void }) {
  const [crypto, setCrypto] = useState("BTC");
  const [amount, setAmount] = useState("100");
  const [frequency, setFrequency] = useState<string>("weekly");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const amountError = validateAmount(Number(amount));

  async function submit() {
    if (amountError) return setError(amountError);
    setBusy(true);
    setError(null);
    const body = { crypto, amount_usdt: Number(amount), frequency };
    try {
      await api.post("/api/purchases/", body);
      onDone();
    } catch (err) {
      const status = (err as { response?: { status?: number } }).response?.status;
      if (status === undefined) {
        // offline: queue for background sync instead of losing the intent
        await enqueueForSync("post", "/api/purchases/", body);
        setError("You're offline — the schedule is queued and will sync automatically.");
      } else {
        const detail = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
        setError(typeof detail === "string" ? detail : "Couldn't save the schedule.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <View style={card}>
      <Text style={{ fontWeight: "600", marginBottom: 8 }}>New schedule</Text>
      {error && <Text style={{ color: colors.danger, marginBottom: 8 }}>{error}</Text>}
      <Text style={{ color: colors.muted }}>Crypto</Text>
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6, marginVertical: 6 }}>
        {CRYPTOS.map((c) => (
          <Pressable key={c} onPress={() => setCrypto(c)}
                     style={{ paddingHorizontal: 10, paddingVertical: 6, borderRadius: 999,
                              backgroundColor: crypto === c ? colors.accent : colors.bg }}>
            <Text style={{ color: crypto === c ? "#fff" : colors.ink }}>{c}</Text>
          </Pressable>
        ))}
      </View>
      <Text style={{ color: colors.muted }}>Amount (USDT)</Text>
      <TextInput style={input} keyboardType="decimal-pad" value={amount}
                 onChangeText={setAmount} testID="amount" />
      {amountError && <Text style={{ color: colors.danger, fontSize: 12 }}>{amountError}</Text>}
      <Text style={{ color: colors.muted, marginTop: 8 }}>Frequency</Text>
      <View style={{ flexDirection: "row", gap: 6, marginVertical: 6 }}>
        {FREQUENCIES.map((f) => (
          <Pressable key={f} onPress={() => setFrequency(f)}
                     style={{ paddingHorizontal: 10, paddingVertical: 6, borderRadius: 999,
                              backgroundColor: frequency === f ? colors.accent : colors.bg }}>
            <Text style={{ color: frequency === f ? "#fff" : colors.ink }}>{f}</Text>
          </Pressable>
        ))}
      </View>
      <Pressable onPress={submit} disabled={busy || Boolean(amountError)}
                 style={{ backgroundColor: colors.accent, borderRadius: 8, padding: 12,
                          marginTop: 12, opacity: busy || amountError ? 0.6 : 1 }}>
        <Text style={{ color: "#fff", textAlign: "center", fontWeight: "600" }}>
          {busy ? "Saving…" : "Create schedule"}
        </Text>
      </Pressable>
    </View>
  );
}
