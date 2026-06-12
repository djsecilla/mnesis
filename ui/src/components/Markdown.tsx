import ReactMarkdown, { type Components } from "react-markdown";
import { Link } from "react-router-dom";
import remarkGfm from "remark-gfm";

// react-markdown does NOT render raw HTML (no rehype-raw), so the body is
// sanitized by construction. We pre-rewrite [[page-id]] into internal links, and
// override the anchor renderer so internal links use the SPA router.

const WIKILINK = /\[\[([^\]]+)\]\]/g;
const INTERNAL = "mnesis:page/";

function preprocess(md: string): string {
  // [[id]] -> a markdown link whose label is "[[id]]" and href encodes the page id.
  return md.replace(WIKILINK, (_m, id: string) => `[[[${id}]]](${INTERNAL}${id.trim()})`);
}

const components: Components = {
  a({ href, children }) {
    if (href && href.startsWith(INTERNAL)) {
      const id = href.slice(INTERNAL.length);
      return (
        <Link to={`/pages/${encodeURIComponent(id)}`} className="text-accent hover:underline">
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

export default function Markdown({ body }: { body: string }) {
  return (
    <article className="prose prose-sm max-w-[70ch] leading-relaxed">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {preprocess(body)}
      </ReactMarkdown>
    </article>
  );
}
