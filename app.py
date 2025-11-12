import os
import re
import requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup

#
# =============================
# App config
# =============================
st.set_page_config(page_title="Meta Ads Strategi", layout="wide")
st.title("Meta Ads Strategi")


#
# =============================
# Sidebar (inputs)
# =============================
with st.sidebar:
    st.header("Indstillinger")
    api_key = st.text_input("OpenAI API-nøgle", type="password")
    model = st.selectbox(
        "Model",
        ["gpt-5", "gpt-4o", "gpt-4o-mini"],
        index=0
    )
    st.divider()

#
# =============================
# Main inputs
# =============================
col1, col2 = st.columns(2)
with col1:
    customer_name = st.text_input("Kundenavn")
    website = st.text_input("Website (https://…)")
    monthly_budget = st.number_input("Månedligt budget (DKK)", min_value=0, step=1000, value=0)
    other_info = st.text_area("Egne idéer / Anden vigtig info", height=120)
    competitors_raw = st.text_area("Konkurrenter (én per linje)", height=120)

with col2:
    ao_rt_campaigns = st.number_input("Antal AO/RT kampagner", min_value=0, max_value=10, value=1, step=1)
    push_campaigns = st.number_input("Antal Push kampagner", min_value=0, max_value=10, value=2, step=1)
    xpect_doc = st.file_uploader("Xpect (DOCX/TXT/PDF)", type=["docx", "txt", "pdf"])
    ad_data_file = st.file_uploader("Eksisterende data fra annoncekonto (CSV/Excel)", type=["csv", "xlsx"])

generate_btn = st.button("Generer strategi")

#
# =============================
# Helpers
# =============================

def safe_read_xpect(file, manual_text=""):
    """Returnér ren tekst fra Xpect upload (eller tom)."""
    if file is not None:
        ext = os.path.splitext(file.name.lower())[1]
        try:
            if ext == ".txt":
                return file.read().decode("utf-8", errors="ignore")
            if ext == ".pdf":
                data = file.read()
                txt = data.decode("latin-1", errors="ignore")
                return re.sub(r"[^A-Za-zÆØÅæøå0-9 ,.\-–:;()\n]+", " ", txt)
            if ext == ".docx":
                import docx
                doc = docx.Document(file)
                return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
        except Exception:
            return manual_text.strip()
    return manual_text.strip()


def summarize_ad_account(df: pd.DataFrame) -> str:
    """Kort tekstopsummering af eksisterende kontodata (hvis uploadet)."""
    try:
        cols = [c.lower() for c in df.columns]
        out = []
        def col(name): return df.columns[cols.index(name)]
        if "spend" in cols or "cost" in cols:
            spend_col = col("spend") if "spend" in cols else col("cost")
            total_spend = df[spend_col].sum(numeric_only=True)
            out.append(f"Samlet spend: {total_spend:,.0f} DKK")
        if "impressions" in cols:
            out.append(f"Impressions: {df[col('impressions')].sum(numeric_only=True):,.0f}")
        if "clicks" in cols:
            out.append(f"Klik: {df[col('clicks')].sum(numeric_only=True):,.0f}")
        if "conversions" in cols:
            out.append(f"Konverteringer: {df[col('conversions')].sum(numeric_only=True):,.0f}")
        if "cpa" in cols:
            out.append(f"Gns. CPA: {df[col('cpa')].mean(numeric_only=True):,.2f} DKK")
        return " | ".join(out) if out else "Ingen standard KPI-kolonner genkendt – data vedlagt som rå bilag."
    except Exception:
        return "Kunne ikke opsummere kontodata – behandles som rå bilag."


