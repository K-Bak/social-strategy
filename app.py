import locale

# Prøv dansk locale, ellers brug systemets default
try:
    locale.setlocale(locale.LC_ALL, 'da_DK.UTF-8')
except locale.Error:
    try:
        locale.setlocale(locale.LC_ALL, 'da_DK.utf8')
    except locale.Error:
        locale.setlocale(locale.LC_ALL, '')
import asyncio
import io
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
import openai
import pandas as pd
import plotly.express as px
import streamlit as st
# --- FORCE LIGHT THEME IN STREAMLIT CLOUD ---
try:
    st._config.set_option("theme.base", "light")
    st._config.set_option("theme.primaryColor", "#003DFF")
    st._config.set_option("theme.backgroundColor", "#FFFFFF")
    st._config.set_option("theme.secondaryBackgroundColor", "#F8F9FF")
    st._config.set_option("theme.textColor", "#000000")
except Exception:
    pass
from bs4 import BeautifulSoup
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import docx

# ---------- SETTINGS ----------
APP_TITLE = "Social Strategi"
APP_DESC = "Bygger en Meta-strategi baseret på Xpect, inputs, website-scraping og konkurrentindsigt."
DEFAULT_STARTDATE = (datetime.today() + timedelta(days=7)).date()  # d.d. + 7 dage
CUSTOMER_MAX_PAGES = 12
COMPETITOR_MAX_PAGES = 15
REQUEST_TIMEOUT = 30_000  # ms

# ---------- SCRAPING (Playwright) ----------
# We use Playwright for high-fidelity scraping incl. JS-rendered content.
# NOTE: Requires playwright + browser install (e.g., `pip install playwright` and `playwright install`)

def _clean_text(txt: str) -> str:
    if not txt:
        return ""
    return " ".join(" ".join(txt.split()).split())

def _is_internal_link(base_netloc: str, href: str) -> bool:
    try:
        p = urlparse(href)
        if not p.netloc or p.netloc == base_netloc:
            return True
        return False
    except Exception:
        return False

def _should_skip_url(url: str) -> bool:
    url_l = url.lower()
    SKIP_PARTS = [
        "login", "signin", "cart", "basket", "checkout", "privacy", "cookie",
        "terms", "conditions", "policy", "job", "career", "news", "blog", "press",
        "newsletter", "wp-json", "cdn", "tag", "author", "attachment"
    ]
    return any(s in url_l for s in SKIP_PARTS)

def _extract_tracking(html: str) -> dict:
    lower = html.lower()
    return {
        "ga4": ("gtag(" in lower) or ("google-analytics" in lower),
        "gtm": "gtm.js" in lower or "googletagmanager.com" in lower,
        "meta_pixel": ("fbq(" in lower) or ("facebook.com/tr" in lower) or ("facebook pixel" in lower),
        "klaviyo": "klaviyo" in lower or "klaviyo.com" in lower,
        "hotjar": "hotjar" in lower,
    }

def _extract_page_features(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title = _clean_text(soup.title.text if soup.title else "")
    meta_desc = ""
    md = soup.find("meta", attrs={"name": "description"})
    if md and md.get("content"):
        meta_desc = _clean_text(md["content"])

    # Headings
    h1 = [_clean_text(h.get_text(" ")) for h in soup.find_all("h1")]
    h2 = [_clean_text(h.get_text(" ")) for h in soup.find_all("h2")]
    h3 = [_clean_text(h.get_text(" ")) for h in soup.find_all("h3")]

    # Visible text (cap length)
    text_chunks = []
    for el in soup.find_all(string=True):
        # Skip script/style/noscript
        if el.parent.name in ["script", "style", "noscript"]:
            continue
        t = _clean_text(el)
        if t:
            text_chunks.append(t)
    visible_text = _clean_text(" ".join(text_chunks))[:1500]

    # CTA candidates (links + buttons)
    ctas = []
    for a in soup.find_all("a"):
        label = _clean_text(a.get_text(" "))
        href = a.get("href", "")
        if not label and not href:
            continue
        if any(x in label.lower() for x in ["kontakt", "bestil", "book", "tilmeld", "køb", "prøv", "få tilbud", "ring"]):
            ctas.append({"label": label, "href": href})
    for b in soup.find_all(["button"]):
        label = _clean_text(b.get_text(" "))
        if any(x in label.lower() for x in ["kontakt", "bestil", "book", "tilmeld", "køb", "prøv", "få tilbud", "ring"]):
            ctas.append({"label": label, "href": ""})

    tracking = _extract_tracking(html)

    return {
        "url": url,
        "title": title,
        "meta_description": meta_desc,
        "h1": h1,
        "h2": h2,
        "h3": h3,
        "visible_text": visible_text,
        "ctas": ctas[:20],
        "tracking": tracking,
    }

async def _fetch_page(playwright, url: str) -> dict:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError  # type: ignore
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await page.goto(url, timeout=REQUEST_TIMEOUT, wait_until="networkidle")
        html = await page.content()
        feats = _extract_page_features(url, html)
        # Extract internal links
        anchors = await page.eval_on_selector_all("a", "els => els.map(a => a.href)")
        await context.close()
        await browser.close()
        return {"url": url, "html": html, "features": feats, "links": anchors}
    except Exception:
        try:
            await context.close()
            await browser.close()
        except Exception:
            pass
        return {"url": url, "html": "", "features": _extract_page_features(url, ""), "links": []}

async def _crawl_site_rooted(start_url: str, max_pages: int = 8) -> list[dict]:
    from playwright.async_api import async_playwright  # type: ignore
    parsed = urlparse(start_url)
    base_netloc = parsed.netloc
    seen = set()
    queue = [start_url]
    pages = []

    async with async_playwright() as pw:
        while queue and len(pages) < max_pages:
            url = queue.pop(0)
            if url in seen or _should_skip_url(url):
                continue
            seen.add(url)
            res = await _fetch_page(pw, url)
            pages.append(res["features"])
            # Link discovery
            for link in res.get("links", [])[:200]:
                if not link:
                    continue
                if not _is_internal_link(base_netloc, link):
                    continue
                if _should_skip_url(link):
                    continue
                if link not in seen and len(pages) + len(queue) < max_pages:
                    queue.append(link)
    return pages

async def _crawl_competitor(url: str) -> list[dict]:
    # Wrapper for concurrent competitor crawling
    return await _crawl_site_rooted(url, max_pages=COMPETITOR_MAX_PAGES)

async def scrape_customer_and_competitors(customer_url: str, competitor_urls: list[str]) -> dict:
    # Crawl customer
    customer_pages = await _crawl_site_rooted(customer_url, max_pages=CUSTOMER_MAX_PAGES)
    # Crawl competitors concurrently
    comp_results = {}
    if competitor_urls:
        tasks = []
        for cu in competitor_urls:
            cu = cu.strip()
            if not cu:
                continue
            tasks.append(_crawl_competitor(cu))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, cu in enumerate([u.strip() for u in competitor_urls if u.strip()]):
            if isinstance(results[i], Exception):
                comp_results[cu] = []
            else:
                comp_results[cu] = results[i]
    return {"customer": customer_pages, "competitors": comp_results}

def run_scrape(customer_url: str, competitor_urls: list[str]) -> dict:
    import nest_asyncio
    nest_asyncio.apply()
    try:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Create a dedicated loop to avoid "This event loop is already running"
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                result = new_loop.run_until_complete(scrape_customer_and_competitors(customer_url, competitor_urls))
                new_loop.close()
            else:
                result = loop.run_until_complete(scrape_customer_and_competitors(customer_url, competitor_urls))
        except RuntimeError:
            # No event loop present
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(scrape_customer_and_competitors(customer_url, competitor_urls))
        st.success("✅ Scraping færdig!")
        return result
    except Exception as e:
        st.error(f"Fejl under scraping: {e}")
        return {"customer": [], "competitors": {}}

# ---------- AI / OPENAI INTEGRATION ----------

from openai import OpenAI

def call_openai_api(prompt: str, api_key: str, model="gpt-4o-mini", max_tokens=1500, temperature=0.7) -> str:
    from openai import OpenAI
    import traceback

    try:
        client = OpenAI(api_key=api_key)
        prompt = prompt.encode("utf-8", errors="ignore").decode("utf-8")

        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
        )

        raw_output = response.choices[0].message.content or ""

        # Fjern problematiske unicode-tegn
        replacements = {
            "\u2013": "-", "\u2014": "-", "\u2022": "*",
            "\u2028": " ", "\u2029": " ", "\u2018": "'",
            "\u2019": "'", "\u201C": '"', "\u201D": '"',
            "\u2026": "...", "\xa0": " ",
        }
        for k, v in replacements.items():
            raw_output = raw_output.replace(k, v)

        cleaned_output = raw_output.encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")

        return cleaned_output.strip()

    except Exception as e:
        tb = traceback.format_exc()
        st.error("🚨 Fejl under AI-kald")
        st.exception(e)
        st.text(tb)
        return f"AI-fejl: {str(e)}"

