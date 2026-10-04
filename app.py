import csv
import html
import mimetypes
import os
import re
import smtplib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from urllib.parse import urljoin, urlparse

import dns.resolver
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

app = Flask(__name__)

EMAIL_REGEX = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
DISCOVERED_BUYERS = []
ASYNC_TASKS = {}
SEARCH_OFFSET = 0

AUDIT_LOG_FILE = BASE_DIR / "outreach_audit_log.csv"
AUDIT_RECORDS = []

DEFAULT_NICHES = (
    "home decor boutique gift shop spa wellness center cafe restaurant "
    "hotel interior design candle decor"
)

MAJOR_US_HUBS = [
    "New York", "Los Angeles", "Chicago", "Dallas", "Atlanta",
    "Miami", "Houston", "Seattle", "Austin", "San Francisco",
    "Boston", "Denver", "Phoenix",
]

REQUEST_HEADERS = {
    "User-Agent": os.getenv(
        "SCRAPER_USER_AGENT",
        "ProductZoneInternationalBuyerResearch/1.0",
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.8",
}

CONTACT_PATHS = [
    "/",
    "/contact",
    "/contact-us",
    "/pages/contact",
    "/pages/contact-us",
    "/wholesale",
    "/pages/wholesale",
    "/pages/wholesale-inquiries",
]

IGNORED_EMAIL_DOMAINS = {
    "example.com", "example.org", "example.net",
    "sentry.io", "schema.org", "cloudflare.com",
    "google.com", "googleapis.com", "gstatic.com",
    "facebook.com", "instagram.com", "linkedin.com",
    "youtube.com", "pinterest.com",
}

IGNORED_RESULT_DOMAINS = {
    "facebook.com", "instagram.com", "linkedin.com", "youtube.com",
    "pinterest.com", "amazon.com", "etsy.com", "faire.com",
    "alibaba.com", "aliexpress.com", "walmart.com", "wayfair.com",
    "ebay.com", "yelp.com", "yellowpages.com", "tripadvisor.com",
    "mapquest.com", "wikipedia.org", "reddit.com", "tiktok.com",
    "manta.com", "bbb.org", "zoominfo.com", "dandb.com",
    "chamberofcommerce.com", "superpages.com", "buzzfile.com",
}

INFORMATIONAL_TITLE_TRIGGERS = [
    "how to", "diy", "recipe", "tutorial", "guide", "tips",
    "safety", "wikipedia", "blog", "top 10", "best 10",
]

PREFERRED_EMAIL_PREFIXES = (
    "wholesale@", "buyers@", "buying@", "orders@", "sales@",
    "hello@", "info@", "contact@", "studio@", "office@",
)


def clean_credential(value, remove_spaces=False):
    value = (value or "").strip().strip('"').strip("'")
    value = value.replace("\r", "").replace("\n", "")
    if remove_spaces:
        value = re.sub(r"\s+", "", value)
    return value


def extract_domain(url):
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def normalize_url(url):
    if not url:
        return ""
    return url if url.startswith(("http://", "https://")) else f"https://{url}"


def clean_company_name(title, domain=""):
    name = html.unescape(title or "").strip()
    name = re.split(r"\s+\|\s+|\s+-\s+", name, maxsplit=1)[0].strip()
    name = re.sub(r"\s+", " ", name).strip(" -|")
    if not name or name.lower() in {"home", "contact", "about us"}:
        return domain.split(".")[0].replace("-", " ").title() if domain else "Business"
    return name[:100]


def is_ignored_domain(domain):
    domain = (domain or "").lower()
    if not domain or domain.endswith((".gov", ".mil", ".edu")):
        return True
    return any(domain == d or domain.endswith("." + d) for d in IGNORED_RESULT_DOMAINS)


def is_plausible_email(email):
    email = (email or "").strip().lower()
    if not EMAIL_REGEX.fullmatch(email):
        return False
    domain = email.split("@", 1)[1]
    local = email.split("@", 1)[0]
    if domain in IGNORED_EMAIL_DOMAINS:
        return False
    if local in {"noreply", "no-reply", "donotreply", "do-not-reply"}:
        return False
    return True


def extract_public_emails(page_text):
    """Only extracts addresses visibly present in normal public HTML."""
    if not page_text:
        return []

    text = html.unescape(page_text)
    candidates = set(EMAIL_REGEX.findall(text))

    for match in re.findall(
        r"mailto:\s*([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})",
        text,
        re.I,
    ):
        candidates.add(match)

    return sorted({
        e.strip(" <>\"'();,").lower()
        for e in candidates
        if is_plausible_email(e.strip(" <>\"'();,").lower())
    })


def choose_best_email(emails, company_domain):
    if not emails:
        return None

    company_domain = (company_domain or "").lower()
    company_matches = [
        e for e in emails
        if e.split("@", 1)[1] == company_domain
        or e.split("@", 1)[1].endswith("." + company_domain)
    ]
    candidates = company_matches or emails

    for prefix in PREFERRED_EMAIL_PREFIXES:
        for email in candidates:
            if email.startswith(prefix):
                return email
    return sorted(candidates)[0]


def init_audit_log():
    global AUDIT_RECORDS
    if not AUDIT_LOG_FILE.exists():
        return
    try:
        with AUDIT_LOG_FILE.open("r", encoding="utf-8", newline="") as f:
            AUDIT_RECORDS = list(csv.DictReader(f))
    except Exception:
        AUDIT_RECORDS = []


def log_outreach_record(company, email, subject, status, detail=""):
    record = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "company": company,
        "email": email,
        "subject": subject,
        "status": status,
        "detail": detail,
    }
    AUDIT_RECORDS.insert(0, record)
    del AUDIT_RECORDS[100:]

    file_exists = AUDIT_LOG_FILE.exists()
    try:
        with AUDIT_LOG_FILE.open("a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "timestamp", "company", "email",
                    "subject", "status", "detail",
                ],
            )
            if not file_exists:
                writer.writeheader()
            writer.writerow(record)
    except Exception as exc:
        app.logger.warning("Audit log error: %s", exc)


