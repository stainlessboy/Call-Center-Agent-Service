from __future__ import annotations

import os
import tempfile
import uuid
from typing import Optional

# fpdf2 must be installed: pip install fpdf2
try:
    from fpdf import FPDF, FPDFException
    _FPDF_AVAILABLE = True
except ImportError:
    _FPDF_AVAILABLE = False


def _annuity_payment(principal: float, monthly_rate: float, n: int) -> float:
    """Compute monthly annuity payment."""
    if monthly_rate == 0:
        return principal / n
    return principal * monthly_rate * (1 + monthly_rate) ** n / ((1 + monthly_rate) ** n - 1)


def generate_amortization_pdf(
    product_name: str,
    principal: int,
    annual_rate_pct: float,
    term_months: int,
    borrower_name: str = "",
    output_dir: str = "/tmp",
) -> str:
    """
    Generate an amortization schedule PDF and return the file path.
    Returns a path like /tmp/schedule_XXXX.pdf.
    If fpdf2 is not installed, returns a .txt fallback.
    """
    if not _FPDF_AVAILABLE:
        return _generate_text_fallback(product_name, principal, annual_rate_pct, term_months, output_dir)

    monthly_rate = annual_rate_pct / 100 / 12
    payment = _annuity_payment(float(principal), monthly_rate, term_months)

    # Build amortization table
    rows = []
    balance = float(principal)
    for month in range(1, term_months + 1):
        interest = balance * monthly_rate
        principal_part = payment - interest
        balance -= principal_part
        if balance < 0:
            balance = 0.0
        rows.append((month, payment, principal_part, interest, max(balance, 0.0)))

    # Build PDF
    pdf = FPDF()
    pdf.add_page()

    # Try to add unicode font for Cyrillic
    # DejaVu is included with fpdf2 but we check for it
    try:
        import fpdf.fonts  # noqa
        font_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "fonts")
        dejavu_path = os.path.join(font_dir, "DejaVuSans.ttf")
        if os.path.exists(dejavu_path):
            pdf.add_font("DejaVu", "", dejavu_path, uni=True)
            pdf.add_font("DejaVu", "B", dejavu_path, uni=True)
            font_name = "DejaVu"
        else:
            # fpdf2 >= 2.7 ships with Helvetica that handles Latin but not Cyrillic
            # Fall back to built-in but transliterate
            font_name = "Helvetica"
    except Exception:
        font_name = "Helvetica"

    # Title
    pdf.set_font(font_name, "B", 14)
    title = _safe_text(f"График платежей: {product_name}", font_name)
    pdf.cell(0, 10, title, ln=True, align="C")
    pdf.ln(2)

    # Metadata
    pdf.set_font(font_name, "", 10)
    if borrower_name:
        pdf.cell(0, 7, _safe_text(f"Заёмщик: {borrower_name}", font_name), ln=True)
    pdf.cell(0, 7, _safe_text(f"Сумма кредита: {principal:,} сум".replace(",", " "), font_name), ln=True)
    pdf.cell(0, 7, _safe_text(f"Годовая ставка: {annual_rate_pct:.1f}%", font_name), ln=True)
    pdf.cell(0, 7, _safe_text(f"Срок: {term_months} мес.", font_name), ln=True)
    pdf.cell(0, 7, _safe_text(f"Ежемесячный платёж: {payment:,.0f} сум".replace(",", " "), font_name), ln=True)
    pdf.ln(4)

    # Table header
    pdf.set_font(font_name, "B", 9)
    col_w = [15, 35, 40, 35, 45]
    headers = ["Мес.", "Платёж", "Осн. долг", "Проценты", "Остаток"]
    for i, h in enumerate(headers):
        pdf.cell(col_w[i], 7, _safe_text(h, font_name), border=1, align="C")
    pdf.ln()

    # Table rows
    pdf.set_font(font_name, "", 8)
    for row in rows:
        month, pmt, princ, inter, bal = row
        values = [
            str(month),
            f"{pmt:,.0f}".replace(",", " "),
            f"{princ:,.0f}".replace(",", " "),
            f"{inter:,.0f}".replace(",", " "),
            f"{bal:,.0f}".replace(",", " "),
        ]
        aligns = ["C", "R", "R", "R", "R"]
        for i, val in enumerate(values):
            pdf.cell(col_w[i], 6, val, border=1, align=aligns[i])
        pdf.ln()

    # Total row
    total_payment = sum(r[1] for r in rows)
    total_interest = sum(r[3] for r in rows)
    pdf.set_font(font_name, "B", 8)
    pdf.cell(col_w[0], 6, _safe_text("Итого", font_name), border=1, align="C")
    pdf.cell(col_w[1], 6, f"{total_payment:,.0f}".replace(",", " "), border=1, align="R")
    pdf.cell(col_w[2], 6, f"{principal:,.0f}".replace(",", " "), border=1, align="R")
    pdf.cell(col_w[3], 6, f"{total_interest:,.0f}".replace(",", " "), border=1, align="R")
    pdf.cell(col_w[4], 6, "0", border=1, align="R")
    pdf.ln()

    # Save
    os.makedirs(output_dir, exist_ok=True)
    filename = f"schedule_{uuid.uuid4().hex[:8]}.pdf"
    path = os.path.join(output_dir, filename)
    pdf.output(path)
    return path


_TRANSLIT: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "'", "ы": "y", "ь": "'", "э": "e", "ю": "yu", "я": "ya",
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E", "Ё": "Yo",
    "Ж": "Zh", "З": "Z", "И": "I", "Й": "Y", "К": "K", "Л": "L", "М": "M",
    "Н": "N", "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T", "У": "U",
    "Ф": "F", "Х": "Kh", "Ц": "Ts", "Ч": "Ch", "Ш": "Sh", "Щ": "Sch",
    "Ъ": "'", "Ы": "Y", "Ь": "'", "Э": "E", "Ю": "Yu", "Я": "Ya",
}


def _safe_text(text: str, font_name: str) -> str:
    """If font doesn't support Cyrillic, transliterate using a dict lookup."""
    if font_name != "Helvetica":
        return text
    return "".join(_TRANSLIT.get(c, c) for c in text)


def _generate_text_fallback(
    product_name: str,
    principal: int,
    annual_rate_pct: float,
    term_months: int,
    output_dir: str,
) -> str:
    """Generate a plain text payment schedule when fpdf2 is unavailable."""
    monthly_rate = annual_rate_pct / 100 / 12
    payment = _annuity_payment(float(principal), monthly_rate, term_months)

    lines = [
        f"График платежей: {product_name}",
        f"Сумма: {principal:,} сум",
        f"Ставка: {annual_rate_pct:.1f}% годовых",
        f"Срок: {term_months} мес.",
        f"Ежемесячный платёж: {payment:,.0f} сум",
        "",
        f"{'Мес.':<6} {'Платёж':<15} {'Осн.долг':<15} {'Проценты':<15} {'Остаток':<15}",
        "-" * 66,
    ]
    balance = float(principal)
    for month in range(1, term_months + 1):
        interest = balance * monthly_rate
        p = payment - interest
        balance -= p
        lines.append(
            f"{month:<6} {payment:<15,.0f} {p:<15,.0f} {interest:<15,.0f} {max(balance, 0):<15,.0f}"
        )

    os.makedirs(output_dir, exist_ok=True)
    filename = f"schedule_{uuid.uuid4().hex[:8]}.txt"
    path = os.path.join(output_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path