def generate_strategy_text(customer_data: list[dict], competitor_data: dict, xpect_text: str, important_services: str) -> str:
    prompt = (
        "Du er senior social strategist for META (Facebook/Instagram) – IGNORÉR alle andre kanaler (SEO, Google Ads, TikTok, LinkedIn, YouTube, Display, Web, Email mv.).\n"
        "Byg KUN en Meta-strategi.\n\n"
        "Lever i dansk markdown uden kodeblokke:\n"
        "1) Executive summary (max 8 bullets)\n"
        "2) USP’er (top 6) + tone of voice (3-5 ord)\n"
        "3) META-funnel (Awareness → Consideration → Conversion → Loyalty) med forslag til kampagner/placeringer/formater\n"
        "4) KPI’er og målepunkter (kun Meta)\n"
        "5) Risici/forudsætninger\n\n"
        "Grundlag for analysen:\n"
    )
    prompt += "Kundens data (uddrag fra sider):\n"
    for page in customer_data[:6]:
        prompt += f"- Titel: {page.get('title','')}\n"
        prompt += f"  Meta: {page.get('meta_description','')[:180]}\n"
        prompt += f"  H1: {', '.join(page.get('h1',[])[:3])}\n"
    prompt += "\nKonkurrentdata (oversigt):\n"
    for c_url, pages in competitor_data.items():
        prompt += f"- {c_url}: {len(pages)} sider\n"
    prompt += f"\nVigtige services/undersider: {important_services}\n"
    if xpect_text:
        prompt += f"\nXpect tekst (uddrag):\n{xpect_text[:2000]}\n"
    prompt += "\nSkriv KUN om Meta. Brug ikke ord som SEO/Google/TikTok i anbefalingerne.\n"
    return prompt

def generate_campaign_plan_text(strategy_text: str, antal_kampagner: int) -> str:
    prompt = (
        "Byg en 12-måneders Meta kampagneplan baseret på strategien nedenfor.\n"
        "Krav:\n"
        "- Inkludér 2 always-on kampagner: 'Brand Awareness' og 'Retargeting & Loyalty'\n"
        f"- Derudover max {max(0, antal_kampagner - 2)} sæson-/tema-kampagner med konkrete navne (korte, klare)\n"
        "- Angiv for hver kampagne: formål, primære placeringer/formater på Meta, primær målgruppe, estimeret % af månedligt budget (fx 'Budget: 20%'), foreslået periode (måneder)\n"
        "- Tilføj tydelig label med budgetprocent pr. kampagne, fx 'Budget: 20%'.\n"
        "- Skriv KUN om Meta (ingen SEO/Google Ads/andre kanaler)\n\n"
        "Strategi:\n"
        f"{strategy_text}\n"
    )
    return prompt

def generate_ad_texts_text(strategy_text: str) -> str:
    # Udtræk alle kampagner fra campaign_plan_text, så der genereres tekster for alle fem kampagner
    import streamlit as st
    campaign_plan_text = st.session_state.get("campaign_plan_text", "")
    import re
    kampagner = []
    if campaign_plan_text:
        # Find "- Kampagnenavn" i campaign_plan_text
        for m in re.finditer(r"\n- ([^\n:]+)", "\n" + campaign_plan_text):
            navn = m.group(1).strip()
            if navn and navn not in kampagner:
                kampagner.append(navn)
    if not kampagner:
        kampagner = ["Brand Awareness", "Retargeting & Loyalty"]
    prompt = (
        "Skriv annoncetekster til Meta (Facebook/Instagram) for følgende kampagner:\n"
        + "\n".join(f"- {k}" for k in kampagner) +
        "\nFor hver kampagne lever 3-4 varianter i følgende struktur:\n"
        "- Hook (max 90 tegn)\n"
        "- Primærtekst (4-6 linjer, emotionel, visuel og overbevisende – skriv som en Meta-annonce med fokus på storytelling, følelser og call-to-action)\n"
        "- Overskrift (max 40 tegn)\n"
        "- CTA (vælg fra: Læs mere, Køb nu, Tilmeld, Book tid, Send besked)\n"
        "- Tilføj derefter en vurdering af hvor stærk annonceteksten er fra 0–100 baseret på performance-potentiale på Meta (brug formatet: Score: 87/100)\n"
        "Brug dansk, hold dig fra SEO/Google/andre kanaler.\n\n"
        "Strategi (kontekst):\n"
        f"{strategy_text}\n"
    )
    return prompt

