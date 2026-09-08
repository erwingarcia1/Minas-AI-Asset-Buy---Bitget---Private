import { NavLink } from "react-router-dom";

const link = ({ isActive }: { isActive: boolean }) =>
  `block rounded px-3 py-2 text-sm ${isActive ? "bg-blue-50 font-medium text-blue-700" : "text-gray-600 hover:bg-gray-100"}`;

export default function Sidebar() {
  return (
    <nav className="w-48 shrink-0 space-y-1 border-r bg-white p-3 max-md:hidden">
      <NavLink to="/dashboard" className={link}>Dashboard</NavLink>
      <NavLink to="/purchases" className={link}>Purchases</NavLink>
      <NavLink to="/auto-buy" className={link}>Auto-Buy</NavLink>
      <NavLink to="/settings" className={link}>Settings</NavLink>
    </nav>
  );
}
