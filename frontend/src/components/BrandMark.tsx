import { motion } from "motion/react";

export function BrandMark({ size = "default" }: { size?: "small" | "default" | "large" }) {
  return (
    <motion.span
      className={`brand-mark brand-mark-${size}`}
      aria-hidden="true"
      initial={false}
      whileHover={{ scale: 1.04, rotate: -1.5 }}
      whileTap={{ scale: .97 }}
    >
      <i /><i /><i />
    </motion.span>
  );
}