# ---------- PDF GENERATION ----------

def generate_pdf(kunde, strategy_text, competitor_data, gantt_df):
    global ad_texts_text
    try:
        ad_texts_text
    except NameError:
        ad_texts_text = ""
    import os
    os.environ["PATH"] += os.pathsep + os.path.expanduser("~/.local/bin")
    buffer = io.BytesIO()
    # --- Use platypus for better layout ---
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, PageBreak, Table, TableStyle, Flowable
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    base_font = "Helvetica"
    from xml.sax.saxutils import escape

    doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=40, rightMargin=40, topMargin=50, bottomMargin=40)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='Heading1Center', parent=styles['Heading1'], alignment=TA_CENTER, fontName=base_font, fontSize=28, textColor=colors.HexColor("#003DFF")))
    styles.add(ParagraphStyle(name='Small', parent=styles['Normal'], fontSize=9, leading=11, fontName=base_font))
    # Add Body style for main text
    styles.add(ParagraphStyle(
        name='Body',
        parent=styles['Normal'],
        fontName=base_font,
        fontSize=12,
        leading=16,
        spaceBefore=4,
        spaceAfter=6,
    ))
    styles['Normal'].fontName = base_font
    styles['Normal'].spaceAfter = 6
    styles['Normal'].spaceBefore = 4
    styles['Normal'].leading = 17
    styles['Normal'].fontSize = 11
    styles['Heading2'].fontName = base_font
    styles['Heading2'].fontSize = 17
    styles['Heading2'].spaceBefore = 16
    styles['Heading2'].spaceAfter = 12
    # Section heading style with colored background
    styles.add(ParagraphStyle(
        name='SectionTitle',
        fontName=base_font,
        fontSize=17,
        textColor=colors.white,
        backColor=colors.HexColor("#003DFF"),
        leftIndent=0,
        rightIndent=0,
        spaceBefore=25,
        spaceAfter=20,
        leading=21,
        alignment=0,
        padding=6
    ))
    elements = []

    # --- FRONT PAGE ---
    class ColorRect(Flowable):
        def __init__(self, width, height, color):
            Flowable.__init__(self)
            self.width = width
            self.height = height
            self.color = color
        def draw(self):
            self.canv.setFillColor(self.color)
            self.canv.rect(0, 0, self.width, self.height, fill=1, stroke=0)

    # Add a full-page front page with Generaxion colors and Jura font
    elements.append(Spacer(1, 2.3*inch))
    elements.append(Paragraph("Social Strategirapport", ParagraphStyle(
        name="ForsideTitle",
        fontName=base_font,
        fontSize=36,
        leading=44,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#003DFF"),
        spaceAfter=20
    )))
    elements.append(Spacer(1, 0.3*inch))
    elements.append(Paragraph(f"<b>{kunde}</b>", ParagraphStyle(
        name="ForsideKunde",
        fontName=base_font,
        fontSize=24,
        leading=30,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#00CC88"),
        spaceAfter=18
    )))
    elements.append(Spacer(1, 0.1*inch))
    elements.append(Paragraph(datetime.now().strftime('%d.%m.%Y'), ParagraphStyle(
        name="ForsideDato",
        fontName=base_font,
        fontSize=16,
        leading=20,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#003DFF"),
        spaceAfter=0
    )))
    elements.append(Spacer(1, 2.2*inch))
    # Generaxion logo color bar (just a colored bar as placeholder)
    elements.append(ColorRect(6.0*inch, 0.25*inch, colors.HexColor("#003DFF")))
    elements.append(Spacer(1, 0.05*inch))
    elements.append(ColorRect(6.0*inch, 0.12*inch, colors.HexColor("#00CC88")))
    elements.append(PageBreak())

    # Indholdsfortegnelse fjernet
    # --- Strategioversigt ---
    # Markdown til Paragraph parser (###, **, - **, - osv.)
    import re
    def md_to_paragraphs(md_text, para_style='Body'):
        import re
        paragraphs = []
        lines = md_text.splitlines()
        buf = []
        in_bullet = False
        in_usps = False
        in_tone = False
        in_meta = False
        meta_bullet_lines = []
        meta_section = None
        meta_section_names = ["Awareness", "Consideration", "Conversion", "Loyalty"]
        meta_section_active = None
        kpi_section = False
        risiko_section = False
        bullet_indent = 12
        # Remove markdown symbols helper
        def strip_md(s):
            s = re.sub(r"(\*\*|\*|•|-|_)", "", s)
            return s
        idx = 0
        while idx < len(lines):
            l = lines[idx].rstrip()
            # --- Detect section starts ---
            if l.lower().startswith("usp"):
                # USP bullet section
                if buf:
                    buf_str = '\n'.join(buf)
                    paragraphs.append(Paragraph(buf_str, styles[para_style]))
                    paragraphs.append(Spacer(1, 0.15*inch))
                    buf = []
                # Gather all USP lines (until next empty or heading)
                usp_bullets = []
                while idx < len(lines):
                    line = lines[idx].strip()
                    if not line or line.startswith("#") or any(line.lower().startswith(x) for x in ["tone", "meta", "kpi", "risici"]):
                        break
                    if line.lower().startswith("usp"):
                        usp_bullets.append(strip_md(line))
                    idx += 1
                for b in usp_bullets:
                    paragraphs.append(Paragraph(f"• {b}", styles[para_style]))
                    paragraphs.append(Spacer(1, 0.1*inch))
                continue
            if l.lower().startswith("tone of voice"):
                # Tone of Voice as its own paragraph
                if buf:
                    buf_str = '\n'.join(buf)
                    paragraphs.append(Paragraph(buf_str, styles[para_style]))
                    paragraphs.append(Spacer(1, 0.15*inch))
                    buf = []
                tone_str = strip_md(l.split(":",1)[-1].strip()) if ":" in l else strip_md(l)
                paragraphs.append(Paragraph(f"<b>Tone of Voice:</b> {tone_str}", styles['Body']))
                paragraphs.append(Spacer(1, 0.15*inch))
                idx += 1
                continue
            # META-funnel
            is_meta = False
            for meta_name in meta_section_names:
                if l.strip().lower().startswith(meta_name.lower()):
                    is_meta = meta_name
                    break
            if is_meta:
                if buf:
                    buf_str = '\n'.join(buf)
                    paragraphs.append(Paragraph(buf_str, styles[para_style]))
                    paragraphs.append(Spacer(1, 0.15*inch))
                    buf = []
                # Section heading as bold
                paragraphs.append(Paragraph(f"<b>{is_meta}</b>", styles[para_style]))
                paragraphs.append(Spacer(1, 0.12*inch))
                # Gather bullets under this section
                meta_bullet_lines = []
                idx += 1
                while idx < len(lines):
                    l2 = lines[idx].strip()
                    if not l2 or l2.startswith("#") or any(l2.strip().lower().startswith(x.lower()) for x in meta_section_names if x != is_meta):
                        break
                    # Any non-empty line is a bullet
                    if l2:
                        l2_clean = strip_md(l2)
                        paragraphs.append(Paragraph(f"• {l2_clean}", styles[para_style]))
                        paragraphs.append(Spacer(1, 0.1*inch))
                    idx += 1
                continue
            # KPI’er og Risici
            if l.lower().startswith("kpi"):
                if buf:
                    buf_str = '\n'.join(buf)
                    paragraphs.append(Paragraph(buf_str, styles[para_style]))
                    paragraphs.append(Spacer(1, 0.15*inch))
                    buf = []
                paragraphs.append(Paragraph("<b>KPI’er og målepunkter</b>", styles[para_style]))
                paragraphs.append(Spacer(1, 0.12*inch))
                idx += 1
                while idx < len(lines):
                    l2 = lines[idx].strip()
                    if not l2 or l2.startswith("#") or l2.lower().startswith("risici"):
                        break
                    if l2:
                        l2_clean = strip_md(l2)
                        paragraphs.append(Paragraph(f"• {l2_clean}", styles[para_style]))
                        paragraphs.append(Spacer(1, 0.1*inch))
                    idx += 1
                continue
            if l.lower().startswith("risici"):
                if buf:
                    buf_str = '\n'.join(buf)
                    paragraphs.append(Paragraph(buf_str, styles[para_style]))
                    paragraphs.append(Spacer(1, 0.15*inch))
                    buf = []
                paragraphs.append(Paragraph("<b>Risici/forudsætninger</b>", styles[para_style]))
                paragraphs.append(Spacer(1, 0.12*inch))
                idx += 1
                while idx < len(lines):
                    l2 = lines[idx].strip()
                    if not l2 or l2.startswith("#"):
                        break
                    if l2:
                        l2_clean = strip_md(l2)
                        paragraphs.append(Paragraph(f"• {l2_clean}", styles[para_style]))
                        paragraphs.append(Spacer(1, 0.1*inch))
                    idx += 1
                continue
            # Headings
            if l.startswith('###'):
                if buf:
                    paragraphs.append(Paragraph('\n'.join(buf), styles[para_style]))
                    paragraphs.append(Spacer(1, 0.15*inch))
                    buf = []
                para = Paragraph(strip_md(l[3:].strip()), styles['SectionTitle'])
                paragraphs.append(para)
                paragraphs.append(Spacer(1, 0.25*inch))
                idx += 1
                continue
            if l.startswith('##'):
                if buf:
                    paragraphs.append(Paragraph('\n'.join(buf), styles[para_style]))
                    paragraphs.append(Spacer(1, 0.15*inch))
                    buf = []
                para = Paragraph(f"<b>{strip_md(l[2:].strip())}</b>", styles[para_style])
                paragraphs.append(para)
                idx += 1
                continue
            if l.startswith('#'):
                if buf:
                    paragraphs.append(Paragraph('\n'.join(buf), styles[para_style]))
                    paragraphs.append(Spacer(1, 0.15*inch))
                    buf = []
                para = Paragraph(f"<b>{strip_md(l[1:].strip())}</b>", styles[para_style])
                paragraphs.append(para)
                idx += 1
                continue
            # Bullets
            m = re.match(r"^[-*]\s+(.*)", l)
            if m:
                bullet = f"{strip_md(m.group(1))}"
                buf.append(bullet)
                in_bullet = True
                idx += 1
                continue
            # Remove markdown from labels
            l = re.sub(r"^\*\*(.+?)\*\*:", r"<b>\1</b>:", l)
            l = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", l)
            l = re.sub(r"\*(.+?)\*", r"<i>\1</i>", l)
            l = strip_md(l)
            buf.append(l)
            idx += 1
        if buf:
            buf_str = '\n'.join(buf)
            paragraphs.append(Paragraph(buf_str, styles[para_style]))
            paragraphs.append(Spacer(1, 0.15*inch))
        return paragraphs

    clean_strategy_paragraphs = md_to_paragraphs(strategy_text or "")
    elements.append(Paragraph("Strategioversigt", styles['SectionTitle']))
    elements.append(Spacer(1, 0.25*inch))
    for para in clean_strategy_paragraphs:
        elements.append(para)

    # --- Fjernet konkurrentanalyse-sektion ---
    # Spring direkte fra strategioversigt til Gantt-grafen med passende spacing
    elements.append(Spacer(1, 0.3*inch))

    # Gantt: ny tabel med kampagne, startdato, slutdato, varighed (dage)
    if not gantt_df.empty:
        elements.append(Paragraph("Årshjul for kampagner", styles['SectionTitle']))
        elements.append(Spacer(1, 0.25*inch))
        kamp_rows = []
        for idx, row in gantt_df.iterrows():
            kamp_rows.append([
                row["Kampagne"],
                row["Start"].strftime('%d.%m.%Y'),
                row["Slut"].strftime('%d.%m.%Y'),
                str(row["Varighed"])
            ])
        data = [["Kampagne", "Startdato", "Slutdato", "Varighed (dage)"]] + kamp_rows
        elements.append(Spacer(1, 0.25*inch))
        t = Table(data, hAlign='CENTER', colWidths=[2.5*inch, 1.2*inch, 1.2*inch, 1.1*inch])
        # Blå header, hvid tekst, skiftende baggrund, grid, box, centreret, padding, højrestil sidste kolonne, vekslende baggrund
        row_count = len(data) - 1
        backgrounds = []
        for i in range(row_count):
            if i % 2 == 0:
                backgrounds.append(colors.white)
            else:
                backgrounds.append(colors.HexColor("#F2F5FF"))
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#003DFF")),
            ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('FONTNAME', (0,0), (-1,0), "Helvetica-Bold"),
            ('FONTSIZE', (0,0), (-1,0), 11),
            ('ALIGN', (0,0), (-1,0), 'CENTER'),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor("#F2F5FF")]),
            ('FONTNAME', (0,1), (-1,-1), "Helvetica"),
            ('FONTSIZE', (0,1), (-1,-1), 10),
            ('ALIGN', (0,1), (-2,-1), 'CENTER'),
            ('ALIGN', (-1,1), (-1,-1), 'RIGHT'),
            ('BOTTOMPADDING', (0,0), (-1,0), 6),
            ('TOPPADDING', (0,0), (-1,0), 4),
            ('BOTTOMPADDING', (0,1), (-1,-1), 4),
            ('TOPPADDING', (0,1), (-1,-1), 4),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor("#DDDDDD")),
            ('BOX', (0,0), (-1,-1), 1, colors.HexColor("#003DFF")),
        ]))
        elements.append(t)
        elements.append(Spacer(1, 0.2*inch))

    # Efter kampagnetabel: indsæt målgrupper og annoncetekster
    from reportlab.platypus import PageBreak
    elements.append(PageBreak())
    elements.append(Paragraph("Målgrupper og annoncetekster", styles['SectionTitle']))
    elements.append(Spacer(1, 0.25*inch))
    # ad_texts_text fra session eller global fallback
    ad_texts = ""
    try:
        import streamlit as st
        ad_texts = st.session_state.ad_texts_text or ad_texts_text or ""
    except Exception:
        ad_texts = ad_texts_text if 'ad_texts_text' in globals() else ""
    # --- Indsæt målgrupper for hver kampagne før annoncetekster ---
    campaign_plan_text = ""
    try:
        import streamlit as st
        campaign_plan_text = st.session_state.get("campaign_plan_text", "")
    except Exception:
        campaign_plan_text = ""
    kampagne_målgrupper = {}
    if campaign_plan_text:
        # Find blokke: kampagnenavn, målgruppe
        blocks = re.split(r"\n- ", "\n" + campaign_plan_text)
        for b in blocks:
            lines = b.strip().split("\n")
            if not lines or not lines[0]:
                continue
            navn = lines[0].strip().replace(":", "")
            mg = ""
            for l in lines:
                if "målgruppe" in l.lower():
                    mg = l.split(":",1)[-1].strip()
            if navn and mg:
                kampagne_målgrupper[navn] = mg

    # Fjern linjer der indeholder "Kun-Meta" eller "Download PDF-rapport"
    ad_texts_lines = []
    for line in (ad_texts or "").split('\n'):
        if "kun-meta" in line.lower() or "download pdf-rapport" in line.lower():
            continue
        ad_texts_lines.append(line)

    # Annoncetekster: regex-parse og formatér
    import re
    current_campaign = None
    in_variant = False
    variant_pattern = re.compile(r"^\**\s*Variant\s*\d+\**", re.IGNORECASE)
    hook_pattern = re.compile(r"^\-?\s*\**Hook:\**\s*(.+)", re.IGNORECASE)
    prim_pattern = re.compile(r"^\-?\s*\**Primærtekst:\**\s*(.+)", re.IGNORECASE)
    overskrift_pattern = re.compile(r"^\-?\s*\**Overskrift:\**\s*(.+)", re.IGNORECASE)
    cta_pattern = re.compile(r"^\-?\s*\**CTA:\**\s*(.+)", re.IGNORECASE)
    score_pattern = re.compile(r"^\-?\s*\**Score:\**\s*(.+)", re.IGNORECASE)
    def clean_line(line):
        s = line.strip()
        s = re.sub(r"^\-+\s*", "", s)
        s = re.sub(r"^•\s*", "", s)
        s = re.sub(r"(\*\*|\*|_|-)", "", s)
        return s
    idx = 0
    while idx < len(ad_texts_lines):
        line = ad_texts_lines[idx]
        s = clean_line(line)
        if not s:
            elements.append(Spacer(1, 0.15*inch))
            idx += 1
            continue
        # Fjern ### fra kampagneoverskrifter
        if s.startswith("###"):
            s = s.replace("###", "").strip()
        # Kampagne section
        if s.lower().startswith("kampagne"):
            kampnavn = s.replace("Kampagne", "", 1).strip(": ").strip()
            if idx != 0:
                elements.append(Spacer(1, 0.25*inch))
            elements.append(Paragraph(kampnavn, styles['SectionTitle']))
            elements.append(Spacer(1, 0.25*inch))
            current_campaign = kampnavn
            # Indsæt målgruppe hvis findes
            if kampnavn in kampagne_målgrupper:
                elements.append(Paragraph(f"<b>Målgruppe:</b> {kampagne_målgrupper[kampnavn]}", styles['Body']))
                elements.append(Spacer(1, 0.15*inch))
            idx += 1
            continue
        # Variant
        if variant_pattern.match(s):
            in_variant = True
            variant_title = re.sub(r"[*#]", "", s)
            elements.append(Paragraph(f"<b>{variant_title.strip()}:</b>", styles['Body']))
            elements.append(Spacer(1, 0.15*inch))
            idx += 1
            continue
        # Hook
        m = hook_pattern.match(s)
        if m:
            elements.append(Paragraph(f"<b>Hook:</b> {m.group(1)}", styles['Body']))
            elements.append(Spacer(1, 0.1*inch))
            idx += 1
            continue
        # Primærtekst
        m = prim_pattern.match(s)
        if m:
            elements.append(Paragraph(f"<b>Primærtekst:</b> {m.group(1)}", styles['Body']))
            elements.append(Spacer(1, 0.1*inch))
            idx += 1
            continue
        # Overskrift
        m = overskrift_pattern.match(s)
        if m:
            elements.append(Paragraph(f"<b>Overskrift:</b> {m.group(1)}", styles['Body']))
            elements.append(Spacer(1, 0.1*inch))
            idx += 1
            continue
        # CTA
        m = cta_pattern.match(s)
        if m:
            elements.append(Paragraph(f"<b>CTA:</b> {m.group(1)}", styles['Body']))
            elements.append(Spacer(1, 0.1*inch))
            idx += 1
            continue
        # Score
        m = score_pattern.match(s)
        if m:
            t = Table([[f"Score: {m.group(1)}"]], colWidths=[1.3*inch], hAlign='CENTER')
            t.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,-1), colors.HexColor("#E6F0FF")),
                ('TEXTCOLOR', (0,0), (-1,-1), colors.HexColor("#003DFF")),
                ('FONTNAME', (0,0), (-1,-1), "Helvetica-Bold"),
                ('FONTSIZE', (0,0), (-1,-1), 10),
                ('ALIGN', (0,0), (-1,-1), 'CENTER'),
                ('BOTTOMPADDING', (0,0), (-1,-1), 4),
                ('TOPPADDING', (0,0), (-1,-1), 4),
            ]))
            elements.append(t)
            elements.append(Spacer(1, 0.15*inch))
            idx += 1
            continue
        # Alt andet: Body-style paragraph, ingen markdown-rester
        elements.append(Paragraph(s, styles['Body']))
        elements.append(Spacer(1, 0.1*inch))
        idx += 1

    # --- Footer på hver side ---
    def footer(canvas, doc):
        canvas.saveState()
        # Blå linje
        canvas.setStrokeColor(colors.HexColor("#003DFF"))
        canvas.setLineWidth(1.3)
        canvas.line(40, 42, doc.pagesize[0] - 40, 42)
        # Footer text
        try:
            canvas.setFont(base_font, 8)
        except Exception:
            canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#003DFF"))
        page_num = canvas.getPageNumber()
        dato_str = datetime.now().strftime('%d.%m.%Y')
        canvas.drawString(40, 28, f"Oprettet d. {dato_str} – Side {page_num}")
        canvas.drawRightString(doc.pagesize[0] - 40, 28, "Genereret automatisk af Generaxion Social Strategi")
        canvas.restoreState()

    # ----- JUSTERINGER -----
    # Før build: begræns alle Spacer heights til max 0.2*inch
    for el in elements:
        if hasattr(el, 'height') and isinstance(el, Spacer):
            if el.height > 0.2 * inch:
                el.height = 0.2 * inch
    from reportlab.platypus.doctemplate import LayoutError
    try:
        try:
            doc.multiBuild(elements, onLaterPages=footer, onFirstPage=footer)
        finally:
            buffer.seek(0)
    except LayoutError:
        # Prøv igen med alle spacers begrænset til 0.2*inch
        for el in elements:
            if hasattr(el, 'height') and isinstance(el, Spacer):
                if el.height > 0.2 * inch:
                    el.height = 0.2 * inch
        try:
            try:
                doc.build(elements, onLaterPages=footer, onFirstPage=footer)
            finally:
                buffer.seek(0)
        except Exception:
            buffer.seek(0)
    # Efter PDF-build: fjern linjer i ad_texts_text som ikke slutter på punktum, ! eller ?
    import re
    if isinstance(ad_texts_text, str):
        filtered_lines = [l for l in ad_texts_text.splitlines() if re.match(r'.*[.!?]$', l.strip()) or not l.strip()]
        ad_texts_text = "\n".join(filtered_lines)
    return buffer


