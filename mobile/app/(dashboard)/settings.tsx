import { router } from "expo-router";
import { useEffect, useState } from "react";
import { Pressable, ScrollView, Text, View } from "react-native";
import { api, auth } from "../../services/api";
import type { ApiKey, User } from "../../services/types";
import { card, colors } from "../../components/theme";

export default function SettingsScreen() {
  const [profile, setProfile] = useState<User | null>(null);
  const [keys, setKeys] = useState<ApiKey[]>([]);

  useEffect(() => {
    api.get<User>("/api/user/profile").then((r) => setProfile(r.data)).catch(() => undefined);
    api.get<ApiKey[]>("/api/exchange/keys").then((r) => setKeys(r.data)).catch(() => undefined);
  }, []);

  return (
    <ScrollView style={{ backgroundColor: colors.bg }} contentContainerStyle={{ padding: 16 }}>
      <View style={card}>
        <Text style={{ fontWeight: "600", marginBottom: 4 }}>Profile</Text>
        <Text>{profile?.email ?? "…"}</Text>
        <Text style={{ marginTop: 4 }}>
          KYC:{" "}
          <Text style={{ color: profile?.kyc_status === "verified" ? colors.accent : colors.warn }}>
            {profile?.kyc_status ?? "…"}
          </Text>
        </Text>
      </View>
      <View style={card}>
        <Text style={{ fontWeight: "600", marginBottom: 4 }}>Exchange keys</Text>
        {keys.map((k) => (
          <Text key={k.id}>
            {k.exchange}: {k.api_key_masked}{" "}
            <Text style={{ color: k.verified ? colors.accent : colors.warn }}>
              {k.verified ? "verified" : "unverified"}
            </Text>
          </Text>
        ))}
        {keys.length === 0 && (
          <Text style={{ color: colors.muted }}>No keys yet — add one on the web app.</Text>
        )}
      </View>
      <Pressable
        onPress={() => auth.logout().then(() => router.replace("/(auth)/login"))}
        style={{ backgroundColor: colors.danger, borderRadius: 8, padding: 14, marginTop: 8 }}>
        <Text style={{ color: "#fff", textAlign: "center", fontWeight: "600" }}>Sign out</Text>
      </Pressable>
    </ScrollView>
  );
}