init_audit_log()


def scrape_public_contact_email(base_url):
    """
    Fetches normal public contact/wholesale pages.
    It deliberately does not decode protected/obfuscated anti-bot email data.
    """
    base_url = normalize_url(base_url)
    if not base_url:
        return None

    company_domain = extract_domain(base_url)
    if not company_domain:
        return None

    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)

    for path in CONTACT_PATHS:
        target = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
        try:
            response = session.get(
                target,
                timeout=float(os.getenv("SCRAPE_TIMEOUT", "7")),
                allow_redirects=True,
            )
            if response.status_code != 200:
                continue

            final_domain = extract_domain(response.url)
            if final_domain and final_domain != company_domain:
                if not (
                    final_domain.endswith("." + company_domain)
                    or company_domain.endswith("." + final_domain)
                ):
                    continue

            selected = choose_best_email(
                extract_public_emails(response.text),
                company_domain,
            )
            if selected:
                return selected

        except (requests.RequestException, ValueError):
            continue
        except Exception:
            continue

    return None


def verify_buyer_realtime(website_url, email):
    web_active = False
    mail_active = False
    web_note = "Website unreachable"
    mail_note = "No MX result"

    website_url = normalize_url(website_url)
    email = (email or "").strip().lower()

    if website_url:
        try:
            response = requests.get(
                website_url,
                timeout=6,
                allow_redirects=True,
                headers=REQUEST_HEADERS,
                stream=True,
            )
            web_active = response.status_code < 400
            web_note = f"Website HTTP {response.status_code}"
            response.close()
        except requests.RequestException as exc:
            web_note = f"Website check failed: {type(exc).__name__}"

    if "@" in email:
        domain = email.rsplit("@", 1)[1]
        try:
            records = dns.resolver.resolve(
                domain, "MX",
                lifetime=float(os.getenv("DNS_TIMEOUT", "4")),
            )
            mail_active = bool(records)
            mail_note = "MX records found" if mail_active else "No MX records"
        except Exception:
            mail_note = "MX lookup failed"

    if web_active and mail_active:
        verdict = "Website + mail domain active"
    elif web_active:
        verdict = "Website active; mail domain not confirmed"
    elif mail_active:
        verdict = "Mail domain active; website not confirmed"
    else:
        verdict = "Not confirmed"

    return {
        "verdict": verdict,
        "web_active": web_active,
        "mail_active": mail_active,
        "web_note": web_note,
        "mail_note": mail_note,
    }


