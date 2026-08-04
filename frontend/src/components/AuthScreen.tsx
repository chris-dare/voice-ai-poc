import { ArrowRight } from "lucide-react";
import { motion } from "motion/react";
import { BrandMark } from "./BrandMark";

interface AuthScreenProps {
  message: string;
  buttonLabel: string;
  disabled: boolean;
  onLogin(): void;
}

export function AuthScreen({ message, buttonLabel, disabled, onLogin }: AuthScreenProps) {
  return (
    <main className="auth-screen">
      <section className="auth-visual" aria-label="Product introduction">
        <a className="wordmark" href="/" aria-label="Voice AI home"><BrandMark /><span>Voice AI</span></a>
        <motion.div className="auth-hero" initial={{ opacity: 0, y: 18 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: .65, ease: [0.22, 1, 0.36, 1] }}>
          <p className="eyebrow">THINK OUT LOUD</p>
          <h1>A clearer space<br />for every thought.</h1>
          <p>Move naturally between asking, exploring, and creating—by text or voice.</p>
        </motion.div>
        <div className="auth-orb" aria-hidden="true"><div /><div /></div>
      </section>
      <section className="auth-entry">
        <motion.div className="auth-card" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: .12, duration: .5 }}>
          <div className="mobile-wordmark"><BrandMark /><span>Voice AI</span></div>
          <p className="eyebrow">WELCOME</p>
          <h2>Continue your conversation.</h2>
          <p>{message}</p>
          <button className="primary-button" type="button" onClick={onLogin} disabled={disabled}>
            <span>{buttonLabel}</span><ArrowRight size={17} />
          </button>
          <small>Your conversations remain private to your account.</small>
        </motion.div>
      </section>
    </main>
  );
}