def simple_scrape(url: str, timeout_sec: int = 10) -> dict:
    """Let scraping: forsiden + op til 3 interne links (titel, meta, H1/H2)."""
    out = {"homepage": {}, "samples": []}
    if not url or not url.startswith("http"):
        return out
    try:
        r = requests.get(url, timeout=timeout_sec, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "lxml")
        title = soup.title.text.strip() if soup.title else ""
        md = soup.find("meta", {"name": "description"})
        meta = md.get("content", "").strip() if md else ""
        h1 = [h.get_text(" ", strip=True) for h in soup.find_all("h1")][:5]
        h2 = [h.get_text(" ", strip=True) for h in soup.find_all("h2")][:8]
        out["homepage"] = {"title": title, "meta": meta, "h1": h1, "h2": h2}

        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith("http") and url.split("/")[2] in href:
                links.append(href)
            elif href.startswith("/"):
                base = url.rstrip("/")
                links.append(base + href)
            if len(links) >= 3:
                break

        for link in links:
            try:
                r2 = requests.get(link, timeout=timeout_sec, headers={"User-Agent": "Mozilla/5.0"})
                s2 = BeautifulSoup(r2.text, "lxml")
                title2 = s2.title.text.strip() if s2.title else ""
                md2 = s2.find("meta", {"name": "description"})
                meta2 = md2.get("content", "").strip() if md2 else ""
                h1_2 = [h.get_text(" ", strip=True) for h in s2.find_all("h1")][:3]
                out["samples"].append({"url": link, "title": title2, "meta": meta2, "h1": h1_2})
            except Exception:
                continue
    except Exception:
        pass
    return out


def sanitize(txt: str) -> str:
    """Fjern markdown, dobbelte mellemrum og bevar bullets med '•'."""
    if not txt:
        return ""
    txt = txt.replace("**", "")
    txt = re.sub(r"^#{1,6}\s*", "", txt, flags=re.MULTILINE)
    txt = txt.replace("* ", "• ")
    txt = txt.replace("\t", " ")
    txt = re.sub(r" {2,}", " ", txt)
    return txt.strip()