PRODUCT_BUYER_MAP={
 "candle":[("Home Decor","home decor store"),("Gift Shop","gift shop"),("Boutique","boutique"),("Spa","spa"),("Wellness","wellness center"),("Yoga","yoga studio"),("Massage","massage spa"),("Salon & Beauty","beauty salon"),("Cafe","cafe"),("Restaurant","restaurant"),("Bar","bar"),("Hotel","hotel"),("Resort","resort"),("Event Venue","event venue"),("Wedding Venue","wedding venue"),("Event Planner","event planner"),("Interior Design","interior designer"),("Florist","florist"),("Furniture & Decor","furniture store"),("Lifestyle Store","lifestyle store"),("Candle Shop","candle store"),("Theater","movie theater")],
 "holder":[("Home Decor","home decor store"),("Gift Shop","gift shop"),("Boutique","boutique"),("Spa","spa"),("Wellness","wellness center"),("Yoga","yoga studio"),("Restaurant","restaurant"),("Cafe","cafe"),("Bar","bar"),("Hotel","hotel"),("Resort","resort"),("Event Venue","event venue"),("Wedding Venue","wedding venue"),("Event Planner","event planner"),("Interior Design","interior designer"),("Florist","florist"),("Furniture & Decor","furniture store"),("Lifestyle Store","lifestyle store"),("Candle Shop","candle store")],
 "decor":[("Home Decor","home decor store"),("Gift Shop","gift shop"),("Boutique","boutique"),("Furniture & Decor","furniture store"),("Interior Design","interior designer"),("Florist","florist"),("Event Planner","event planner"),("Event Venue","event venue"),("Hotel","hotel"),("Restaurant","restaurant"),("Cafe","cafe"),("Spa","spa"),("Wellness","wellness center"),("Lifestyle Store","lifestyle store")]
}
GENERIC_BUYER_CATEGORIES=[("Home Decor","home decor store"),("Gift Shop","gift shop"),("Boutique","boutique"),("Spa","spa"),("Wellness","wellness center"),("Yoga","yoga studio"),("Salon & Beauty","beauty salon"),("Cafe","cafe"),("Restaurant","restaurant"),("Bar","bar"),("Hotel","hotel"),("Resort","resort"),("Event Venue","event venue"),("Wedding Venue","wedding venue"),("Event Planner","event planner"),("Interior Design","interior designer"),("Florist","florist"),("Furniture & Decor","furniture store"),("Lifestyle Store","lifestyle store"),("Specialty Store","specialty store")]
DEFAULT_PAGES=min(max(int(os.getenv("BUYER_SEARCH_PAGES","3")),1),6)
SEARCH_CATEGORY_WORKERS=max(int(os.getenv("SEARCH_CATEGORY_WORKERS","8")),1)
SCRAPE_WORKERS=max(int(os.getenv("SCRAPE_WORKERS","16")),1)
MAX_WEBSITES_TO_SCRAPE=max(int(os.getenv("MAX_WEBSITES_TO_SCRAPE","60")),1)
SERPAPI_TIMEOUT=float(os.getenv("SERPAPI_TIMEOUT","20"))
SCRAPE_TIMEOUT=float(os.getenv("SCRAPE_TIMEOUT","3.5"))
CONTACT_PATHS=["/","/contact","/contact-us"]

