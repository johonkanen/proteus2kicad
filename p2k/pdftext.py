"""Words of a schematic PDF with page and position (pdftotext -bbox), and the page of each refdes.

  words = pdf_words("source/schematic_pdf/x.PDF")   # [(page, x0, y0, x1, y1, text)], pt, y down
  pages = refdes_pages(words, refs)                 # {refdes: [pages where the word occurs]}

A refdes word may carry a unit suffix ("U1:A"). A refdes-like word can also be a net label
(e.g. FPGA ball names C15/C16 on a connector) or appear in notes, so callers resolve the
refdes found on several pages (or none) explicitly. Needs poppler's pdftotext.
"""
import html
import re
import subprocess


def pdf_words(pdf):
    out = subprocess.run(["pdftotext", "-bbox", pdf, "-"], capture_output=True, text=True, check=True).stdout
    words = []
    for n, page in enumerate(re.split(r"<page ", out)[1:], 1):
        for m in re.finditer(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>', page):
            words.append((n, *map(float, m.groups()[:4]), html.unescape(m.group(5))))
    return words


def refdes_pages(words, refs):
    first = {}
    for r in refs:
        first.setdefault(r.split()[0], []).append(r)
    pages = {r: set() for r in refs}
    for n, *_, text in words:
        for r in first.get(text.split(":")[0], ()):
            if " " not in r:
                pages[r].add(n)
    # multi-word refdes ("SEC GND"): consecutive words on one line
    for r in refs:
        if " " in r:
            a, b = r.split(" ", 1)
            for i, w in enumerate(words[:-1]):
                v = words[i + 1]
                if w[5] == a and v[5] == b and w[0] == v[0] and abs(w[2] - v[2]) < 2:
                    pages[r].add(w[0])
    return {r: sorted(p) for r, p in pages.items()}
