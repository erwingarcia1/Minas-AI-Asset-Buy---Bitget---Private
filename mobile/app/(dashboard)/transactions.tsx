import { useCallback, useEffect, useState } from "react";
import { FlatList, Pressable, Text, View } from "react-native";
import { transactions } from "../../services/api";
import type { Transaction } from "../../services/types";
import { card, colors } from "../../components/theme";

const FILTERS = ["all", "completed", "failed", "pending"] as const;

export default function TransactionsScreen() {
  const [items, setItems] = useState<Transaction[]>([]);
  const [filter, setFilter] = useState<string>("all");
  const [limit, setLimit] = useState(25);

  const load = useCallback(async () => {
    try {
      const page = await transactions.list(limit);
      setItems(filter === "all" ? page.items
        : page.items.filter((t) => t.status === filter));
    } catch { /* stale cache acceptable */ }
  }, [filter, limit]);
  useEffect(() => { load(); }, [load]);

  return (
    <FlatList
      style={{ backgroundColor: colors.bg }}
      contentContainerStyle={{ padding: 16 }}
      ListHeaderComponent={
        <View style={{ flexDirection: "row", gap: 6, marginBottom: 12 }}>
          {FILTERS.map((f) => (
            <Pressable key={f} onPress={() => setFilter(f)}
                       style={{ paddingHorizontal: 10, paddingVertical: 6, borderRadius: 999,
                                backgroundColor: filter === f ? colors.accent : colors.surface }}>
              <Text style={{ color: filter === f ? "#fff" : colors.ink }}>{f}</Text>
            </Pressable>
          ))}
        </View>
      }
      data={items}
      keyExtractor={(t) => t.id}
      renderItem={({ item }) => (
        <View style={card}>
          <Text style={{ fontWeight: "600" }}>
            {item.crypto} · {item.amount_crypto.toFixed(8)} · ${item.cost_usdt.toFixed(2)}
          </Text>
          <Text style={{ color: item.status === "completed" ? colors.accent
                         : item.status === "failed" ? colors.danger : colors.warn }}>
            {item.status}{item.error_message ? ` — ${item.error_message}` : ""}
          </Text>
          <Text style={{ color: colors.muted, fontSize: 12 }}>
            {new Date(item.created_at).toLocaleString()}
          </Text>
        </View>
      )}
      onEndReached={() => setLimit((l) => Math.min(l + 25, 100))}
      onEndReachedThreshold={0.4}
      ListEmptyComponent={<Text style={{ color: colors.muted }}>No transactions.</Text>}
    />
  );
}