US_STATES={"alabama":"Alabama","alaska":"Alaska","arizona":"Arizona","arkansas":"Arkansas","california":"California","colorado":"Colorado","connecticut":"Connecticut","delaware":"Delaware","florida":"Florida","georgia":"Georgia","hawaii":"Hawaii","idaho":"Idaho","illinois":"Illinois","indiana":"Indiana","iowa":"Iowa","kansas":"Kansas","kentucky":"Kentucky","louisiana":"Louisiana","maine":"Maine","maryland":"Maryland","massachusetts":"Massachusetts","michigan":"Michigan","minnesota":"Minnesota","mississippi":"Mississippi","missouri":"Missouri","montana":"Montana","nebraska":"Nebraska","nevada":"Nevada","new hampshire":"New Hampshire","new jersey":"New Jersey","new mexico":"New Mexico","new york":"New York","north carolina":"North Carolina","north dakota":"North Dakota","ohio":"Ohio","oklahoma":"Oklahoma","oregon":"Oregon","pennsylvania":"Pennsylvania","rhode island":"Rhode Island","south carolina":"South Carolina","south dakota":"South Dakota","tennessee":"Tennessee","texas":"Texas","utah":"Utah","vermont":"Vermont","virginia":"Virginia","washington":"Washington","west virginia":"West Virginia","wisconsin":"Wisconsin","wyoming":"Wyoming"}
STATE_ABBR={"AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California","CO":"Colorado","CT":"Connecticut","DE":"Delaware","FL":"Florida","GA":"Georgia","HI":"Hawaii","ID":"Idaho","IL":"Illinois","IN":"Indiana","IA":"Iowa","KS":"Kansas","KY":"Kentucky","LA":"Louisiana","ME":"Maine","MD":"Maryland","MA":"Massachusetts","MI":"Michigan","MN":"Minnesota","MS":"Mississippi","MO":"Missouri","MT":"Montana","NE":"Nebraska","NV":"Nevada","NH":"New Hampshire","NJ":"New Jersey","NM":"New Mexico","NY":"New York","NC":"North Carolina","ND":"North Dakota","OH":"Ohio","OK":"Oklahoma","OR":"Oregon","PA":"Pennsylvania","RI":"Rhode Island","SC":"South Carolina","SD":"South Dakota","TN":"Tennessee","TX":"Texas","UT":"Utah","VT":"Vermont","VA":"Virginia","WA":"Washington","WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming"}

def normalize_location(city,state,country):
    parts=[x.strip() for x in (city or "").split(",") if x.strip()]; city_name=parts[0] if parts else (city or "").strip(); state_name=(state or "").strip()
    if not state_name:
        for x in parts[1:]:
            if x.lower() in US_STATES: state_name=US_STATES[x.lower()]; break
            if x.upper() in STATE_ABBR: state_name=STATE_ABBR[x.upper()]; break
    if state_name.lower() in US_STATES: state_name=US_STATES[state_name.lower()]
    elif state_name.upper() in STATE_ABBR: state_name=STATE_ABBR[state_name.upper()]
    city_name=re.sub(r"\s+city$","",city_name,flags=re.I).strip()
    country=(country or "United States").strip()
    return {"city":city_name,"state":state_name,"country":country,"display":", ".join(x for x in (city_name,state_name,country) if x)}

def expand_product_to_categories(product):
    p=(product or "").lower(); out=[]
    for k,v in PRODUCT_BUYER_MAP.items():
        if k in p: out.extend(v)
    if any(x in p for x in ("candle","votive","tea light","tealight")): out.extend(PRODUCT_BUYER_MAP["candle"])
    if any(x in p for x in ("holder","candle holder","votive holder")): out.extend(PRODUCT_BUYER_MAP["holder"])
    if any(x in p for x in ("decor","decoration","decorative")): out.extend(PRODUCT_BUYER_MAP["decor"])
    if not out: out=GENERIC_BUYER_CATEGORIES[:]
    seen=set(); result=[]
    for label,q in out:
        if q not in seen: seen.add(q); result.append((label,q))
    return result

def scrape_public_contact_email(base_url):
    domain=extract_domain(base_url); base_url=normalize_url(base_url)
    if not domain: return None
    session=requests.Session(); session.headers.update(REQUEST_HEADERS)
    for path in CONTACT_PATHS:
        try:
            r=session.get(urljoin(base_url.rstrip("/")+"/",path.lstrip("/")),timeout=float(os.getenv("SCRAPE_TIMEOUT","7")),allow_redirects=True)
            if r.status_code!=200: continue
            final=extract_domain(r.url)
            if final and final!=domain and not (final.endswith("."+domain) or domain.endswith("."+final)): continue
            text=html.unescape(r.text); found=set(EMAIL_REGEX.findall(text)); found.update(re.findall(r"mailto:\s*([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})",text,re.I))
            emails=[]
            for e in found:
                e=e.strip(" <>\"'();,").lower(); local,ed=e.split("@",1)
                if EMAIL_REGEX.fullmatch(e) and ed not in IGNORED_EMAIL_DOMAINS and local not in {"noreply","no-reply","donotreply","do-not-reply"}: emails.append(e)
            if emails:
                company=[e for e in emails if e.split("@",1)[1]==domain or e.split("@",1)[1].endswith("."+domain)] or emails
                for pref in PREFERRED_EMAIL_PREFIXES:
                    for e in company:
                        if e.startswith(pref): return e
                return sorted(company)[0]
        except Exception: pass
    return None

