import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export default function MessageMarkdown({ children }: { children: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>;
}