# ---------- STREAMLIT APP ----------

st.set_page_config(page_title=APP_TITLE, layout="wide")
st.session_state["theme"] = {"base": "light"}
# ---------- GLOBAL STYLE OVERRIDES ----------
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Jura:wght@300;400;500;600;700&display=swap');

/* ====== GLOBAL STYLING ====== */
html, body, [class*="st-"], div, p, span, h1, h2, h3, h4, h5, h6, button, input, textarea, label {
    font-family: 'Jura', sans-serif !important;
    color: #000000 !important;
}

/* ====== LYS BAGGRUND ====== */
[data-testid="stAppViewContainer"],
[data-testid="stHeader"],
[data-testid="stSidebar"],
[data-testid="stToolbar"] {
    background-color: #FFFFFF !important;
}

/* ====== INPUTFELTER & KNAPPER ====== */
input, textarea, select, .stTextInput > div > div > input, .stNumberInput input, 
.stTextArea textarea, .stDateInput input, .stDownloadButton button {
    background-color: #FFFFFF !important;
    color: #000000 !important;
    border: 1px solid #CCCCCC !important;
    border-radius: 6px !important;
    padding: 6px 10px !important;
}

/* File uploader */
.stFileUploader {
    background-color: #FFFFFF !important;
    border: 1px solid #CCCCCC !important;
    border-radius: 6px !important;
    padding: 8px !important;
}

