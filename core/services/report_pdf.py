"""PDF report for backtest."""
import io
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

def build_report(metrics: dict, trade_list: list, symbol: str, start: str, end: str, walk_forward: dict=None, monte: dict=None) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=12*mm, bottomMargin=12*mm)
    styles = getSampleStyleSheet()
    story = []
    story.append(Paragraph(f"RaTrade Backtest Report - {symbol} {start} to {end}", styles['Title']))
    story.append(Spacer(1, 6*mm))
    # metrics table
    rows = [["Metric","Value"]]
    for k in ["total_return","total_return_pct","win_rate","profit_factor","sharpe_ratio","sortino_ratio","calmar_ratio","max_drawdown","avg_win","avg_loss","total_trades","total_brokerage"]:
        rows.append([k, str(metrics.get(k,"-"))])
    if walk_forward:
        rows.append(["WF avg_efficiency", str(walk_forward.get("avg_efficiency","-"))])
    if monte:
        rows.append(["MC prob_profit", str(monte.get("prob_profit","-"))+"%"])
    t = Table(rows, colWidths=[70*mm, 70*mm])
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.lightgrey),('GRID',(0,0),(-1,-1),0.5,colors.grey),('FONTSIZE',(0,0),(-1,-1),8)]))
    story.append(t)
    story.append(Spacer(1, 6*mm))
    # trades
    story.append(Paragraph("Trades (first 40)", styles['Heading2']))
    tr = [["#","Entry","Exit","Type","Strike","PnL"]]
    for i, trad in enumerate(trade_list[:40],1):
        tr.append([str(i), str(trad.get("entry_date","")), str(trad.get("exit_date","")), str(trad.get("position","")), str(trad.get("strike","")), str(trad.get("pnl",""))])
    tt = Table(tr, colWidths=[10*mm,25*mm,25*mm,20*mm,20*mm,25*mm])
    tt.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.lightgrey),('GRID',(0,0),(-1,-1),0.25,colors.grey),('FONTSIZE',(0,0),(-1,-1),6)]))
    story.append(tt)
    doc.build(story)
    return buf.getvalue()
