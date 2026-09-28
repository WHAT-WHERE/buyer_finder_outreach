import csv
import os
import re
import smtplib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

import dns.resolver
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
import requests

# Load environment variables cleanly from the root directory
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path, override=True)

app = Flask(__name__)

EMAIL_REGEX = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
DISCOVERED_BUYERS = []
ASYNC_TASKS = {}

AUDIT_LOG_FILE = os.path.join(os.path.dirname(__file__), "outreach_audit_log.csv")
AUDIT_RECORDS = []


def init_audit_log():
    """Initializes and loads persistent outreach logs from disk."""
    global AUDIT_RECORDS
    if os.path.exists(AUDIT_LOG_FILE):
        try:
            with open(AUDIT_LOG_FILE, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                AUDIT_RECORDS = list(reader)
        except Exception:
            AUDIT_RECORDS = []


init_audit_log()


def log_outreach_record(company, email, subject, status):
    """Logs an outreach attempt in memory and appends to the CSV audit file."""
    record = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "company": company,
        "email": email,
        "subject": subject,
        "status": status,
    }
    AUDIT_RECORDS.insert(0, record)

    file_exists = os.path.exists(AUDIT_LOG_FILE)
    try:
        with open(AUDIT_LOG_FILE, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "timestamp",
                    "company",
                    "email",
                    "subject",
                    "status",
                ],
            )
            if not file_exists:
                writer.writeheader()
            writer.writerow(record)
    except Exception as e:
        print(f"Error persisting audit record: {e}")


def extract_domain(url):
    """Extracts a clean domain name from a URL."""
    if not url:
        return ""
    clean = url.split("//")[-1].split("/")[0].replace("www.", "")
    return clean


def scrape_deep_contacts(base_url):
    """Crawls /contact and /contact-us subpages to discover genuine business emails.
    Uses a strict 1.0-second timeout to avoid stalling search discovery.
    """
    if not base_url:
        return None

    candidate_paths = ["/contact", "/contact-us"]
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url

    for path in candidate_paths:
        target = urljoin(base_url, path)
        try:
            res = requests.get(target, headers=headers, timeout=1.0, allow_redirects=True)
            if res.status_code == 200:
                # 1. Search for mailto: links in raw HTML
                mailto_matches = re.findall(
                    r"mailto:([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)",
                    res.text,
                    re.IGNORECASE,
                )
                if mailto_matches:
                    clean = mailto_matches[0].strip().lower()
                    if not clean.endswith((".png", ".jpg", ".webp", ".svg", ".js")):
                        return clean

                # 2. General regex match across body text
                matches = re.findall(EMAIL_REGEX, res.text)
                for found_email in matches:
                    clean = found_email.strip().lower()
                    if not clean.endswith((".png", ".jpg", ".webp", ".js", ".css", ".svg")):
                        return clean
        except Exception:
            continue

    return None


def verify_buyer_realtime(website_url, email):
    """Performs live health diagnostics:
    1. HTTP Ping to check server connectivity (HTTP < 400).
    2. DNS MX record resolution to check mail deliverability.
    """
    web_active = False
    mail_active = False
    web_note = "Website Unreachable"
    mail_note = "No MX Records"

    # 1. Check Website HTTP Status
    if website_url:
        try:
            head_res = requests.head(
                website_url,
                timeout=4,
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if head_res.status_code < 400:
                web_active = True
                web_note = f"Website Online ({head_res.status_code})"
            else:
                web_note = f"Website Error ({head_res.status_code})"
        except Exception:
            web_note = "Website Timeout/Down"

    # 2. Check Mail Exchange (MX) Server Configuration
    domain = email.split("@")[-1] if "@" in email else ""
    if domain:
        try:
            records = dns.resolver.resolve(domain, "MX", lifetime=4)
            if records:
                mail_active = True
                mail_note = "Mail Server Ready"
        except Exception:
            mail_note = "No Mail Server Found"

    # Compute verdict
    if web_active and mail_active:
        verdict = "Active Now"
    elif web_active or mail_active:
        verdict = "Partially Active"
    else:
        verdict = "Inactive"

    return {
        "verdict": verdict,
        "web_active": web_active,
        "mail_active": mail_active,
        "web_note": web_note,
        "mail_note": mail_note,
    }


def async_email_worker(task_id, email_data):
    """Executes SMTP transmission in a separate worker thread
    to keep Flask request cycles completely non-blocking.
    """
    recipient_email = email_data["email"]
    company_name = email_data["company"]
    subject = email_data["subject"]
    body = email_data["body"]
    smtp_user = email_data["smtp_user"]
    smtp_pass = email_data["smtp_pass"]
    attachment_data = email_data.get("attachment_data")
    attachment_name = email_data.get("attachment_name")

    personalized_body = body.replace("{{company}}", company_name)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user if smtp_user else "export-desk@tradehub.com"
    msg["To"] = recipient_email
    msg.set_content(personalized_body)

    if attachment_data and attachment_name:
        msg.add_attachment(
            attachment_data,
            maintype="application",
            subtype="octet-stream",
            filename=attachment_name,
        )

    delivery_status = "Failed"
    detail_message = ""

    if smtp_user and smtp_pass:
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=12) as server:
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
            delivery_status = "Delivered"
            detail_message = f"Email successfully delivered to {recipient_email}"
        except Exception as e:
            detail_message = f"SMTP Delivery Error: {str(e)[:45]}"
    else:
        time.sleep(1.2)  # Simulated latency for testing
        delivery_status = "Delivered (Simulated)"
        detail_message = f"[Demo Mode] Asynchronously transmitted to {recipient_email}"

    # Record in audit log
    log_outreach_record(company_name, recipient_email, subject, delivery_status)

    # Update background task state
    ASYNC_TASKS[task_id] = {
        "status": "completed",
        "delivery_status": delivery_status,
        "message": detail_message,
    }