def place_website(place):
    links=place.get("links") or {}
    for v in (place.get("website"),links.get("website"),place.get("website_link")):
        if isinstance(v,str) and v.strip(): return normalize_url(v)
    return ""

def local_category_search(query,location_text,pages,serp_key):
    results=[]; seen=set(); ll=None
    for page in range(min(max(pages,1),6)):
        params={"engine":"google_maps","type":"search","q":query,"hl":"en","gl":"us","start":page*20,"api_key":serp_key}
        if page==0: params.update({"location":location_text,"z":"14"})
        elif ll: params["ll"]=ll
        else: break
        try: payload=requests.get("https://serpapi.com/search.json",params=params,timeout=SERPAPI_TIMEOUT).json()
        except Exception as exc: app.logger.warning("Maps request failed %s: %s",query,exc); break
        if payload.get("error"): app.logger.warning("Maps API error %s: %s",query,payload["error"]); break
        places=payload.get("local_results") or []
        if not places: break
        for place in places:
            title=(place.get("title") or "").strip()
            if not title: continue
            key=str(place.get("place_id") or place.get("data_id") or place.get("data_cid") or (title.lower()+"|"+(place.get("address") or "").lower()))
            if key in seen: continue
            seen.add(key)
            website=place_website(place); domain=extract_domain(website)
            if domain and is_ignored_domain(domain): website=""; domain=""
            c=place.get("gps_coordinates") or {}
            try:
                if ll is None and c.get("latitude") is not None and c.get("longitude") is not None: ll=f"@{float(c['latitude'])},{float(c['longitude'])},14z"
            except Exception: pass
            results.append({"key":key,"company":clean_company_name(title,domain),"website":website,"domain":domain,"address":place.get("address",""),"phone":place.get("phone",""),"type":place.get("type",""),"rating":place.get("rating"),"reviews":place.get("reviews")})
        if len(places)<20: break
    return results

def find_buyers_from_serpapi(product,country,city,state,pages=3):
    key=clean_credential(os.getenv("SERPAPI_KEY"))
    if not key: return [],"SERPAPI_KEY is missing from .env",[]
    loc=normalize_location(city,state,country)
    if not loc["city"]: return [],"Please enter a target city.",[]
    location=f"{loc['city']}, {loc['state']}, {loc['country']}" if loc["state"] else f"{loc['city']}, {loc['country']}"
    categories=expand_product_to_categories(product)[:max(int(os.getenv("MAX_BUYER_CATEGORIES","8")),1)]
    all_places=[]; stats=[]
    with ThreadPoolExecutor(max_workers=min(SEARCH_CATEGORY_WORKERS,len(categories))) as ex:
        jobs={ex.submit(local_category_search,q,location,pages,key):(label,q) for label,q in categories}
        for fut in as_completed(jobs):
            label,q=jobs[fut]
            try: places=fut.result()
            except Exception: places=[]
            for p in places: p["buyer_category"]=label
            all_places.extend(places); stats.append({"category":label,"query":q,"businesses_found":len(places)})
    unique={}
    for p in all_places:
        d=(p.get("domain") or "").lower(); name=re.sub(r"[^a-z0-9]+"," ",(p.get("company") or "").lower()).strip(); addr=re.sub(r"[^a-z0-9]+"," ",(p.get("address") or "").lower()).strip(); k="domain:"+d if d else "name:"+name+"|address:"+addr
        unique.setdefault(k,p)
    places=[p for p in unique.values() if p.get("website") and p.get("domain")]
    # Fast first pass: scrape only a bounded number of the discovered official sites.
    # This prevents one broad category search from turning into hundreds of slow website requests.
    places=places[:MAX_WEBSITES_TO_SCRAPE]
    buyers=[]
    with ThreadPoolExecutor(max_workers=SCRAPE_WORKERS) as ex:
        jobs={ex.submit(scrape_public_contact_email,p["website"]):p for p in places}
        for fut in as_completed(jobs):
            p=jobs[fut]
            try: email=fut.result()
            except Exception: email=None
            if not email: continue
            buyers.append({"id":0,"company":p["company"],"email":email,"email_source":"Public website","website":p["website"],"city":loc["city"],"state":loc["state"],"country":loc["country"],"category":p.get("buyer_category",""),"business_type":p.get("type",""),"address":p.get("address",""),"phone":p.get("phone",""),"rating":p.get("rating"),"reviews":p.get("reviews"),"description":p.get("address") or p.get("type") or "Local business"})
    seen=set(); final=[]
    for b in sorted(buyers,key=lambda x:((x.get("category") or "").lower(),(x.get("company") or "").lower())):
        if b["email"].lower() in seen: continue
        seen.add(b["email"].lower()); b["id"]=len(final)+1; final.append(b)
    return final,None,stats

