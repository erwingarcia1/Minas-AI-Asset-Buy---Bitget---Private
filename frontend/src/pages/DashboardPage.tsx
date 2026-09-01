import Holdings from "../components/Dashboard/Holdings";
import Stats from "../components/Dashboard/Stats";
import PurchaseHistory from "../components/Dashboard/PurchaseHistory";

export default function DashboardPage() {
  return (
    <div className="space-y-6 p-6">
      <Stats />
      <Holdings />
      <PurchaseHistory />
    </div>
  );
}
