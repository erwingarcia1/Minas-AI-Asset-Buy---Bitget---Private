import { Redirect } from "expo-router";
import { useEffect, useState } from "react";
import { auth } from "../services/api";

export default function Index() {
  const [target, setTarget] = useState<string | null>(null);
  useEffect(() => {
    auth.isLoggedIn().then((ok) => setTarget(ok ? "/(dashboard)" : "/(auth)/login"));
  }, []);
  if (!target) return null;
  return <Redirect href={target as never} />;
}