def verify_buyer_realtime(website_url, email):
    web_active = False
    mail_active = False
    web_note = "Website unreachable"
    mail_note = "No MX result"

    website_url = normalize_url(website_url)
    email = (email or "").strip().lower()

    if website_url:
        try:
            response = requests.get(
                website_url,
                timeout=6,
                allow_redirects=True,
                headers=REQUEST_HEADERS,
                stream=True,
            )
            web_active = response.status_code < 400
            web_note = f"Website HTTP {response.status_code}"
            response.close()
        except requests.RequestException as exc:
            web_note = f"Website check failed: {type(exc).__name__}"

    if "@" in email:
        domain = email.rsplit("@", 1)[1]
        try:
            records = dns.resolver.resolve(
                domain, "MX",
                lifetime=float(os.getenv("DNS_TIMEOUT", "4")),
            )
            mail_active = bool(records)
            mail_note = "MX records found" if mail_active else "No MX records"
        except Exception:
            mail_note = "MX lookup failed"

    if web_active and mail_active:
        verdict = "Website + mail domain active"
    elif web_active:
        verdict = "Website active; mail domain not confirmed"
    elif mail_active:
        verdict = "Mail domain active; website not confirmed"
    else:
        verdict = "Not confirmed"

    return {
        "verdict": verdict,
        "web_active": web_active,
        "mail_active": mail_active,
        "web_note": web_note,
        "mail_note": mail_note,
    }



def smtp_settings():
    user = clean_credential(os.getenv("SMTP_EMAIL"))
    password = clean_credential(
        os.getenv("SMTP_PASSWORD"), remove_spaces=True
    )
    host = clean_credential(os.getenv("SMTP_HOST")) or "smtp.gmail.com"
    sender_name = clean_credential(
        os.getenv("SMTP_SENDER_NAME")
    ) or "Product Zone International"
    return user, password, host, sender_name


def make_message(
    recipient, company, subject, body,
    attachment_data=None, attachment_name=None
):
    smtp_user, _, _, sender_name = smtp_settings()

    recipient = (recipient or "").strip()
    company = (company or "there").strip()
    subject = (subject or "Glass candle holder collection").strip()
    body = body or ""

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((sender_name, smtp_user))
    msg["To"] = recipient
    msg["Reply-To"] = smtp_user
    msg.set_content(body.replace("{{company}}", company))

    if attachment_data and attachment_name:
        mime_type, encoding = mimetypes.guess_type(attachment_name)
        if not mime_type or encoding:
            mime_type = "application/octet-stream"

        maintype, subtype = mime_type.split("/", 1)
        msg.add_attachment(
            attachment_data,
            maintype=maintype,
            subtype=subtype,
            filename=os.path.basename(attachment_name),
        )

    return msg


