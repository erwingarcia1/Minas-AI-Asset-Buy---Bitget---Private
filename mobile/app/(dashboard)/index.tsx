import { useCallback, useEffect, useState } from "react";
import { FlatList, RefreshControl, Text, View } from "react-native";
import { dashboard, transactions } from "../../services/api";
import type { Dashboard, Transaction } from "../../services/types";
import { card, colors } from "../../components/theme";

const usd = (n: number) => `$${n.toFixed(2)}`;

export default function DashboardScreen() {
  const [data, setData] = useState<Dashboard | null>(null);
  const [recent, setRecent] = useState<Transaction[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [d, txs] = await Promise.all([dashboard.get(), transactions.list(10)]);
      setData(d);
      setRecent(txs.items);
    } catch {
      setError("Couldn't load — showing nothing new. Pull to retry.");
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  const invested = data?.holdings.reduce((s, h) => s + h.total_cost_usdt, 0) ?? 0;

  return (
    <FlatList
      style={{ backgroundColor: colors.bg }}
      contentContainerStyle={{ padding: 16 }}
      refreshControl={<RefreshControl refreshing={refreshing}
        onRefresh={async () => { setRefreshing(true); await load(); setRefreshing(false); }} />}
      ListHeaderComponent={
        <View>
          {error && <Text style={{ color: colors.warn, marginBottom: 8 }}>{error}</Text>}
          <View style={{ flexDirection: "row", gap: 12, marginBottom: 12 }}>
            <View style={[card, { flex: 1 }]}>
              <Text style={{ color: colors.muted, fontSize: 12 }}>INVESTED</Text>
              <Text style={{ fontSize: 20, fontWeight: "700" }}>{usd(invested)}</Text>
            </View>
            <View style={[card, { flex: 1 }]}>
              <Text style={{ color: colors.muted, fontSize: 12 }}>ACTIVE</Text>
              <Text style={{ fontSize: 20, fontWeight: "700" }}>
                {data?.active_purchases.length ?? "—"}
              </Text>
            </View>
          </View>
          <View style={card}>
            <Text style={{ fontWeight: "600", marginBottom: 8 }}>Holdings</Text>
            {(data?.holdings ?? []).map((h) => (
              <View key={h.crypto}
                    style={{ flexDirection: "row", justifyContent: "space-between", paddingVertical: 4 }}>
                <Text style={{ fontWeight: "600" }}>{h.crypto}</Text>
                <Text>{h.total_amount.toFixed(8)}</Text>
                <Text style={{ color: colors.muted }}>{usd(h.total_cost_usdt)}</Text>
              </View>
            ))}
            {data && data.holdings.length === 0 && (
              <Text style={{ color: colors.muted }}>No completed purchases yet.</Text>
            )}
          </View>
          <View style={card}>
            <Text style={{ fontWeight: "600", marginBottom: 8 }}>Next buys</Text>
            {(data?.upcoming_purchases ?? []).slice(0, 5).map((p) => (
              <Text key={p.id} style={{ paddingVertical: 2 }}>
                {p.crypto} · {usd(p.amount_usdt)} · {new Date(p.next_purchase_date).toLocaleDateString()}
              </Text>
            ))}
          </View>
          <Text style={{ fontWeight: "600", marginVertical: 8 }}>Recent activity</Text>
        </View>
      }
      data={recent}
      keyExtractor={(t) => t.id}
      renderItem={({ item }) => (
        <View style={[card, { marginBottom: 8 }]}>
          <Text style={{ fontWeight: "600" }}>
            {item.crypto} · {usd(item.cost_usdt)}
            <Text style={{ color: item.status === "completed" ? colors.accent
                           : item.status === "failed" ? colors.danger : colors.warn }}>
              {"  "}{item.status}
            </Text>
          </Text>
          <Text style={{ color: colors.muted, fontSize: 12 }}>
            {new Date(item.created_at).toLocaleString()}
          </Text>
        </View>
      )}
    />
  );
}