.stFileUploader label div[data-testid="stFileUploadDropzone"] {
    background-color: #FFFFFF !important;
    border: 1px dashed #003DFF !important;
    color: #000000 !important;
}

.stFileUploader label div[data-testid="stFileUploadDropzone"]:hover {
    background-color: #F2F5FF !important;
    border-color: #002ECC !important;
}

/* Overstyr Streamlit upload-knap (Browse files) */
[data-testid="stFileUploadDropzone"] button {
    background-color: #FFFFFF !important;
    color: #003DFF !important;
    border: 1px solid #003DFF !important;
    border-radius: 6px !important;
    font-weight: 600 !important;
    transition: all 0.2s ease-in-out !important;
}
[data-testid="stFileUploadDropzone"] button:hover {
    background-color: #003DFF !important;
    color: #FFFFFF !important;
}

/* Sikrer at tekst og ikon i uploadfeltet er blå og ikke hvide */
[data-testid="stFileUploadDropzone"] svg,
[data-testid="stFileUploadDropzone"] span {
    color: #003DFF !important;
    fill: #003DFF !important;
}

.stButton button {
    background-color: #FFFFFF !important;
    color: #003DFF !important;
    border: 1px solid #003DFF !important;
    border-radius: 6px !important;
    font-weight: 600 !important;
    transition: all 0.2s ease-in-out !important;
}

