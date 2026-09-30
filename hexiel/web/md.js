/* markdown-lite: small, local renderer for streamed assistant replies.
   Deliberately dependency-free — no CDN fetches, works offline.
   Supports: headings, fenced code, inline code, bold/italic/strike,
   links (rel=noopener), unordered/ordered lists, blockquotes, hr, tables,
   paragraphs. Escapes HTML first, so model output can't inject markup. */

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
           .replace(/"/g, "&quot;");
}

function inline(text) {
  let s = escapeHtml(text);
  // inline code first so its contents are untouched by later rules
  const codes = [];
  s = s.replace(/`([^`\n]+)`/g, (_, c) => {
    codes.push(`<code>${c}</code>`);
    return `\u0000${codes.length - 1}\u0000`;
  });
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|\W)\*([^*\n]+)\*/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  s = s.replace(/\u0000(\d+)\u0000/g, (_, i) => codes[Number(i)]);
  return s;
}

function renderMarkdown(src) {
  const lines = String(src ?? "").split("\n");
  let html = "", i = 0;
  const flushParagraph = (buf) => {
    if (buf.length) html += `<p>${inline(buf.join(" "))}</p>`;
  };
  let para = [];

  while (i < lines.length) {
    const line = lines[i];

    // fenced code
    const fence = line.match(/^\s*```(\w*)\s*$/);
    if (fence) {
      flushParagraph(para); para = [];
      const lang = fence[1];
      const body = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
        body.push(lines[i]); i++;
      }
      i++; // closing fence (or EOF)
      html += `<pre data-lang="${escapeHtml(lang)}"><code>${escapeHtml(body.join("\n"))}</code></pre>`;
      continue;
    }

    // heading
    const head = line.match(/^(#{1,4})\s+(.*)$/);
    if (head) {
      flushParagraph(para); para = [];
      const level = Math.min(head[1].length + 1, 6);
      html += `<h${level}>${inline(head[2])}</h${level}>`;
      i++; continue;
    }

    // hr
    if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) {
      flushParagraph(para); para = [];
      html += "<hr>";
      i++; continue;
    }

    // blockquote
    if (/^\s*>\s?/.test(line)) {
      flushParagraph(para); para = [];
      const body = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        body.push(lines[i].replace(/^\s*>\s?/, "")); i++;
      }
      html += `<blockquote>${renderMarkdown(body.join("\n"))}</blockquote>`;
      continue;
    }

    // table: | a | b | / |---|---|
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length &&
        /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      flushParagraph(para); para = [];
      const cells = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      const headCells = cells(line), alignLine = cells(lines[i + 1]); i += 2;
      const rowHtml = (cells_, tag) => cells_.map(c => `<${tag}>${inline(c)}</${tag}>`).join("");
      html += `<table><thead><tr>${rowHtml(headCells, "th")}</tr></thead><tbody>`;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        html += `<tr>${rowHtml(cells(lines[i]), "td")}</tr>`; i++;
      }
      html += "</tbody></table>";
      continue;
    }

    // lists
    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || ordered) {
      flushParagraph(para); para = [];
      const tag = bullet ? "ul" : "ol";
      const re = bullet ? /^\s*[-*+]\s+(.*)$/ : /^\s*\d+[.)]\s+(.*)$/;
      let body = "";
      while (i < lines.length) {
        const m = lines[i].match(re);
        if (m) { body += `<li>${inline(m[1])}</li>`; i++; continue; }
        if (/^\s{2,}\S/.test(lines[i]) && body) { // continuation line
          body = body.replace(/<\/li>$/, " " + inline(lines[i].trim()) + "</li>"); i++; continue;
        }
        break;
      }
      html += `<${tag}>${body}</${tag}>`;
      continue;
    }

    // task list items (todo rendering)
    const task = line.match(/^\s*[-*]\s+\[([ x~x])\]\s+(.*)$/);
    if (task) {
      flushParagraph(para); para = [];
      const cls = task[1] === "x" ? "done" : task[1] === "~" ? "doing" : "queued";
      html += `<div class="task ${cls}">${inline(task[2])}</div>`;
      i++; continue;
    }

    if (!line.trim()) { flushParagraph(para); para = []; i++; continue; }
    para.push(line); i++;
  }
  flushParagraph(para);
  return html;
}