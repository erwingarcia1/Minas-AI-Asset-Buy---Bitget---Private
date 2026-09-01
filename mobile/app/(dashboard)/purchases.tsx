import { useCallback, useEffect, useState } from "react";
import { FlatList, Pressable, RefreshControl, Switch, Text, View } from "react-native";
import PurchaseForm from "../../components/PurchaseForm";
import { api, purchases } from "../../services/api";
import type { RecurringPurchase } from "../../services/types";
import { card, colors } from "../../components/theme";

export default function PurchasesScreen() {
  const [items, setItems] = useState<RecurringPurchase[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    try { setItems(await purchases.list()); } catch { /* stale cache acceptable */ }
  }, []);
  useEffect(() => { load(); }, [load]);

  async function toggle(p: RecurringPurchase) {
    setItems((prev) => prev.map((x) => x.id === p.id ? { ...x, enabled: !x.enabled } : x));
    try { await api.patch(`/api/purchases/${p.id}/toggle`); } catch { load(); }
  }

  return (
    <FlatList
      style={{ backgroundColor: colors.bg }}
      contentContainerStyle={{ padding: 16 }}
      refreshControl={<RefreshControl refreshing={refreshing}
        onRefresh={async () => { setRefreshing(true); await load(); setRefreshing(false); }} />}
      ListHeaderComponent={
        <View>
          <Pressable onPress={() => setShowForm((v) => !v)}
                     style={{ backgroundColor: colors.accent, borderRadius: 8,
                              padding: 12, marginBottom: 12 }}>
            <Text style={{ color: "#fff", textAlign: "center", fontWeight: "600" }}>
              {showForm ? "Close" : "New schedule"}
            </Text>
          </Pressable>
          {showForm && <PurchaseForm onDone={() => { setShowForm(false); load(); }} />}
        </View>
      }
      data={items}
      keyExtractor={(p) => p.id}
      renderItem={({ item }) => (
        <View style={[card, { flexDirection: "row", alignItems: "center" }]}>
          <View style={{ flex: 1 }}>
            <Text style={{ fontWeight: "600" }}>
              {item.crypto} · ${item.amount_usdt} {item.frequency}
            </Text>
            <Text style={{ color: colors.muted, fontSize: 12 }}>
              Next: {new Date(item.next_purchase_date).toLocaleDateString()}
            </Text>
          </View>
          <Switch value={item.enabled} onValueChange={() => toggle(item)} />
        </View>
      )}
      ListEmptyComponent={<Text style={{ color: colors.muted }}>No schedules yet.</Text>}
    />
  );
}