#
# =============================
# Format RAW output for Markdown
# =============================
def format_raw_output(txt: str) -> str:
    """
    Forbedret formattering til onlinevisning.
    """
    if not txt:
        return ""

    # Normalize bullets and trim double spaces
    txt = txt.replace("•", "-")
    txt = re.sub(r" {2,}", " ", txt)

    # --- Section header definitions ---
    main_headers = [
        "Overordnet strategi og målsætning",
        "KPI’er der skal måles på",
        "Elementer der skal trackes på",
        "USP’er og Tone of Voice",
        "Konkurrentanalyse",
        "Generelle spørgsmål til kunden"
    ]
    exec_headers = [
        "6) Forslag til kreativer",
        "7) Kampagneplan for et år",
        "8) Budgetplan for året"
    ]

    def normalize_header(s):
        s = s.strip()
        s = re.sub(r"^\d+\)?\.?\s*", "", s)
        s = re.sub(r"[:.\-–—\s]+$", "", s)
        return s.lower()
    header_map = {normalize_header(h): h for h in main_headers}
    exec_header_map = {normalize_header(h): h for h in exec_headers}

    # --- RAW EXECUTION OUTPUT refactor ---
    # We'll treat exec_headers as main sections, and apply special formatting for campaigns, announcements, fields, etc.
    # Remove "Spørgsmål til kunden" in execution part
    lines = txt.splitlines()
    out = []
    i = 0
    in_exec_section = False
    in_campaign = False
    in_ad = False
    last_section = None
    prev_line_bullet = False
    funnel_stages = ["Awareness", "Consideration", "Conversion", "Loyalty", "Retention", "Prospecting", "Remarketing"]
    field_labels = [
        "Fokus", "Formater", "Målgruppe", "Periode", "KPI’er", "Budget",
        "Hook", "Primærtekst", "Overskrift", "CTA", "Score"
    ]
    # Precompile regexes for performance
    re_exec_header = re.compile(r"^(6\)|7\)|8\))\s*[^:]*", re.IGNORECASE)
    re_campaign = re.compile(r"^Kampagne\s+\d+[:]?")
    re_ad = re.compile(r"^Annonce\s+\d+[:]?")
    re_field = re.compile(r"^(" + "|".join(field_labels) + r")\s*:(.*)")
    re_funnel = re.compile(r"^(" + "|".join(funnel_stages) + r")\s*:(.*)", re.IGNORECASE)
    # To skip "Spørgsmål til kunden" in exec
    def is_spg_kunden(line):
        return line.lower().startswith("spørgsmål til kunden")

    while i < len(lines):
        raw = lines[i].strip()
        if not raw:
            prev_line_bullet = False
            i += 1
            continue

        # Detect RAW EXECUTION OUTPUT section headers (6,7,8)
        norm_exec = normalize_header(raw)
        if norm_exec in exec_header_map:
            # Main execution header
            if out and out[-1] != "":
                out.append("")
            out.append(f"## **{exec_header_map[norm_exec]}**")
            out.append("")
            last_section = exec_header_map[norm_exec]
            in_exec_section = True
            in_campaign = False
            in_ad = False
            prev_line_bullet = False
            i += 1
            continue
        # Detect campaign header
        if in_exec_section and re_campaign.match(raw):
            out.append("")
            out.append("---")
            out.append("")
            out.append(f"## **{raw}**")
            out.append("")
            in_campaign = True
            in_ad = False
            prev_line_bullet = False
            i += 1
            continue
        # Detect funnel stage
        if in_exec_section and re_funnel.match(raw):
            match = re_funnel.match(raw)
            label = match.group(1)
            value = match.group(2).strip()
            out.append("")
            out.append(f"**{label}:**")
            if value:
                out.append(value)
            out.append("")
            prev_line_bullet = False
            i += 1
            continue
        # Detect Annonce X header
        if in_exec_section and re_ad.match(raw):
            out.append("")
            out.append(f"### **{raw}**")
            out.append("")
            in_ad = True
            prev_line_bullet = False
            i += 1
            continue
        # Detect field label
        if in_exec_section and re_field.match(raw):
            match = re_field.match(raw)
            label = match.group(1)
            value = match.group(2).strip()
            out.append("")
            out.append(f"**{label}:**")
            if value:
                out.append(value)
            out.append("")
            prev_line_bullet = False
            i += 1
            continue
        # Remove "Spørgsmål til kunden" in execution
        if in_exec_section and is_spg_kunden(raw):
            # Skip this and any following bullet lines (max 2), and blank lines
            j = i + 1
            skipped = 0
            while j < len(lines) and skipped < 2:
                next_ln = lines[j].strip()
                if next_ln.startswith("-") or next_ln.startswith("•"):
                    skipped += 1
                    j += 1
                elif not next_ln:
                    j += 1
                else:
                    break
            i = j
            continue
        # Bullets: keep and ensure blank line after each bullet
        if in_exec_section and raw.startswith("-"):
            if out and out[-1] and not out[-1].startswith("-"):
                out.append("")
            out.append(raw)
            prev_line_bullet = True
            i += 1
            continue
        # Everything else in execution: just append, with spacing
        if in_exec_section:
            out.append(raw)
            prev_line_bullet = False
            i += 1
            continue

        # --- STRATEGY OUTPUT logic below (unchanged) ---
        # Detect main strategy headers (even if not numbered)
        norm = normalize_header(raw)
        if norm in header_map:
            header_text = header_map[norm]
            if out and out[-1] != "":
                out.append("")
            out.append(f"## **{header_text}**")
            out.append("")
            last_section = header_text
            prev_line_bullet = False
            i += 1
            continue
        # Numbered section headers
        if re.match(r"^\d+\)", raw):
            if out and out[-1] != "":
                out.append("")
            out.append(f"## **{raw}**")
            out.append("")
            last_section = raw
            prev_line_bullet = False
            i += 1
            continue
        # Special handling for "elementer der skal trackes på"
        if "elementer der skal trackes på" in raw.lower():
            out.append("")
            out.append("## **Elementer der skal trackes på**")
            out.append("")
            last_section = "Elementer der skal trackes på"
            prev_line_bullet = False
            i += 1
            continue
        # "Spørgsmål til kunden" logic (for strategy)
        if raw.lower().startswith("spørgsmål til kunden"):
            if out and out[-1] != "":
                out.append("")
            out.append(f"**Spørgsmål til kunden:**")
            # Try to collect the next 2 bullet lines (if present)
            bullet_count = 0
            j = i + 1
            while j < len(lines) and bullet_count < 2:
                next_ln = lines[j].strip()
                if next_ln.startswith("-") or next_ln.startswith("•"):
                    bullet = next_ln[1:].strip()
                    out.append(f"- {bullet}")
                    bullet_count += 1
                    j += 1
                elif not next_ln:
                    j += 1
                else:
                    break
            out.append("")
            i = j
            continue
        # Bullets in strategy: ensure single blank line between bullets
        if raw.startswith("-"):
            if out and out[-1] and not out[-1].startswith("-"):
                out.append("")
            out.append(raw)
            prev_line_bullet = True
            i += 1
            continue
        # Regular text
        out.append(raw)
        prev_line_bullet = False
        i += 1

    # Collapse multiple blank lines to max two
    formatted = "\n".join(out)
    formatted = re.sub(r"\n{3,}", "\n\n", formatted)
    formatted = "\n".join([l.rstrip() for l in formatted.splitlines()])
    # Collapse multiple blank lines between bullets to just one
    def collapse_bullet_newlines(text):
        return re.sub(r"(- .+)\n{2,}(?=- )", r"\1\n", text)
    formatted = collapse_bullet_newlines(formatted)
    formatted = formatted.strip() + "\n"
    return formatted

