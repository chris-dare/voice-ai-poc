import { createRoot } from "react-dom/client";
import App from "./VoiceAIApp";
import "./styles/tokens.css";
import "./styles/app.css";

createRoot(document.getElementById("root")!).render(
  <App />,
);