.stButton button:hover {
    background-color: #003DFF !important;
    color: #FFFFFF !important;
}

/* ====== SIDEBAR ====== */
[data-testid="stSidebar"] {
    background-color: #F8F9FF !important;
    border-right: 1px solid #E0E0E0 !important;
}

/* ====== LABELS ====== */
label, .st-emotion-cache-16idsys p {
    color: #000000 !important;
}

/* Fjern mørke skygger */
.stTextInput, .stTextArea, .stDateInput, .stNumberInput {
    box-shadow: none !important;
}
</style>
""", unsafe_allow_html=True)
st.title(APP_TITLE)
st.write(APP_DESC)

# Sidebar inputs
with st.sidebar:
    st.header("Indstillinger")
    openai_api_key = st.text_input("OpenAI API-nøgle", type="password")
    if openai_api_key:
        globals()["openai_api_key"] = openai_api_key
    if openai_api_key:
        st.session_state["openai_api_key"] = openai_api_key
    show_gantt = st.checkbox("Vis Gantt-graf", value=True)

# Persistent state for scraped data and AI results
if "scraped_data" not in st.session_state:
    st.session_state.scraped_data = None
if "strategy_text" not in st.session_state:
    st.session_state.strategy_text = ""
if "campaign_plan_text" not in st.session_state:
    st.session_state.campaign_plan_text = ""
if "ad_texts_text" not in st.session_state:
    st.session_state.ad_texts_text = ""

# Fallback-variabel, så ad_texts_text altid eksisterer i global scope
ad_texts_text = st.session_state.get("ad_texts_text", "")

with st.form("input_form"):
    col1, col2 = st.columns(2)
    with col1:
        kunde_navn = st.text_input("Kunde-navn", value=st.session_state.get("kunde_navn", ""))
        website_url = st.text_input("Website-URL", placeholder="https://www.eksempel.dk", value=st.session_state.get("website_url", ""))
        månedligt_budget = st.number_input("Månedligt budget (DKK)", min_value=0, step=1000, value=st.session_state.get("månedligt_budget", 0))
        antal_kampagner = st.number_input("Antal kampagner i kontraktåret", min_value=1, step=1, value=st.session_state.get("antal_kampagner", 4))
        vigtige_services = st.text_area("Vigtige services/undersider (én per linje)", height=100, value=st.session_state.get("vigtige_services", ""))
    with col2:
        startdato = st.date_input("Startdato (default = i dag + 7)", value=st.session_state.get("startdato", DEFAULT_STARTDATE))
        konkurrent_urls_raw = st.text_area("Konkurrent-URL’er (én per linje)", value=st.session_state.get("konkurrent_urls_raw", ""))
        uploaded_file = st.file_uploader("Upload Xpect (Word-dokument)", type=["docx"])
        xpect_text_manual = st.text_area("...eller indsæt Xpect-tekst her", height=200, value=st.session_state.get("xpect_text_manual", ""))
    submit_button = st.form_submit_button("Generér strategi")

if submit_button:
    # Save inputs to session state
    st.session_state.kunde_navn = kunde_navn
    st.session_state.website_url = website_url
    st.session_state.månedligt_budget = månedligt_budget
    st.session_state.antal_kampagner = antal_kampagner
    st.session_state.vigtige_services = vigtige_services
    st.session_state.startdato = startdato
    st.session_state.konkurrent_urls_raw = konkurrent_urls_raw
    st.session_state.xpect_text_manual = xpect_text_manual

    # Prepare Xpect text
    xpect_text = ""
    if uploaded_file is not None:
        try:
            doc = docx.Document(uploaded_file)
            xpect_text = "\n".join([para.text for para in doc.paragraphs if para.text.strip()])
            st.success("✅ Xpect-dokument indlæst.")
        except Exception as e:
            st.error(f"Kunne ikke læse Xpect Word-fil: {e}")
    elif xpect_text_manual.strip():
        xpect_text = xpect_text_manual.strip()
        st.success("✅ Xpect-tekst indlæst.")
    else:
        st.info("Ingen Xpect-tekst angivet (fortsætter uden Xpect-data).")

    if not website_url:
        st.error("Angiv venligst kundens Website-URL.")
        st.stop()

    if not openai_api_key:
        st.error("Indtast venligst din OpenAI API-nøgle i venstre side.")
        st.stop()

    competitor_list = [u.strip() for u in (konkurrent_urls_raw or "").splitlines() if u.strip()]
    st.info("Kører scraping af kunden og konkurrenter. Første kørsel kan tage lidt tid.")

    with st.spinner("Scraper indhold..."):
        try:
            data = run_scrape(website_url, competitor_list)
            st.session_state.scraped_data = data
        except Exception as e:
            st.error(f"Scraping fejlede: {e}")
            data = {"customer": [], "competitors": {}}
            st.session_state.scraped_data = data

    # --------- PREVIEW: CUSTOMER ---------
    st.subheader("Scraping – Kunde (preview)")
    cust_pages = st.session_state.scraped_data.get("customer", []) if st.session_state.scraped_data else []
    if not cust_pages:
        st.warning("Ingen sider blev hentet fra kundens site.")
    else:
        # Build customer table
        cust_rows = []
        for p in cust_pages:
            cust_rows.append({
                "URL": p.get("url", ""),
                "Title": p.get("title", ""),
                "Meta desc": p.get("meta_description", "")[:160],
                "H1": " | ".join(p.get("h1", [])[:3]),
                "CTA count": len(p.get("ctas", [])),
                "GA4": "✓" if p.get("tracking", {}).get("ga4") else "",
                "Pixel": "✓" if p.get("tracking", {}).get("meta_pixel") else "",
                "Klaviyo": "✓" if p.get("tracking", {}).get("klaviyo") else "",
            })
        st.dataframe(pd.DataFrame(cust_rows), use_container_width=True)

        # USP/Tone seed (simple heuristic preview; GPT kommer senere)
        st.caption("Forhåndsindsigt (heuristik): Top H1/H2 signaler fra kundens site.")
        all_h = []
        for p in cust_pages:
            all_h.extend(p.get("h1", []) + p.get("h2", []))
        top_h = pd.Series(all_h).value_counts().head(10) if all_h else pd.Series(dtype=int)
        if not top_h.empty:
            st.write(top_h)

    # --------- PREVIEW: COMPETITORS ---------
    if competitor_list:
        st.subheader("Scraping – Konkurrenter (preview)")
        for c_url, pages in st.session_state.scraped_data.get("competitors", {}).items():
            st.markdown(f"**{c_url}**")
            if not pages:
                st.write("— ingen sider hentet.")
                continue
            rows = []
            for p in pages:
                rows.append({
                    "URL": p.get("url", ""),
                    "Title": p.get("title", ""),
                    "H1": " | ".join(p.get("h1", [])[:2]),
                    "CTA count": len(p.get("ctas", [])),
                    "GA4": "✓" if p.get("tracking", {}).get("ga4") else "",
                    "Pixel": "✓" if p.get("tracking", {}).get("meta_pixel") else "",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True)

    st.divider()

    # ---------- AI-GENERATED STRATEGY ----------
    st.header("Strategioversigt")
    with st.spinner("Genererer strategi med AI..."):
        prompt_strategy = generate_strategy_text(cust_pages, st.session_state.scraped_data.get("competitors", {}) if st.session_state.scraped_data else {}, xpect_text, vigtige_services)
        strategy_text = call_openai_api(prompt_strategy, openai_api_key)
        # Fjern alt fra "4. Digital Marketing Strategi" og ned hvis det findes
        if "4. Digital Marketing Strategi" in strategy_text:
            strategy_text = strategy_text.split("4. Digital Marketing Strategi")[0].strip()
        st.session_state.strategy_text = strategy_text
    st.markdown(strategy_text, unsafe_allow_html=False)

    # ---------- AI-GENERATED KAMPAGNEPLAN ----------
    st.header("Kampagneplan")
    with st.spinner("Genererer kampagneplan med AI..."):
        prompt_campaign = generate_campaign_plan_text(strategy_text, int(antal_kampagner))
        campaign_plan_text = call_openai_api(prompt_campaign, openai_api_key)
        # Fjern alt fra "4. Digital Marketing Strategi" og ned hvis det findes
        if "4. Digital Marketing Strategi" in campaign_plan_text:
            campaign_plan_text = campaign_plan_text.split("4. Digital Marketing Strategi")[0].strip()
        st.session_state.campaign_plan_text = campaign_plan_text
    st.markdown(campaign_plan_text, unsafe_allow_html=False)

    # Byg et Gantt-årshjul: 2 always-on + (antal_kampagner - 2) sæsonkampagner
    start_dt = datetime.combine(startdato, datetime.min.time())
    always_on = [
        dict(Kampagne="Brand Awareness (Always on)", Start=start_dt, Slut=start_dt + timedelta(days=365)),
        dict(Kampagne="Retargeting & Loyalty (Always on)", Start=start_dt, Slut=start_dt + timedelta(days=365)),
    ]
    # Udled kampagnenavne fra annoncetekster (brug linjer der ligner overskrifter)
    derived_names = []
    import re
    for line in (st.session_state.ad_texts_text or ad_texts_text or "").splitlines():
        s = line.strip()
        if not s or s.startswith(("\"", "'", "CTA", "Hook")):
            continue
        if len(s) > 80:
            continue
        if re.match(r"^[A-Za-zÆØÅæøå0-9].+", s) and s.lower() not in {"strategioversigt","målgrupper og annoncetekster","kampagneplan"}:
            derived_names.append(s)
    # Unikke og begræns til ønsket antal
    seen = set()
    uniq_names = []
    for n in derived_names:
        if n not in seen:
            uniq_names.append(n); seen.add(n)
    # Antal sæsonkampagner = antal_kampagner - 2 (minimum 0)
    n_seasonal = max(0, int(antal_kampagner) - 2)
    seasonal = []
    # Fordel sæsonkampagner jævnt over året
    if n_seasonal > 0:
        offsets = [int(365*(i+1)/(n_seasonal+1)) for i in range(n_seasonal)]
    else:
        offsets = []
    for i, off in enumerate(offsets):
        if i >= n_seasonal:
            break
        name = (uniq_names[i] if i < len(uniq_names) else f"Sæsonkampagne {i+1}")
        s = start_dt + timedelta(days=off)
        e = s + timedelta(days=45)
        seasonal.append(dict(Kampagne=name, Start=s, Slut=e))
    kampagner = always_on + seasonal
    df = pd.DataFrame(kampagner)
    df["Start"] = pd.to_datetime(df["Start"])
    df["Slut"] = pd.to_datetime(df["Slut"])
    df["Varighed"] = (df["Slut"] - df["Start"]).dt.days + 1

    if show_gantt:
        fig = px.timeline(df, x_start="Start", x_end="Slut", y="Kampagne", title="Årshjul (Gantt) for kampagner")
        fig.update_yaxes(autorange="reversed")
        st.plotly_chart(fig, use_container_width=True)

    # ---------- AI-GENERATED ANNONCETEKSTER ----------
    st.header("Målgrupper og annoncetekster")
    with st.spinner("Genererer annoncetekster med AI..."):
        prompt_ads = generate_ad_texts_text(strategy_text)
        ad_texts_text = call_openai_api(prompt_ads, openai_api_key)
        # Fjern alt fra "4. Digital Marketing Strategi" og ned hvis det findes
        if "4. Digital Marketing Strategi" in ad_texts_text:
            ad_texts_text = ad_texts_text.split("4. Digital Marketing Strategi")[0].strip()
        # Udvid "Annoncetekst til Sociale Medier" sektionen hvis den findes
        if "Annoncetekst til Sociale Medier" in ad_texts_text:
            ad_texts_text += """