# ==========================================
# HTTP ROUTES
# ==========================================


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/find-buyers", methods=["POST"])
def find_buyers():
    global DISCOVERED_BUYERS
    data = request.json or {}
    niche = data.get("niche", "home decor wholesale distributors")
    country = data.get("country", "United States")
    city = data.get("city", "").strip()

    serp_key = os.getenv("SERPAPI_KEY")
    buyers = []
    error_msg = None

    if not serp_key:
        error_msg = "SERPAPI_KEY missing in .env file! Showing fallback data."
    else:
        try:
            # 1. Clean the city input (strip multi-comma inputs if user entered examples)
            clean_city = ""
            if city:
                first_part = city.split(",")[0].replace("e.g.", "").strip()
                if first_part.lower() not in ("optional", "e.g.", "any"):
                    clean_city = first_part

            # 2. Targeted query filtering out aggregators (Etsy, Faire, Amazon)
            location_part = f'"{clean_city}" "{country}"' if clean_city else f'"{country}"'
            search_query = (
                f'{niche} ("boutique buyer" OR "interior showroom" OR "home decor importer" OR "wholesale showroom") '
                f'{location_part} -site:etsy.com -site:faire.com -site:amazon.com -site:ebay.com'
            )

            params = {
                "engine": "google",
                "q": search_query,
                "gl": "us" if "united states" in country.lower() else "uk",
                "hl": "en",
                "num": 8,
                "api_key": serp_key,
            }

            if clean_city:
                params["location"] = f"{clean_city}, {country}"

            # 3. Search with retry protection if SerpApi rejects location parameter
            res = requests.get("https://serpapi.com/search.json", params=params, timeout=25)
            api_data = res.json()

            if "error" in api_data and "location" in api_data.get("error", "").lower():
                params.pop("location", None)
                res = requests.get("https://serpapi.com/search.json", params=params, timeout=25)
                api_data = res.json()

            if "error" in api_data:
                error_msg = f"SerpApi Error: {api_data.get('error')}"
            else:
                # 4. Filter marketplace domains
                ignored = (
                    "facebook.com", "instagram.com", "linkedin.com", "youtube.com",
                    "pinterest.com", "amazon.com", "yelp.com", "etsy.com", "faire.com",
                    "alibaba.com", "aliexpress.com", "walmart.com", "wayfair.com",
                    "ebay.com", "yellowpages.com", "tripadvisor.com", "supplyleader.com"
                )

                for item in api_data.get("organic_results", []):
                    title = item.get("title", "Wholesale Buyer")
                    link = item.get("link", "")
                    snippet = item.get("snippet", "")
                    domain = extract_domain(link)

                    if any(ig in domain.lower() for ig in ignored) or not domain:
                        continue

                    # First, search SerpApi snippet for public email
                    snippet_emails = re.findall(EMAIL_REGEX, snippet)
                    if snippet_emails:
                        email = snippet_emails[0]
                    else:
                        # Deep-Page Scraping: Crawl company's /contact, /contact-us
                        scraped_email = scrape_deep_contacts(link)
                        if scraped_email:
                            email = scraped_email
                        else:
                            # Fallback to domain wholesale address
                            email = f"sales@{domain}"

                    buyers.append({
                        "id": len(buyers) + 1,
                        "company": title.split(" - ")[0].split(" | ")[0][:45],
                        "email": email,
                        "website": link,
                        "city": clean_city.title() if clean_city else "Major Hub",
                        "country": country,
                        "description": (
                            snippet[:120] + "..." if len(snippet) > 120 else snippet
                        ),
                    })

        except Exception as e:
            error_msg = f"Network or Search API Error: {str(e)}"

    # High-quality fallback leads if external search hits quota limits
    if not buyers:
        fallback_city = clean_city.title() if (city and clean_city) else "New York"
        buyers = [
            {
                "id": 1,
                "company": f"Pacific Living Decor ({fallback_city})",
                "email": f"wholesale@pacificliving{fallback_city.lower().replace(' ', '')}.com",
                "website": "https://pacificlivingstyle.com",
                "city": fallback_city,
                "country": country,
                "description": "Nationwide distributor supplying boutique home goods and handcrafted decorations.",
            },
            {
                "id": 2,
                "company": f"American Artisan Furnishings ({fallback_city})",
                "email": f"purchasing@artisanfurnishings{fallback_city.lower().replace(' ', '')}.com",
                "website": "https://americanhomedecor.com",
                "city": fallback_city,
                "country": country,
                "description": "Leading bulk importer and distributor of artisanal and contemporary home decor items.",
            },
        ]

    DISCOVERED_BUYERS = buyers
    return jsonify({
        "status": "success",
        "buyers": buyers,
        "error_alert": error_msg,
    })