def send_smtp_mail(msg, smtp_user=None, smtp_pass=None):
    """
    465 = implicit TLS.
    587 = STARTTLS.

    A 535 response is an authentication failure. Port switching cannot
    repair an invalid/revoked Gmail credential.
    """
    env_user, env_pass, host, _ = smtp_settings()
    user = clean_credential(smtp_user or env_user)
    password = clean_credential(
        smtp_pass or env_pass, remove_spaces=True
    )

    if not user or not password:
        return False, "SMTP credentials are missing."

    errors = []

    try:
        with smtplib.SMTP_SSL(host, 465, timeout=20) as server:
            server.ehlo()
            server.login(user, password)
            server.send_message(msg)
        return True, "Sent successfully using SMTP SSL on port 465."

    except smtplib.SMTPAuthenticationError as exc:
        code = getattr(exc, "smtp_code", None)
        detail = getattr(exc, "smtp_error", b"").decode(
            "utf-8", errors="replace"
        )
        return (
            False,
            f"SMTP authentication rejected ({code}): {detail}. "
            "For Gmail, use the full Gmail address and a current App Password.",
        )

    except Exception as exc:
        errors.append(f"465: {type(exc).__name__}: {exc}")

    try:
        with smtplib.SMTP(host, 587, timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(user, password)
            server.send_message(msg)
        return True, "Sent successfully using STARTTLS on port 587."

    except smtplib.SMTPAuthenticationError as exc:
        code = getattr(exc, "smtp_code", None)
        detail = getattr(exc, "smtp_error", b"").decode(
            "utf-8", errors="replace"
        )
        return (
            False,
            f"SMTP authentication rejected ({code}): {detail}. "
            "The Gmail credentials/App Password are not accepted.",
        )

    except Exception as exc:
        errors.append(f"587: {type(exc).__name__}: {exc}")

    return False, "SMTP connection failed: " + " | ".join(errors)


def smtp_auth_test():
    """Authenticate only; does not send an email."""
    user, password, host, _ = smtp_settings()

    if not user or not password:
        return False, "SMTP_EMAIL or SMTP_PASSWORD is missing."

    errors = []

    for mode in ("ssl", "starttls"):
        try:
            server = (
                smtplib.SMTP_SSL(host, 465, timeout=20)
                if mode == "ssl"
                else smtplib.SMTP(host, 587, timeout=20)
            )

            with server:
                server.ehlo()
                if mode == "starttls":
                    server.starttls()
                    server.ehlo()
                server.login(user, password)

            return True, f"SMTP authentication succeeded using {mode}."

        except smtplib.SMTPAuthenticationError as exc:
            code = getattr(exc, "smtp_code", None)
            detail = getattr(exc, "smtp_error", b"").decode(
                "utf-8", errors="replace"
            )
            return False, f"Authentication rejected ({code}): {detail}"

        except Exception as exc:
            errors.append(f"{mode}: {type(exc).__name__}: {exc}")

    return False, " | ".join(errors)


def async_email_worker(task_id, email_data):
    recipient = (email_data.get("email") or "").strip()
    company = (email_data.get("company") or "there").strip()
    subject = (
        email_data.get("subject")
        or "Glass candle holder collection"
    ).strip()
    body = email_data.get("body") or ""

    smtp_user = clean_credential(email_data.get("smtp_user"))
    smtp_pass = clean_credential(
        email_data.get("smtp_pass"), remove_spaces=True
    )

    attachment_data = email_data.get("attachment_data")
    attachment_name = email_data.get("attachment_name")

    if not recipient:
        message = "Recipient email is missing."
        ASYNC_TASKS[task_id] = {
            "status": "completed",
            "delivery_status": "Failed",
            "message": message,
        }
        return

    try:
        msg = make_message(
            recipient, company, subject, body,
            attachment_data, attachment_name
        )

        success, detail = send_smtp_mail(
            msg,
            smtp_user=smtp_user or None,
            smtp_pass=smtp_pass or None,
        )

        status = "Delivered" if success else "Failed"
        log_outreach_record(
            company, recipient, subject, status, detail
        )

        ASYNC_TASKS[task_id] = {
            "status": "completed",
            "delivery_status": status,
            "message": detail,
        }

    except Exception as exc:
        detail = f"Unexpected email error: {type(exc).__name__}: {exc}"
        log_outreach_record(
            company, recipient, subject, "Failed", detail
        )
        ASYNC_TASKS[task_id] = {
            "status": "completed",
            "delivery_status": "Failed",
            "message": detail,
        }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/find-buyers", methods=["POST"])
def find_buyers():
    global DISCOVERED_BUYERS
    data=request.get_json(silent=True) or {}
    product=(data.get("niche") or "candles").strip(); country=(data.get("country") or "United States").strip(); city=(data.get("city") or "").strip(); state=(data.get("state") or "").strip()
    try: pages=min(max(int(data.get("pages",DEFAULT_PAGES)),1),6)
    except (TypeError,ValueError): pages=DEFAULT_PAGES
    buyers,error,stats=find_buyers_from_serpapi(product,country,city,state,pages)
    DISCOVERED_BUYERS=buyers
    return jsonify({"status":"success" if not error else "partial","buyers":buyers,"error_alert":error,"product":product,"location":normalize_location(city,state,country),"pages_searched":pages,"buyer_categories":[{"label":l,"query":q} for l,q in expand_product_to_categories(product)],"category_stats":stats})

@app.route("/api/verify-buyer", methods=["POST"])
def verify_single_buyer():
    data = request.get_json(silent=True) or {}
    return jsonify({
        "status": "success",
        "result": verify_buyer_realtime(
            data.get("website", ""),
            data.get("email", ""),
        ),
    })


@app.route("/api/verify-batch", methods=["POST"])
def verify_batch_buyers():
    data = request.get_json(silent=True) or {}
    buyers = data.get("buyers", [])

    if not buyers:
        return jsonify({
            "status": "error",
            "message": "No buyers provided.",
        }), 400

    def check_item(item):
        return {
            "index": item.get("index"),
            "result": verify_buyer_realtime(
                item.get("website", ""),
                item.get("email", ""),
            ),
        }

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(check_item, buyers))

    return jsonify({
        "status": "success",
        "results": results,
    })


