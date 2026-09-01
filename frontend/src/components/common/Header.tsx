import { useNavigate } from "react-router-dom";
import { auth } from "../../services/api";

export default function Header() {
  const navigate = useNavigate();
  return (
    <header className="flex items-center justify-between border-b bg-white px-6 py-3">
      <span className="text-lg font-semibold">AutoBuy</span>
      <button
        onClick={() => auth.logout().then(() => navigate("/login"))}
        className="text-sm text-gray-600 hover:text-gray-900">
        Sign out
      </button>
    </header>
  );
}