@app.route("/api/verify-buyer", methods=["POST"])
def verify_single_buyer():
    """On-demand verification endpoint for a single buyer row."""
    data = request.json or {}
    website = data.get("website", "")
    email = data.get("email", "")

    result = verify_buyer_realtime(website, email)
    return jsonify({"status": "success", "result": result})


@app.route("/api/verify-batch", methods=["POST"])
def verify_batch_buyers():
    """Concurrent parallel verification using ThreadPoolExecutor."""
    data = request.json or {}
    buyers = data.get("buyers", [])

    if not buyers:
        return jsonify({"status": "error", "message": "No buyers provided"}), 400

    def check_item(item):
        index = item.get("index")
        website = item.get("website", "")
        email = item.get("email", "")
        res = verify_buyer_realtime(website, email)
        return {"index": index, "result": res}

    with ThreadPoolExecutor(max_workers=10) as executor:
        batch_results = list(executor.map(check_item, buyers))

    return jsonify({"status": "success", "results": batch_results})


@app.route("/api/send-email-async", methods=["POST"])
def send_email_async():
    """Asynchronous outreach endpoint that hands off to a background worker."""
    task_id = str(uuid.uuid4())
    ASYNC_TASKS[task_id] = {
        "status": "processing",
        "message": "Queuing background SMTP transmission...",
    }

    attachment = request.files.get("catalog")
    att_bytes = attachment.read() if (attachment and attachment.filename) else None
    att_name = attachment.filename if (attachment and attachment.filename) else None

    email_data = {
        "email": request.form.get("email"),
        "company": request.form.get("company", "Partner"),
        "subject": request.form.get("subject", "Export Inquiry"),
        "body": request.form.get("body", ""),
        "smtp_user": request.form.get("smtp_user") or os.getenv("SMTP_EMAIL"),
        "smtp_pass": request.form.get("smtp_pass") or os.getenv("SMTP_PASSWORD"),
        "attachment_data": att_bytes,
        "attachment_name": att_name,
    }

    worker = threading.Thread(
        target=async_email_worker, args=(task_id, email_data), daemon=True
    )
    worker.start()

    return jsonify({"status": "queued", "task_id": task_id})


@app.route("/api/task-status/<task_id>", methods=["GET"])
def get_task_status(task_id):
    """Polls the completion status of a queued background task."""
    task = ASYNC_TASKS.get(task_id, {"status": "not_found"})
    return jsonify(task)


@app.route("/api/audit-logs", methods=["GET"])
def get_audit_logs():
    """Retrieves recent outreach history."""
    return jsonify({"status": "success", "logs": AUDIT_RECORDS[:25]})


@app.route("/api/download-audit-csv", methods=["GET"])
def download_audit_csv():
    """Serves the accumulated campaign outreach log as a CSV download."""
    if not os.path.exists(AUDIT_LOG_FILE):
        with open(AUDIT_LOG_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "company", "email", "subject", "status"])
    return send_file(
        AUDIT_LOG_FILE,
        as_attachment=True,
        download_name="campaign_audit_report.csv",
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)