@app.route("/api/test-smtp", methods=["GET", "POST"])
def test_smtp():
    success, message = smtp_auth_test()
    return jsonify({
        "status": "success" if success else "error",
        "smtp_authenticated": success,
        "message": message,
    }), (200 if success else 400)


@app.route("/api/send-email-async", methods=["POST"])
def send_email_async():
    task_id = str(uuid.uuid4())
    ASYNC_TASKS[task_id] = {
        "status": "processing",
        "message": "Preparing SMTP transmission...",
    }

    attachment = request.files.get("catalog")
    attachment_data = None
    attachment_name = None

    if attachment and attachment.filename:
        attachment_data = attachment.read()
        attachment_name = attachment.filename

    email_data = {
        "email": request.form.get("email"),
        "company": request.form.get("company", "there"),
        "subject": request.form.get(
            "subject",
            "Glass candle holders for your collection",
        ),
        "body": request.form.get("body", ""),
        "smtp_user": clean_credential(request.form.get("smtp_user")),
        "smtp_pass": clean_credential(
            request.form.get("smtp_pass"),
            remove_spaces=True,
        ),
        "attachment_data": attachment_data,
        "attachment_name": attachment_name,
    }

    worker = threading.Thread(
        target=async_email_worker,
        args=(task_id, email_data),
        daemon=True,
    )
    worker.start()

    return jsonify({
        "status": "queued",
        "task_id": task_id,
    })


# FIXED: no backslash before <task_id>
@app.route("/api/task-status/<task_id>", methods=["GET"])
def get_task_status(task_id):
    return jsonify(
        ASYNC_TASKS.get(task_id, {"status": "not_found"})
    )


@app.route("/api/audit-logs", methods=["GET"])
def get_audit_logs():
    return jsonify({
        "status": "success",
        "logs": AUDIT_RECORDS[:25],
    })


@app.route("/api/download-audit-csv", methods=["GET"])
def download_audit_csv():
    if not AUDIT_LOG_FILE.exists():
        with AUDIT_LOG_FILE.open("w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow([
                "timestamp", "company", "email",
                "subject", "status", "detail",
            ])

    return send_file(
        str(AUDIT_LOG_FILE),
        as_attachment=True,
        download_name="campaign_audit_report.csv",
    )


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "smtp_user_configured": bool(smtp_settings()[0]),
        "serpapi_configured": bool(
            clean_credential(os.getenv("SERPAPI_KEY"))
        ),
    })


if __name__ == "__main__":
    app.run(
        debug=os.getenv("FLASK_DEBUG", "true").lower() == "true",
        port=int(os.getenv("PORT", "5000")),
    )