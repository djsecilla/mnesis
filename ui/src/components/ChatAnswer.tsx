import ReactMarkdown, { type Components } from "react-markdown";
import { Link } from "react-router-dom";
import remarkGfm from "remark-gfm";

// In a chat answer, [[page-id]] is a citation. We number citations by order of
// first appearance (matching the done event's citation list) and render each as
// a compact numbered chip that links into the page. Like the page reader, the
// body is sanitized by construction — react-markdown renders no raw HTML.

const CITE = /\[\[([^\]]+)\]\]/g;
const SCHEME = "mnesis:cite/";

function numbering(text: string): Map<string, number> {
  const order = new Map<string, number>();
  for (const m of text.matchAll(CITE)) {
    const id = m[1].trim();
    if (!order.has(id)) order.set(id, order.size + 1);
  }
  return order;
}

export default function ChatAnswer({ text }: { text: string }) {
  const nums = numbering(text);
  const pre = text.replace(CITE, (_m, id: string) => {
    const t = id.trim();
    return `[${nums.get(t)}](${SCHEME}${t})`;
  });

  const components: Components = {
    a({ href, children }) {
      if (href && href.startsWith(SCHEME)) {
        const id = href.slice(SCHEME.length);
        return (
          <Link
            to={`/pages/${encodeURIComponent(id)}`}
            title={id}
            className="mx-0.5 inline-flex h-[1.05rem] min-w-[1.05rem] items-center justify-center rounded bg-accent/15 px-1 align-text-top text-[10px] font-medium text-accent no-underline hover:bg-accent/25"
          >
            {children}
          </Link>
        );
      }
      return (
        <a href={href} target="_blank" rel="noreferrer" className="text-accent hover:underline">
          {children}
        </a>
      );
    },
  };

  return (
    <div className="prose prose-sm max-w-none leading-relaxed">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {pre}
      </ReactMarkdown>
    </div>
  );
}
