import { router } from "expo-router";
import { useState } from "react";
import { Pressable, Switch, Text, TextInput, View } from "react-native";
import { auth } from "../../services/api";
import { passwordStrength, validateEmail, validatePassword } from "../../services/logic";
import { card, colors, input } from "../../components/theme";

const STRENGTH = ["Too weak", "Weak", "Good", "Strong"] as const;
const STRENGTH_COLORS = [colors.danger, colors.warn, colors.accent, colors.accent];

export default function RegisterScreen() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [accepted, setAccepted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const strength = passwordStrength(password);

  async function submit() {
    const problem = validateEmail(email) ?? validatePassword(password)
      ?? (password !== confirm ? "Passwords don't match." : null)
      ?? (!accepted ? "Please accept the terms to continue." : null);
    if (problem) return setError(problem);
    setBusy(true);
    setError(null);
    try {
      await auth.register(email.trim(), password);
      router.replace("/(auth)/login");
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
      setError(typeof detail === "string" ? detail : "Registration failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <View style={{ flex: 1, justifyContent: "center", padding: 24, backgroundColor: colors.bg }}>
      <View style={card}>
        <Text style={{ fontSize: 22, fontWeight: "600", marginBottom: 16 }}>Create your account</Text>
        {error && <Text style={{ color: colors.danger, marginBottom: 8 }}>{error}</Text>}
        <Text style={{ color: colors.muted }}>Email</Text>
        <TextInput style={input} autoCapitalize="none" keyboardType="email-address"
                   value={email} onChangeText={setEmail} testID="email" />
        <Text style={{ color: colors.muted, marginTop: 12 }}>Password</Text>
        <TextInput style={input} secureTextEntry value={password}
                   onChangeText={setPassword} testID="password" />
        {password.length > 0 && (
          <Text style={{ color: STRENGTH_COLORS[strength], marginTop: 4 }}>
            {STRENGTH[strength]}
          </Text>
        )}
        <Text style={{ color: colors.muted, marginTop: 12 }}>Confirm password</Text>
        <TextInput style={input} secureTextEntry value={confirm}
                   onChangeText={setConfirm} testID="confirm" />
        <View style={{ flexDirection: "row", alignItems: "center", marginTop: 16, gap: 8 }}>
          <Switch value={accepted} onValueChange={setAccepted} testID="terms" />
          <Text style={{ color: colors.muted, flex: 1 }}>
            I accept the terms of service and understand purchases execute with my own exchange funds.
          </Text>
        </View>
        <Pressable onPress={submit} disabled={busy}
                   style={{ backgroundColor: colors.accent, borderRadius: 8, padding: 14,
                            marginTop: 20, opacity: busy ? 0.6 : 1 }}>
          <Text style={{ color: "#fff", textAlign: "center", fontWeight: "600" }}>
            {busy ? "Creating…" : "Create account"}
          </Text>
        </Pressable>
      </View>
    </View>
  );
}
