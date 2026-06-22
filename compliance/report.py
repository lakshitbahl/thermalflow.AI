"""
report.py — render the JSON ledger to a PDF structured to EU AI Act Annex IV headings.

Reportlab built-in fonts (Helvetica) cover Latin-1; we avoid Unicode sub/superscripts per the
toolchain guidance and use "C" rather than the degree glyph in body text.
"""
from __future__ import annotations

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (HRFlowable, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

INK = colors.HexColor("#1a1a2e")
MUTED = colors.HexColor("#6b7280")
GREEN = colors.HexColor("#15803d")
RED = colors.HexColor("#b91c1c")
RULE = colors.HexColor("#d1d5db")
BAND = colors.HexColor("#f3f4f6")

# Which Annex IV headings the runner contributes evidence toward (paraphrased headings).
ANNEX_IV_MAP = [
    ("§2(c)", "System architecture; how components build on/feed each other",
     "Series single-writer topology; relay as sole writer; fencing at the actuator."),
    ("§2(g)", "Validation and testing procedures and results, including metrics",
     "This entire runner: deterministic conformance checks with measured latencies."),
    ("§3", "Monitoring, functioning and control; capabilities and limitations",
     "Interlocks (L1/L2/watchdogs), command validation, dead-man, HA fencing."),
    ("§4", "Appropriateness of the performance metrics",
     "Fallback-latency budgets and alarm-lead metrics stated per check."),
    ("§5", "Risk management system (Art 9): identified risks and mitigations",
     "Each fault class mapped to its detecting interlock and mitigating fallback."),
    ("§9", "Post-market monitoring plan",
     "Coil-health drift detector (advisory alarm) with load-bin baselining."),
]


def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle("TF_Title", parent=ss["Title"], textColor=INK, fontSize=20, spaceAfter=4))
    ss.add(ParagraphStyle("TF_Sub", parent=ss["Normal"], textColor=MUTED, fontSize=9, spaceAfter=2))
    ss.add(ParagraphStyle("TF_H", parent=ss["Heading2"], textColor=INK, fontSize=12,
                          spaceBefore=12, spaceAfter=6))
    ss.add(ParagraphStyle("TF_Body", parent=ss["Normal"], fontSize=9, leading=13, textColor=INK))
    ss.add(ParagraphStyle("TF_Small", parent=ss["Normal"], fontSize=8, leading=11, textColor=MUTED))
    ss.add(ParagraphStyle("TF_Mono", parent=ss["Code"], fontSize=8, leading=10, textColor=INK))
    ss.add(ParagraphStyle("TF_Disc", parent=ss["Normal"], fontSize=8, leading=12, textColor=INK))
    return ss


def _verdict_badge(v):
    c = {"PASS": GREEN, "FAIL": RED, "ERROR": RED}.get(v, MUTED)
    return f'<font color="#{c.hexval()[2:]}"><b>{v}</b></font>'


def _fmt(d):
    return ", ".join(f"{k}={v}" for k, v in d.items()) if d else "—"