#
# =============================
# OpenAI wrapper
# =============================

def run_gpt(prompt: str, api_key: str, model: str, max_tokens: int = 2000) -> str:
    """
    OpenAI wrapper for konsistente completion-kald.
    """
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    try:
        temperature_value = 1.0 if model == "gpt-5" else 0.0
        response = client.chat.completions.create(
            model="gpt-5" if model == "gpt-5" else model,
            messages=[
                {"role": "system", "content": "Du er en senior Meta Ads strategist. Svar altid i ren tekst uden markdown."},
                {"role": "user", "content": prompt}
            ],
            temperature=temperature_value
        )
        content = response.choices[0].message.content.strip() if response.choices else ""
        if not content:
            return "[AI-ERROR] Modellen returnerede tomt svar."
        return content
    except Exception as e:
        return f"[AI-ERROR] {e}"

#
# =============================
# Prompts (2-kalds flow)
# =============================

def build_context():
    # Xpect
    xpect_text = safe_read_xpect(xpect_doc, "")

    # Eksisterende kontodata
    ad_account_summary = ""
    if ad_data_file is not None:
        try:
            if ad_data_file.name.endswith(".csv"):
                df = pd.read_csv(ad_data_file)
            else:
                df = pd.read_excel(ad_data_file)
            ad_account_summary = summarize_ad_account(df)
        except Exception:
            ad_account_summary = "Kunne ikke læse filen – ignoreres i analysen."

    # Let website-scrape (valgfrit input til prompten)
    site = simple_scrape(website)

    # Kampagner
    base_ao_rt = max(0, int(ao_rt_campaigns))
    base_push = max(0, int(push_campaigns))
    requested_push_total = base_push + 2  # +2 ekstra push som krav

    # Parse competitors
    competitors = [c.strip() for c in (competitors_raw or "").splitlines() if c.strip()]

    return {
        "customer_name": customer_name,
        "website": website,
        "monthly_budget": monthly_budget,
        "other_info": other_info,
        "xpect": xpect_text,
        "ad_account_summary": ad_account_summary,
        "site": site,
        "ao_rt_count": base_ao_rt,
        "push_count": base_push,
        "requested_push_total": requested_push_total,
        "competitors": competitors,
    }

