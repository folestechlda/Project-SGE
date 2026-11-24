"""PDF A4 retrato dos relatórios: texto quebra em linhas, nunca se sobrepõe."""
import io
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable

AZUL = colors.HexColor("#1e4e8c")
AZUL_CLARO = colors.HexColor("#eaf1fa")
CINZA = colors.HexColor("#6b7280")


def _txt(v, filtro_mt, fmt_qtd):
    if v is None or v == "":
        return ""
    if hasattr(v, "fat"):            # Qtd (quantidade na unidade vista)
        return fmt_qtd(v)
    if isinstance(v, bool):
        return "Sim" if v else "Não"
    if isinstance(v, (int, float)):
        return filtro_mt(v)
    return str(v)


def gerar(titulo, colunas, linhas, empresa, filtro_mt, fmt_qtd, subtitulo=None):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=12*mm, rightMargin=12*mm,
                            topMargin=12*mm, bottomMargin=14*mm, title=titulo)
    s_emp = ParagraphStyle("e", fontName="Helvetica", fontSize=9, textColor=CINZA, leading=11)
    s_tit = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=14, textColor=AZUL, leading=17, spaceAfter=2)
    s_cel = ParagraphStyle("c", fontName="Helvetica", fontSize=7.5, leading=9)
    s_celd = ParagraphStyle("cd", parent=s_cel, alignment=2)
    s_cab = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=7, leading=8.5, textColor=colors.white)
    s_cabd = ParagraphStyle("hd", parent=s_cab, alignment=2)
    s_tot = ParagraphStyle("tt", parent=s_cel, fontName="Helvetica-Bold")
    s_totd = ParagraphStyle("td", parent=s_tot, alignment=2)

    el: list = [Paragraph(escape(titulo), s_tit)]
    linha_emp = " · ".join(x for x in [empresa.get("nome"), ("NUIT " + empresa["nuit"]) if empresa.get("nuit") else None] if x)
    if subtitulo:
        linha_emp = (linha_emp + " · " if linha_emp else "") + subtitulo
    el.append(Paragraph(escape(linha_emp or ""), s_emp))
    el.append(Paragraph("Emitido em " + datetime.now().strftime("%d/%m/%Y %H:%M"), s_emp))
    el += [Spacer(1, 3*mm), HRFlowable(width="100%", thickness=1, color=AZUL), Spacer(1, 3*mm)]

    n = len(colunas)
    textos = [[_txt(c, filtro_mt, fmt_qtd) for c in (list(l) + [''] * n)[:n]] for l in linhas]
    # colunas numéricas = a maioria das células com dígitos e sem letras
    def numerica(j):
        vals = [t[j] for t in textos if t[j]]
        return bool(vals) and sum(1 for v in vals if any(ch.isdigit() for ch in v) and len(v) < 22) / len(vals) > .7
    num = [numerica(j) for j in range(len(colunas))]
    # números nunca quebram; o texto (nomes) recebe o resto da largura e quebra em linhas
    textos = [[t.replace(" ", "\u00a0") if num[j] else t for j, t in enumerate(l)] for l in textos]
    larg_util = A4[0] - 24*mm
    fixa = {}
    for j, c in enumerate(colunas):
        if num[j]:
            mx = max([max((len(w) for w in str(c).split()), default=4)] + [len(t[j]) for t in textos])
            fixa[j] = mx * 4.3 + 8
    livre = larg_util - sum(fixa.values())
    txt_cols = [j for j in range(len(colunas)) if j not in fixa]
    pesos = {j: min(max(max([len(colunas[j])] + [len(t[j]) for t in textos]), 8), 40) for j in txt_cols}
    larguras = []
    for j in range(len(colunas)):
        if j in fixa:
            larguras.append(fixa[j])
        else:
            larguras.append(max(livre, 40 * len(txt_cols)) * pesos[j] / sum(pesos.values()))
    esc = larg_util / sum(larguras)
    if esc < 1:
        larguras = [w * esc for w in larguras]

    dados: list = [[Paragraph(escape(str(c)), s_cabd if num[j] else s_cab) for j, c in enumerate(colunas)]]
    estilo = [("BACKGROUND", (0, 0), (-1, 0), AZUL), ("VALIGN", (0, 0), (-1, -1), "TOP"),
              ("LINEBELOW", (0, 1), (-1, -1), .3, colors.HexColor("#e5e7eb")),
              ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
              ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
    for i, t in enumerate(textos, start=1):
        primeira = (t[0] or "").upper()
        e_secao = bool(primeira) and primeira == (t[0] or "") and primeira.isupper() and not any(t[1:])
        e_total = primeira == "TOTAL" or primeira.startswith("TOTAL ") and not e_secao
        pt, ptd = (s_tot, s_totd) if e_total else (s_cel, s_celd)
        dados.append([Paragraph(escape(x), ptd if num[j] else pt) for j, x in enumerate(t)])
        if e_secao:
            estilo += [("BACKGROUND", (0, i), (-1, i), AZUL_CLARO), ("SPAN", (0, i), (-1, i)),
                       ("LINEBELOW", (0, i), (-1, i), .8, AZUL)]
            dados[i] = [Paragraph("<b>" + escape(t[0]) + "</b>", ParagraphStyle("s", parent=s_cel, textColor=AZUL, fontSize=8))] + [""] * (len(colunas) - 1)
        if e_total:
            estilo += [("BACKGROUND", (0, i), (-1, i), AZUL_CLARO), ("LINEABOVE", (0, i), (-1, i), 1, AZUL)]
    tabela = Table(dados, colWidths=larguras, repeatRows=1)
    tabela.setStyle(TableStyle(estilo))
    el.append(tabela)

    def rodape(c, d):
        c.saveState(); c.setFont("Helvetica", 7); c.setFillColor(CINZA)
        c.drawRightString(A4[0] - 12*mm, 8*mm, f"Página {d.page}")
        c.restoreState()
    doc.build(el, onFirstPage=rodape, onLaterPages=rodape)
    buf.seek(0)
    return buf