import { router } from "expo-router";
import { useState } from "react";
import { Pressable, Text, TextInput, View } from "react-native";
import { auth, flushSyncQueue } from "../../services/api";
import { validateEmail } from "../../services/logic";
import { card, colors, input } from "../../components/theme";

export default function LoginScreen() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit() {
    const emailError = validateEmail(email);
    if (emailError) return setError(emailError);
    setBusy(true);
    setError(null);
    try {
      await auth.login(email.trim(), password);
      flushSyncQueue().catch(() => undefined);
      router.replace("/(dashboard)");
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } })
        .response?.data?.detail;
      setError(detail ?? "Login failed — check your connection.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <View style={{ flex: 1, justifyContent: "center", padding: 24, backgroundColor: colors.bg }}>
      <View style={card}>
        <Text style={{ fontSize: 22, fontWeight: "600", marginBottom: 16 }}>Sign in to AutoBuy</Text>
        {error && <Text style={{ color: colors.danger, marginBottom: 8 }}>{error}</Text>}
        <Text style={{ color: colors.muted }}>Email</Text>
        <TextInput style={input} autoCapitalize="none" keyboardType="email-address"
                   value={email} onChangeText={setEmail} testID="email" />
        <Text style={{ color: colors.muted, marginTop: 12 }}>Password</Text>
        <TextInput style={input} secureTextEntry value={password}
                   onChangeText={setPassword} testID="password" />
        <Pressable onPress={submit} disabled={busy}
                   style={{ backgroundColor: colors.accent, borderRadius: 8, padding: 14,
                            marginTop: 20, opacity: busy ? 0.6 : 1 }}>
          <Text style={{ color: "#fff", textAlign: "center", fontWeight: "600" }}>
            {busy ? "Signing in…" : "Sign in"}
          </Text>
        </Pressable>
        <Pressable onPress={() => router.push("/(auth)/register")}>
          <Text style={{ color: colors.accent, marginTop: 16, textAlign: "center" }}>
            New here? Create an account
          </Text>
        </Pressable>
      </View>
    </View>
  );
}
