import { codeToHtml } from "shiki";

const THEME = "vesper";

export async function Shiki({
  code,
  lang = "python",
  title,
  className = "",
}: {
  code: string;
  lang?: string;
  title?: string;
  className?: string;
}) {
  const html = await codeToHtml(code.trim(), {
    lang,
    theme: THEME,
    transformers: [
      {
        pre(node) {
          this.addClassToHast(node, "shiki-pre");
        },
      },
    ],
  });

  return (
    <div className={`shiki-card rounded-xl overflow-hidden ${className}`}>
      {title && (
        <div
          className="mono text-[11.5px] px-4 py-2.5 flex items-center gap-2"
          style={{ background: "var(--color-code-2)", color: "#8b8577" }}
        >
          <span className="w-2 h-2 rounded-full" style={{ background: "#3a352b" }} />
          {title}
        </div>
      )}
      <div dangerouslySetInnerHTML={{ __html: html }} />
    </div>
  );
}