def prompt_strategy_core(ctx: dict) -> str:
    # Brug lidt site/Xpect som kontekst
    home = ctx["site"].get("homepage", {})
    sample_titles = ", ".join([s.get("title","") for s in ctx["site"].get("samples",[]) if s.get("title")][:3])

    return f"""
Du er senior Meta Ads-strateg og skal levere STRATEGI-KERNEN på DANSK som ren tekst med bullets (•). Følg PRÆCIS denne rækkefølge af sektioner og afslut hver sektion med 'Spørgsmål til kunden:' efterfulgt af 2 bullets.

• Brug nedenstående kontekst under hele udarbejdelsen:
    Kunde: {ctx.get('customer_name') or '(ukendt)'}
    Website: {ctx.get('website') or '(ukendt)'}
    Månedligt budget: {ctx.get('monthly_budget')} DKK
    Anden vigtig info: {ctx.get('other_info') or '(tom)'}
    Xpect (uddrag): {(ctx.get('xpect') or '')[:800]}
    Website (titel/meta/h1 fra forside): {home.get('title','')} | {home.get('meta','')} | {', '.join(home.get('h1',[])[:3])}
    Eksempler på undersider (titler): {sample_titles}
    Eksisterende data fra annoncekonto: {ctx.get('ad_account_summary') or 'Ingen data'}

• STRUKTUR & INDHOLD:
    1. Overordnet strategi og målsætning (5–8 bullets)
        • Beskriv kort de primære strategiske tilgange og mål for Meta Ads-indsatsen, baseret på kontekst og branche.
        • Sektionen afsluttes med 'Spørgsmål til kunden:' og 2 relevante spørgsmål, som uddyber strategi eller målsætning.

    2. KPI’er der skal måles på (6–10 bullets)
        • List de vigtigste Key Performance Indicators, som succesen for indsatsen skal måles ud fra – både standard og evt. branchespecifikke KPI’er.
        • Afslut med 'Spørgsmål til kunden:' efterfulgt af 2 relevante spørgsmål om KPI-prioriteringer eller datatilgængelighed.

    3. Elementer der skal trackes på (events/sider) (6–10 bullets)
        • Beskriv hvilke events eller sider på websitet, der bør opsættes til tracking for at måle kampagnen rigtigt.
        • Afslut med 'Spørgsmål til kunden:' samt 2 spørgsmål, der relaterer til trackingmuligheder eller eksisterende opsætning.

    4. USP’er og Tone of Voice
        • List 4–8 unikke salgspunkter (USP’er) for virksomheden – tag udgangspunkt i tilgængelig kontekst.
        • Angiv efterfølgende 3–5 nøgleord, der beskriver Tone of Voice for kommunikationen.
        • Afslut med 'Spørgsmål til kunden:' og 2 spørgsmål om differentiering eller ønsket tone.

    5. Konkurrentanalyse (4–8 bullets)
        • Vurder og beskriv relevante konkurrenttyper eller konkrete konkurrenter, baseret på kundens branche, website og evt. tilgængelig info.
        • Afslut sektionen med 'Spørgsmål til kunden:' og 2 forslag til samarbejdet om konkurrentindsigt.

Efter punkt 5. Konkurrentanalyse skal der tilføjes en ekstra afsluttende sektion med overskriften:

    6. Generelle spørgsmål til kunden
        • Tilføj 4–5 overordnede spørgsmål, der hjælper rådgiveren med at forstå kundens mål, forventninger og erfaringer med Meta Ads.

• OUTPUTFORMAT:
    • Lever som ren tekst uden markdown, uden ** eller andre formateringer.
    • Hver sektion opdel tydeligt med sektionstitel efterfulgt af bullets.
    • Afslut hver sektion med to relaterede spørgsmål til kunden, mærket: 'Spørgsmål til kunden:'
    • Der SKAL altid være en blank linje mellem hver sektion og undersektion, så output ikke flyder sammen — sørg for at der er tydelig luft mellem alle hovedsektioner.

• Kæd logisk mellem sektionerne så strategien fremstår sammenhængende.
• Overvej altid kontekstdata og tag kun relevante antagelser, hvis information mangler.
• Først udføres en intern kæde af tanker for at sikre sammenhæng og relevans. Brug flere interne overvejelser inden du starter på første sektion.
• Gennemfør alle sektioner, og stop først når alle punkter er dækket.

Eksempel (forkortet, brug relevante branche- og kontekstdata i stedet for eksempler):
Overordnet strategi og målsætning
• Få flere kvalificerede leads via hjemmesideformularen
• Udnytte lookalike-audiences baseret på eksisterende kundedata
...
Spørgsmål til kunden:
• Hvad er et lead i jeres optik?
• Er der særlige målgrupper I ønsker fremhævet?

(NB: Autentiske eksempler vil i praksis være længere og mere branchetilpassede, brug altid [x] til at markere erstattebare felter hvor relevant.)

Vigtige instrukser og mål:
– Følg rækkefølgen og titlerne i sektionerne, og afslut altid med to kunde-spørgsmål
– Ingen markdown eller ekstra formateringer
– Udnyt kontekstdata maksimalt og husk interne overvejelser før du starter
– Fortsæt til alle sektioner er komplet afleveret
"""

