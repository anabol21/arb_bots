import { Routes, Route } from "react-router-dom";
import Home from "./pages/Home.jsx";
import CoinPage from "./pages/CoinPage.jsx";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Home />} />
      <Route path="/coin/:coin" element={<CoinPage />} />
    </Routes>
  );
}