def render_pdf(ledger, out_path):
    S = _styles()
    doc = SimpleDocTemplate(out_path, pagesize=A4, topMargin=18 * mm, bottomMargin=16 * mm,
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            title="ThermalFlow Annex IV Technical Documentation",
                            author="ThermalFlow compliance-runner")
    s = ledger["summary"]
    prov = ledger["provenance"]
    story = []

    # ── header ──
    story.append(Paragraph("ThermalFlow — Safety Conformance Ledger", S["TF_Title"]))
    story.append(Paragraph("Technical-documentation evidence, structured to EU AI Act Annex IV",
                           S["TF_Sub"]))
    story.append(Paragraph(f"Generated {ledger['generated_utc']} &middot; runner "
                           f"v{prov['runner_version']} &middot; schema {ledger['schema']}", S["TF_Sub"]))
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=1, color=RULE))
    story.append(Spacer(1, 6))

    vcol = GREEN if s["verdict"] == "PASS" else RED
    head = Table([[
        Paragraph(f"<b>VERDICT</b><br/><font size=15 color='#{vcol.hexval()[2:]}'><b>{s['verdict']}</b></font>", S["TF_Body"]),
        Paragraph(f"<b>Checks</b><br/>{s['passed']}/{s['total']} passed<br/>"
                  f"<font size=8 color='#6b7280'>failed {s['failed']} &middot; errored {s['errored']}</font>", S["TF_Body"]),
        Paragraph(f"<b>Code digest (SHA-256)</b><br/><font size=7>{prov['code_digest']['combined_sha256']}</font><br/>"
                  f"<font size=8 color='#6b7280'>git {prov['git_commit'] or 'n/a'}</font>", S["TF_Body"]),
    ]], colWidths=[34 * mm, 38 * mm, 102 * mm])
    head.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, RULE), ("INNERGRID", (0, 0), (-1, -1), 0.5, RULE),
        ("BACKGROUND", (0, 0), (-1, -1), BAND), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8)]))
    story.append(head)
    story.append(Spacer(1, 10))

    # ── scope / disclaimer (prominent) ──
    story.append(Paragraph("Scope and legal notice", S["TF_H"]))
    disc_rows = [[Paragraph(p, S["TF_Disc"])] for p in ledger["scope_note"].split("\n\n")]
    disc = Table(disc_rows, colWidths=[156 * mm])
    disc.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1, RED), ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fef2f2")),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8)]))
    story.append(disc)
    story.append(Spacer(1, 8))

    # ── provenance ──
    story.append(Paragraph("Provenance", S["TF_H"]))
    pr = [["Python", prov["python"]], ["Platform", prov["platform"]],
          ["Git commit", str(prov["git_commit"])],
          ["Combined code digest", prov["code_digest"]["combined_sha256"]]]
    for rel, dg in prov["code_digest"]["files"].items():
        pr.append([rel, (dg[:32] + "…") if dg else "MISSING"])
    t = Table([[Paragraph(f"<font size=8>{a}</font>", S["TF_Body"]),
                Paragraph(f"<font size=8 face='Courier'>{b}</font>", S["TF_Body"])] for a, b in pr],
              colWidths=[52 * mm, 104 * mm])
    t.setStyle(TableStyle([("INNERGRID", (0, 0), (-1, -1), 0.4, RULE), ("BOX", (0, 0), (-1, -1), 0.4, RULE),
                           ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, BAND]),
                           ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                           ("LEFTPADDING", (0, 0), (-1, -1), 6)]))
    story.append(t)
    story.append(PageBreak())

    # ── Annex IV coverage map ──
    story.append(Paragraph("Annex IV coverage map", S["TF_H"]))
    story.append(Paragraph("Best-effort structural mapping of the evidence below to Annex IV "
                           "headings. Not a completeness claim for the documentation as a whole.",
                           S["TF_Small"]))
    story.append(Spacer(1, 4))
    rows = [[Paragraph("<b>Ref</b>", S["TF_Small"]), Paragraph("<b>Annex IV heading (paraphrased)</b>", S["TF_Small"]),
             Paragraph("<b>Evidence in this ledger</b>", S["TF_Small"])]]
    for ref, head_t, ev in ANNEX_IV_MAP:
        rows.append([Paragraph(ref, S["TF_Small"]), Paragraph(head_t, S["TF_Small"]), Paragraph(ev, S["TF_Small"])])
    t = Table(rows, colWidths=[16 * mm, 66 * mm, 74 * mm])
    t.setStyle(TableStyle([("INNERGRID", (0, 0), (-1, -1), 0.4, RULE), ("BOX", (0, 0), (-1, -1), 0.4, RULE),
                           ("BACKGROUND", (0, 0), (-1, 0), INK), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                           ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BAND]),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                           ("LEFTPADDING", (0, 0), (-1, -1), 6)]))
    story.append(t)
    story.append(Spacer(1, 10))

    # ── results summary table ──
    story.append(Paragraph("Conformance results", S["TF_H"]))
    rows = [[Paragraph("<b>ID</b>", S["TF_Small"]), Paragraph("<b>Check</b>", S["TF_Small"]),
             Paragraph("<b>Annex IV</b>", S["TF_Small"]), Paragraph("<b>Verdict</b>", S["TF_Small"])]]
    for c in ledger["checks"]:
        rows.append([Paragraph(c["id"], S["TF_Small"]), Paragraph(c["title"], S["TF_Small"]),
                     Paragraph(c["annex_iv"], S["TF_Small"]),
                     Paragraph(_verdict_badge(c["verdict"]), S["TF_Small"])])
    t = Table(rows, colWidths=[30 * mm, 74 * mm, 38 * mm, 14 * mm])
    t.setStyle(TableStyle([("INNERGRID", (0, 0), (-1, -1), 0.4, RULE), ("BOX", (0, 0), (-1, -1), 0.4, RULE),
                           ("BACKGROUND", (0, 0), (-1, 0), INK), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                           ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BAND]),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                           ("LEFTPADDING", (0, 0), (-1, -1), 6)]))
    story.append(t)
    story.append(PageBreak())

    # ── per-check evidence ──
    story.append(Paragraph("Evidence detail", S["TF_H"]))
    for c in ledger["checks"]:
        story.append(Paragraph(f"{c['id']} &middot; {c['title']} &nbsp; {_verdict_badge(c['verdict'])}", S["TF_Body"]))
        block = [["Requirement", c["requirement"]], ["Method", c["method"]],
                 ["Measured", _fmt(c["metrics"])], ["Budget", _fmt(c["budgets"]) or "—"],
                 ["Annex IV", c["annex_iv"]]]
        if c.get("detail"):
            block.append(["Note", c["detail"]])
        if c.get("error"):
            block.append(["Error", c["error"].splitlines()[0]])
        t = Table([[Paragraph(f"<font size=8><b>{k}</b></font>", S["TF_Body"]),
                    Paragraph(f"<font size=8>{v}</font>", S["TF_Body"])] for k, v in block],
                  colWidths=[26 * mm, 130 * mm])
        t.setStyle(TableStyle([("INNERGRID", (0, 0), (-1, -1), 0.3, RULE), ("BOX", (0, 0), (-1, -1), 0.3, RULE),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("BACKGROUND", (0, 0), (0, -1), BAND),
                               ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                               ("LEFTPADDING", (0, 0), (-1, -1), 6)]))
        story.append(t)
        story.append(Spacer(1, 7))

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 9 * mm, "ThermalFlow compliance-runner — automatically generated evidence; "
                          "not legal advice or certification")
        canvas.drawRightString(192 * mm, 9 * mm, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return out_path