def prompt_execution(ctx: dict, strategy_core_text: str) -> str:
    base_names = [f"AO/RT kampagne {i+1}" for i in range(ctx.get("ao_rt_count", 0))]
    base_block = ", ".join(base_names) if base_names else "Brand Awareness (always-on), Retargeting & Loyalty (always-on)"
    push_total = ctx.get("requested_push_total", 2)

    # Prompt med ekstra afsnit om primærtekst-længde efter Annonce 3 Hook/Primærtekst...
    prompt = f"""
Du skal nu bygge EKSEKVERINGEN baseret på strategiens kerne (nedenfor).
Svar på DANSK, i ren tekst (ingen markdown, ingen **). Brug bullets '•' kun hvor det er naturligt.

Sektioner i PRÆCIS denne rækkefølge:

6) Forslag til kreativer — angiv formater og budskaber/storytelling pr. funnel-stage (Awareness, Consideration, Conversion, Loyalty),
tekstprincipper, social proof/UGC, før/efter osv.

7) Kampagneplan for et år — behold always-on kampagner: {base_block}.
Foreslå derudover {push_total} navngivne push-kampagner (korte, skarpe navne).
Der SKAL leveres 3 annoncetekster for hver kampagne, og ingen kampagne må mangle annoncer.
Hver kampagne skal præsenteres med præcis følgende struktur og indeholde 3 annoncetekster pr. kampagne:

Kampagne X
Fokus: (F.eks. Always-on eller kampagnens tema)
Formater: (F.eks. video, karussel, dynamiske annoncer)
Målgruppe: (Kort beskrivelse af målgruppen)
Periode: (Angiv forventet kampagneperiode)
KPI’er: (Angiv de vigtigste KPI’er for kampagnen)
Budget: (F.eks. 30% af det samlede årlige budget)

Annonce 1
Hook: (Kort fængende startlinje)
Primærtekst: (Uddybende tekst som sælger budskabet)
Overskrift: (Kampagnens headline)
CTA: (Call to action – f.eks. Læs mere, Køb nu)
Score: (Vurder styrken 0–100, kun inkluder hvis >90)

Annonce 2
Hook:
Primærtekst:
Overskrift:
CTA:
Score:

Annonce 3
Hook:
Primærtekst:
Overskrift:
CTA:
Score:

Primærtekster skal være 3–5 linjer lange med storytelling, følelser og et klart call-to-action. De skal fremstå som små mikroannoncer med naturligt flow. Hvis de er kortere end 2 linjer, skal de automatisk udbygges med mere beskrivelse og engagement.

8) Budgetplan for året — fordel % pr. kampagne (summer ca. 100% pr. måned).
Overvej faser (fx Q1 opsætning, Q2 skalering, Q3 peak, Q4 loyalitet),
og kom med 3 konkrete anbefalinger til budgetstyring (budstrategier/thresholds).

Strategi-kernen (kontekst):
{strategy_core_text[:4500]}
"""
    # Remove all occurrences (case-insensitive) of "Spørgsmål til kunden" (and any trailing punctuation)
    import re
    prompt = re.sub(r"Spørgsmål til kunden:?(\s*\+\s*2\s*bullets)?", "", prompt, flags=re.IGNORECASE)
    return prompt