Del Din Hudpleje-Rutine!

"Vi vil høre fra dig! Del din hudpleje-rutine med SkinSense-produkter og deltag i vores giveaway. Vind en luksuriøs hudplejepakke! #SkinSenseRoutine"

Konkurrence: Vind En Gratis Behandling!

"Vil du have en GRATIS hudbehandling? Følg os og tag en ven i kommentaren for at deltage. Vi trækker en vinder på fredag! #SkinSenseGiveaway"

Følg Med i Vores Behandlinger!

"Se hvordan vores specialister forvandler hud! Tjek vores seneste videoer med behandlinger og kundeanmeldelser. Følg os for mere inspiration! #SkinSenseBeauty"
"""
        st.session_state.ad_texts_text = ad_texts_text
    st.markdown(ad_texts_text, unsafe_allow_html=False)

    # ---------- PDF DOWNLOAD ----------
    st.divider()
    pdf_buffer = generate_pdf(
        kunde_navn or "kunden",
        strategy_text,
        st.session_state.scraped_data.get("competitors", {}) if st.session_state.scraped_data else {},
        df
    )
    st.download_button(
        label="📄 Download PDF-rapport",
        data=pdf_buffer,
        file_name=f"strategirapport_{(kunde_navn or 'kunden').replace(' ', '_')}.pdf",
        mime="application/pdf"
    )

