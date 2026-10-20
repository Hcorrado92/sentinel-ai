export type DiffLineType = "added" | "removed" | "hunk" | "header" | "context";

export const DIFF_LINE_STYLES: Record<DiffLineType, string> = {
  added: "bg-emerald-50 text-emerald-800",
  removed: "bg-red-50 text-red-800",
  hunk: "bg-blue-50 text-blue-700",
  header: "bg-slate-100 font-semibold text-slate-600",
  context: "text-slate-700",
};

const HEADER_PREFIXES = ["diff ", "index ", "--- ", "+++ ", "new file", "deleted file", "similarity ", "rename "];

/** Classifies one line of a unified diff. File headers ("--- a/x", "+++ b/x") are not changes. */
export function classifyDiffLine(line: string): DiffLineType {
  if (HEADER_PREFIXES.some((prefix) => line.startsWith(prefix))) return "header";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "added";
  if (line.startsWith("-")) return "removed";
  return "context";
}

export function DiffViewer({ diff }: { diff: string }) {
  const lines = diff.replace(/\n$/, "").split("\n");
  if (!diff.trim()) {
    return <p className="text-sm text-slate-600">The diff is empty.</p>;
  }
  return (
    <div
      data-testid="diff-viewer"
      className="max-h-[32rem] overflow-auto whitespace-pre rounded border border-slate-200 bg-white py-1 font-mono text-xs leading-5"
    >
      {lines.map((line, index) => {
        const type = classifyDiffLine(line);
        return (
          <div key={index} data-testid="diff-line" data-line-type={type} className={`px-3 ${DIFF_LINE_STYLES[type]}`}>
            {line || " "}
          </div>
        );
      })}
    </div>
  );
}
