import APIKeys from "../components/Settings/APIKeys";
import KYC from "../components/Settings/KYC";
import Integrations from "../components/Integrations";
import Reports from "../components/Reports";
import Profile from "../components/Settings/Profile";

export default function SettingsPage() {
  return (
    <div className="space-y-6 p-6">
      <Profile />
      <APIKeys />
      <KYC />
      <Reports />
      <Integrations />
    </div>
  );
}