# =============================
# DOCX builder
# =============================
def build_docx(customer_name: str, website: str, monthly_budget: int, strategy_core: str, execution_text: str) -> bytes:
    from docx import Document
    from docx.shared import Pt
    from docx.oxml.ns import qn
    from io import BytesIO

    # Required sections in exact order
    CORE_HEADERS = [
        "Overordnet strategi og målsætning",
        "KPI’er der skal måles på",
        "Elementer der skal trackes på",
        "USP’er og Tone of Voice",
        "Konkurrentanalyse",
    ]
    EXEC_HEADERS = [
        "Forslag til kreativer",
        "Kampagneplan for et år",
        "Budgetplan for året",
    ]

    def split_sections(text: str, headers: list[str]) -> dict:
        """
        Robust splitter der genkender en sektion uanset om GPT skriver:
        '1) Header', 'Header:', 'Header —', 'Header -', 'Header–' osv.
        """
        if not text:
            return {h: "" for h in headers}

        lines = text.splitlines()
        cleaned_lines = []
        for ln in lines:
            base = ln.strip().lower()
            base = base.lstrip("0123456789). ").rstrip(": .-–—")
            cleaned_lines.append(base)
        positions = {h: None for h in headers}
        for idx, header in enumerate(headers):
            target = header.lower()
            for i, cl in enumerate(cleaned_lines):
                if cl.startswith(target):
                    positions[header] = i
                    break
        result = {}
        for i, header in enumerate(headers):
            start = positions[header]
            if start is None:
                result[header] = ""
                continue
            next_pos = None
            for h2 in headers[i+1:]:
                if positions[h2] is not None:
                    next_pos = positions[h2]
                    break
            end = next_pos if next_pos is not None else len(lines)
            body = "\n".join(lines[start+1:end]).strip()
            result[header] = body
        return result

    core_sec = split_sections(strategy_core, CORE_HEADERS)
    exec_sec = split_sections(execution_text, EXEC_HEADERS)

    doc = Document()

    # ---- Helpers: TOC + page numbers ----
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    def _add_field_run(paragraph, field_code):
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        r = paragraph.add_run()
        fldChar1 = OxmlElement('w:fldChar')
        fldChar1.set(qn('w:fldCharType'), 'begin')
        r._r.append(fldChar1)

        instr = OxmlElement('w:instrText')
        instr.set(qn('xml:space'), 'preserve')
        instr.text = field_code
        r._r.append(instr)

        fldChar2 = OxmlElement('w:fldChar')
        fldChar2.set(qn('w:fldCharType'), 'separate')
        r._r.append(fldChar2)

        r2 = paragraph.add_run()
        fldChar3 = OxmlElement('w:fldChar')
        fldChar3.set(qn('w:fldCharType'), 'end')
        r2._r.append(fldChar3)

    def add_toc(document):
        # Creates a Word TOC field (updates on open with F9)
        p = document.add_paragraph()
        _add_field_run(p, 'TOC \\o "1-3" \\h \\z \\u')
        p.paragraph_format.space_after = Pt(6)

    def add_page_numbers(document):
        for section in document.sections:
            footer = section.footer
            p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _add_field_run(p, 'PAGE')
            p.add_run(" / ")
            _add_field_run(p, 'NUMPAGES')

    # Title
    doc.add_heading("Meta Ads Strategi", 0)
    # Kunde og website
    p = doc.add_paragraph()
    p.add_run(f"{customer_name or 'Kunde'} — {website or ''}").font.size = Pt(12)
    if monthly_budget:
        p = doc.add_paragraph()
        p.add_run(f"Månedligt budget: {monthly_budget} DKK").font.size = Pt(12)

    # Insert TOC (updates when the file is opened in Word) and page numbers
    add_toc(doc)
    add_page_numbers(doc)

    # Helper: Write section with proper bullet formatting and bold for "Spørgsmål til kunden:"
    def add_section_docx(title, body, heading_level=1):
        doc.add_heading(title, heading_level)
        if not body:
            doc.add_paragraph("(Ingen data modtaget – tjek AI-output)")
            doc.add_paragraph()
            return

        lines = [ln.rstrip() for ln in body.split("\n")]
        buffer = []

        def flush_buffer():
            for ptxt in buffer:
                para = doc.add_paragraph(ptxt)
                para.paragraph_format.space_after = Pt(3)
            buffer.clear()

        for ln in lines:
            p = ln.strip()
            if not p:
                flush_buffer()
                continue

            if p.lower().startswith("spørgsmål til kunden"):
                flush_buffer()
                para = doc.add_paragraph()
                run = para.add_run("Spørgsmål til kunden:")
                run.bold = True
                para.paragraph_format.space_after = Pt(3)
                continue

            # Bullet handling ("- " or "• ")
            if p.startswith("- ") or p.startswith("• "):
                flush_buffer()
                para = doc.add_paragraph(p[2:].strip(), style='List Bullet')
                para.paragraph_format.space_after = Pt(2)
            else:
                buffer.append(p)

        flush_buffer()
        doc.add_paragraph()

    # Helper for "Forslag til kreativer" with stage subheadings and bullets
    def add_section_kreativer_docx(title, body):
        stages = ["Awareness", "Consideration", "Conversion", "Loyalty", "Retention"]
        doc.add_heading(title, 1)
        if not body:
            doc.add_paragraph("(Ingen data modtaget – tjek AI-output)")
            doc.add_paragraph()
            return

        lines = [ln.rstrip() for ln in body.split("\n")]
        current_stage = None
        buffer = []

        def flush_buffer_as_paras():
            for txt in buffer:
                if txt.startswith("- ") or txt.startswith("• "):
                    para = doc.add_paragraph(txt[2:].strip(), style='List Bullet')
                else:
                    para = doc.add_paragraph(txt)
                para.paragraph_format.space_after = Pt(2)
            buffer.clear()

        for ln in lines:
            p = ln.strip()
            if not p:
                flush_buffer_as_paras()
                continue

            # Stage heading line (exact match or ends with colon)
            if any(p.lower().startswith(s.lower()) for s in stages) and (p.lower() in [s.lower() for s in stages] or p.endswith(":")):
                flush_buffer_as_paras()
                stage_title = p.rstrip(":")
                doc.add_heading(stage_title, 2)
                current_stage = stage_title
                continue

            buffer.append(p)

        flush_buffer_as_paras()
        doc.add_paragraph()

    # Helper for Kampagneplan (Heading 2 for underpunkter, improved bullets and ad headings)
    def add_section_kampagneplan_docx(title, body):
        doc.add_heading(title, 1)
        if not body:
            doc.add_paragraph("(Ingen data modtaget – tjek AI-output)")
            doc.add_paragraph()
            return
        lines = [ln.rstrip() for ln in body.split("\n")]
        buffer = []
        def flush_buffer():
            for p in buffer:
                para = doc.add_paragraph(p)
                para.paragraph_format.space_after = Pt(2)
            buffer.clear()
        for ln in lines:
            p = ln.strip()
            if not p:
                flush_buffer()
                continue
            if p.lower().startswith("kampagne"):
                flush_buffer()
                doc.add_heading(p, 2)
                doc.add_paragraph()  # a bit of air
            elif p.lower().startswith("annonce"):
                flush_buffer()
                doc.add_heading(p, 3)
            elif p.startswith("- ") or p.startswith("• ") or any(p.startswith(prefix) for prefix in ["Fokus:", "Formater:", "Målgruppe:", "Periode:", "KPI’er:", "Budget:", "Hook:", "Primærtekst:", "Overskrift:", "CTA:", "Score:"]):
                flush_buffer()
                if p.startswith("- ") or p.startswith("• "):
                    para = doc.add_paragraph(p[2:].strip(), style='List Bullet')
                else:
                    # Bold label + normal text for fields
                    if ":" in p:
                        label, val = p.split(":", 1)
                        para = doc.add_paragraph()
                        r1 = para.add_run(label + ": ")
                        r1.bold = True
                        para.add_run(val.strip())
                    else:
                        para = doc.add_paragraph(p)
                para.paragraph_format.space_after = Pt(2)
            else:
                buffer.append(p)
        flush_buffer()
        doc.add_paragraph()

    # Strategy core
    for header in CORE_HEADERS:
        add_section_docx(header, core_sec.get(header, ""), heading_level=1)
    # Execution
    for header in EXEC_HEADERS:
        if header == "Kampagneplan for et år":
            add_section_kampagneplan_docx(header, exec_sec.get(header, ""))
        elif header == "Forslag til kreativer":
            add_section_kreativer_docx(header, exec_sec.get(header, ""))
        else:
            add_section_docx(header, exec_sec.get(header, ""), heading_level=1)

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


if generate_btn:
    if not api_key:
        st.error("Indtast OpenAI API-nøgle i venstre side.")
        st.stop()

    ctx = build_context()

    # Kald 1: Strategi-kernen
    with st.spinner("Strategi-kernen…"):
        strategy_core_raw = run_gpt(prompt_strategy_core(ctx), api_key, model, max_tokens=3200)
        if strategy_core_raw.startswith("[AI-ERROR]") or strategy_core_raw.startswith("[FEJL]"):
            st.error(strategy_core_raw)
        st.subheader("RAW STRATEGY OUTPUT")
        st.markdown(format_raw_output(strategy_core_raw), unsafe_allow_html=True)
        strategy_core = sanitize(strategy_core_raw)

    # Kald 2: Eksekvering (baseret på kernen)
    with st.spinner("Eksekvering…"):
        execution_raw = run_gpt(prompt_execution(ctx, strategy_core), api_key, model, max_tokens=3200)
        if execution_raw.startswith("[AI-ERROR]") or execution_raw.startswith("[FEJL]"):
            st.error(execution_raw)
        st.subheader("RAW EXECUTION OUTPUT")
        st.markdown(format_raw_output(execution_raw), unsafe_allow_html=True)
        execution_text = sanitize(execution_raw)

    
    try:
        docx_bytes = build_docx(customer_name, website, monthly_budget, strategy_core, execution_text)
        st.success("Word-dokument klar – hent herunder")
        st.download_button(
            "⬇️ Download DOCX",
            data=docx_bytes,
            file_name=f"meta_strategi_{(customer_name or 'kunde').replace(' ','_')}.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    except Exception as e:
        st.error(f"DOCX-export fejlede: {e}")